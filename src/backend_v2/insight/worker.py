"""Worker-only execution of durable Manga Insight analysis jobs."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Engine

from src.backend_v2.insight.page_schema import (
    get_run_extra_entities,
    normalize_model_page,
    normalize_page_analysis,
)
from src.backend_v2.insight.provider_runtime import frozen_vlm_config
from src.backend_v2.insight.repository import InsightConflict, InsightRepository
from src.backend_v2.jobs.repository import (
    AttemptFence,
    AttemptFenced,
    JobConflict,
    JobQueueRepository,
)
from src.backend_v2.storage.assets import AssetStorageService
from src.backend_v2.storage.platform_repositories import SettingsRepository
from src.shared.user_logging import json_details, log_result, user_log


class InsightAlgorithms(Protocol):
    def analyze_batch(
        self,
        image_bytes: Sequence[bytes],
        *,
        page_numbers: Sequence[int],
        previous_batches: Sequence[Sequence[Mapping[str, Any]]],
        config: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class ProviderInsightAlgorithms:
    """Multi-image page-analysis provider implementation for the Worker."""

    def analyze_batch(
        self,
        image_bytes: Sequence[bytes],
        *,
        page_numbers: Sequence[int],
        previous_batches: Sequence[Sequence[Mapping[str, Any]]],
        config: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        from src.core.manga_insight.vlm_client import VLMClient
        from src.shared.memory_errors import is_memory_allocation_error

        if not page_numbers or len(image_bytes) != len(page_numbers):
            raise ValueError("Insight batch images and page numbers must align")

        vlm_config = frozen_vlm_config(config)
        prompts = _required_mapping(
            config.get("prompts"),
            "frozen Insight prompts",
        )
        prompt_section = _required_mapping(
            prompts.get("batch_analysis"),
            "frozen Insight batch_analysis prompt",
        )
        prompt = _required_string(
            prompt_section.get("content"),
            "frozen Insight batch_analysis prompt content",
        ).strip().replace("{page_count}", str(len(page_numbers)))
        prompt = prompt.replace("{start_page}", str(page_numbers[0]))
        prompt = prompt.replace("{end_page}", str(page_numbers[-1]))
        previous_context = _format_previous_batches(previous_batches)
        if previous_context:
            prompt += (
                f"\n\n【前文概要（前{len(previous_batches)}批内容）】\n"
                "请参考以下前文信息，保持剧情、人物和事件连贯：\n"
                f"{previous_context}"
            )
        page_number_json = json.dumps(list(page_numbers), ensure_ascii=False)
        analysis_cfg = config.get("analysis") if isinstance(config.get("analysis"), Mapping) else {}
        extra_entities = list(analysis_cfg.get("extraEntities") or config.get("extraEntities") or [])
        knowledge_mode = bool(analysis_cfg.get("knowledgeMode") or config.get("knowledgeMode") or extra_entities)
        if knowledge_mode and not extra_entities:
            extra_entities = ["characters", "locations", "abilities", "items"]

        extra_template_parts = ""
        if extra_entities:
            parts = []
            for ent in extra_entities:
                parts.append(f'"{ent}":[{{"name":"..."}}]')
            extra_template_parts = "," + ",".join(parts)

        page_templates = ",".join(
            (
                '{"page_number":'
                f"{page_number}"
                ',"page_summary":"...","key_events":'
                '[{"summary":"...","importance":"high|medium|normal",'
                '"event_type":"optional"}],"continuity_notes":"...",'
                '"warnings":[{"code":"...","message":"..."}]'
                f"{extra_template_parts}"
                "}"
            )
            for page_number in page_numbers
        )
        common_guidelines = (
            "\n【生成质量与评估标准】\n"
            "1. 生成顺序与摘要质量：必须先完整生成 page_summary，再提取其他字段。"
            "page_summary 必须具备足够的信息量（涵盖时间/地点/核心人物动作/关键对白意图），不得出现一句空泛带过的敷衍描述。\n"
            "2. key_events 的 importance 判定标准（严格执行）：\n"
            "   - high: 推动主线剧情或故事走向的核心转折、主线任务触发、生死战斗、重要角色初次亮相。\n"
            "   - medium: 承上启下的剧情推进、常规信息获悉/线索汇报、关键日常冲突。\n"
            "   - normal: 纯日常对话、逗趣插曲、非关键背景互动。\n"
        )

        if knowledge_mode:
            forbidden = "scene、mood、panels、dialogues、speaker_name、original_text、translated_text"
            extraction_rules = (
                f"{common_guidelines}"
                "\n【知识实体提取规则】\n"
                f"必须提取的实体字段：{', '.join(extra_entities)}。\n"
                "每个实体列表必须是对象数组，且每个对象必须包含非空的 'name' 字段。\n"
                "实体分类与边界定义（严禁混淆）：\n"
                "  - items（物品）：仅收录可随身携带、可移动、具备独立实体属性的具体物品/道具/咒物（如：手机、特级咒物、宿傩之指、硬币）。严禁将建筑物、房间、固定设施、家具或环境结构归入 items！\n"
                "  - locations（地点）：收录场景发生的具体地理位置、建筑、房间、固定设施或环境结构。"
                "【地点粒度与命名规则（严格执行）】：同一场景只保留一个条目，采用完整层级路径命名（例如：'杉泽第三高中·家庭科准备室'、'杉泽第三高中·操场百叶箱'），严禁拆分成'学校'和'房间'两条独立记录！同一场景在连续页面中必须保持完全一致的命名与粒度。\n"
                "  - abilities（能力/招式）：收录明确施展或提及的技能、术式、咒术、超常能力（如：十种影法术·蟾蜍）。\n"
                "  - 确定性原则：实体归属不确定或边界模糊时，宁可留空 []，绝不要归入错误的字段中。\n"
                "强制判定：只要本页有角色、对话、战斗或能力展示，即使不是重大转折，也必须提取实体。只有纯风景页或纯过渡页才允许返回空列表 []。\n"
            )
        else:
            forbidden = "scene、mood、panels、dialogues、speaker_name、original_text、translated_text、characters 或 character_mentions"
            extraction_rules = f"{common_guidelines}\n"

        strict_suffix = (
            f"\n\n{extraction_rules}"
            f"按图片给出的顺序分析这 {len(page_numbers)} 页，"
            f"对应页码依次为 {page_number_json}。只输出 JSON："
            f'{{"pages":[{page_templates}]}}。'
            "pages 必须逐页完整覆盖上述页码且不得重复。"
            f"不要输出 {forbidden}。"
        )
        client = VLMClient(vlm_config)
        valid_pages: dict[int, dict[str, Any]] = {}

        def validate_batch(result: dict[str, Any]) -> dict[str, Any]:
            errors = []
            for page in result["pages"]:
                number = page["page_number"]
                if number in valid_pages:
                    continue
                try:
                    valid_pages[number] = normalize_model_page(
                        page,
                        page_number=number,
                        extra_entities=extra_entities,
                    )
                except ValueError as exc:
                    errors.append(f"第 {number} 页：{exc}")
            missing = [number for number in page_numbers if number not in valid_pages]
            if missing:
                raise ValueError(f"页面 {missing} 尚无有效结果；" + "; ".join(errors))
            return {"pages": [valid_pages[number] for number in page_numbers]}

        async def execute() -> Mapping[str, Any]:
            try:
                return await client.analyze_batch(
                    list(image_bytes),
                    list(page_numbers),
                    (prompt + strict_suffix).strip(),
                    validator=validate_batch,
                )
            except Exception as exc:
                if not valid_pages or is_memory_allocation_error(exc):
                    raise
                user_log("warning", f"本批部分页面重试失败，保留 {len(valid_pages)} 页有效结果｜{exc}")
                return {"pages": [valid_pages[number] for number in page_numbers if number in valid_pages]}

        return asyncio.run(execute())


class InsightAnalysisWorkerService:
    def __init__(
        self,
        *,
        data_root: Path,
        engine: Engine,
        jobs: JobQueueRepository,
        algorithms: InsightAlgorithms | None = None,
    ) -> None:
        self.jobs = jobs
        self.repository = InsightRepository(engine)
        self.storage = AssetStorageService(data_root, engine)
        self.credentials = SettingsRepository(engine)
        self.algorithms = algorithms or ProviderInsightAlgorithms()

    def handle(
        self,
        fence: AttemptFence,
        step: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        kind = _required_string(step.get("stepKind"), "Insight step kind")
        if kind in {"insight_analyze_batch", "insight_analyze_page"}:
            return self._analyze_batch(fence, step)
        if kind == "insight_validate_run":
            return self._validate_run(fence, step)
        if kind == "insight_publish_run":
            return self._publish_run(fence, step)
        raise JobConflict(f"unsupported Insight step: {kind}")

    def _analyze_batch(
        self,
        fence: AttemptFence,
        step: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        config = _required_mapping(
            step.get("config"),
            "frozen Insight job config",
        )
        run_id = _required_string(config.get("runId"), "Insight runId")
        scope = _required_string(config.get("scope"), "Insight scope")
        step_id = _required_string(step.get("stepId"), "Insight stepId")
        page_id = _required_string(step.get("pageId"), "Insight pageId")
        target = self.repository.run_target(run_id=run_id, page_id=page_id)
        analysis_config = _required_mapping(
            config.get("analysis"),
            "frozen Insight analysis config",
        )
        pages_per_batch = _required_positive_integer(
            analysis_config.get("pagesPerBatch"),
            "frozen Insight pagesPerBatch",
        )
        context_batch_count = _required_nonnegative_integer(
            analysis_config.get("contextBatchCount"),
            "frozen Insight contextBatchCount",
        )
        grouping = _analysis_batch_grouping(
            scope=scope,
            analysis_config=analysis_config,
        )
        batch_first_ordinal, batch = self.repository.batch_window(
            run_id=run_id,
            target=target,
            pages_per_batch=pages_per_batch,
            grouping=grouping,
        )
        target_status = _required_string(
            target.get("status"),
            "Insight target status",
        )
        if target_status == "completed":
            checkpoint = {
                "runId": run_id,
                "pageId": page_id,
                "batchStartOrdinal": batch_first_ordinal,
                "batchResultReused": True,
            }
            self.jobs.complete_step(
                fence,
                step_id=step_id,
                checkpoint=checkpoint,
            )
            return {**checkpoint, "__already_published__": True}
        if target_status != "pending":
            error = target.get("error")
            message = (
                str(error.get("message"))
                if isinstance(error, Mapping) and error.get("message")
                else "本页所属批次分析失败"
            )
            code = (
                str(error.get("code"))
                if isinstance(error, Mapping) and error.get("code")
                else "INSIGHT_PAGE_FAILED"
            )
            self.jobs.fail_step(
                fence,
                step_id=step_id,
                code=code,
                message=message,
            )
            return {
                "runId": run_id,
                "pageId": page_id,
                "failed": True,
                "__already_published__": True,
            }

        pending_targets = [
            value for value in batch if value["status"] == "pending"
        ]
        if grouping == "contiguous":
            # Reused pages provide context but cannot join two separate recomputation ranges.
            contiguous_targets = []
            for value in pending_targets:
                if value["page_number_snapshot"] < target["page_number_snapshot"]:
                    continue
                if contiguous_targets and value["page_number_snapshot"] != contiguous_targets[-1]["page_number_snapshot"] + 1:
                    break
                contiguous_targets.append(value)
            pending_targets = contiguous_targets
        try:
            images: list[bytes] = []
            for pending_target in pending_targets:
                source_asset_id = _required_string(
                    pending_target.get("source_asset_id"),
                    "Insight source asset id",
                )
                source_checksum = _required_string(
                    pending_target.get("source_checksum"),
                    "Insight source checksum",
                )
                record = self.storage.get_record(source_asset_id)
                if record is None or record.checksum != source_checksum:
                    raise JobConflict("frozen source asset binding changed")
                path = self.storage.resolve_relative_path(record.relative_path)
                image = path.read_bytes()
                if hashlib.sha256(image).hexdigest() != source_checksum:
                    raise JobConflict("source file checksum failed validation")
                images.append(image)

            page_numbers = [
                _required_positive_integer(
                    value.get("page_number_snapshot"),
                    "Insight page number snapshot",
                )
                for value in pending_targets
            ]
            previous_batches = self.repository.previous_successful_batches(
                run_id=run_id,
                before_page_number=page_numbers[0],
                pages_per_batch=pages_per_batch,
                batch_count=context_batch_count,
                grouping=_analysis_batch_grouping(
                    scope="full", analysis_config=analysis_config,
                ),
            )
            algorithm_config = self._with_vlm_credentials(config)
            raw = self.algorithms.analyze_batch(
                images,
                page_numbers=page_numbers,
                previous_batches=previous_batches,
                config=algorithm_config,
            )
            raw_pages = raw.get("pages") if isinstance(raw, Mapping) else None
            if not isinstance(raw_pages, list):
                raise JobConflict("Insight batch result pages must be an array")
            raw_by_page: dict[int, Mapping[str, Any]] = {}
            for raw_page in raw_pages:
                if not isinstance(raw_page, Mapping):
                    raise JobConflict("Insight batch page result must be an object")
                raw_page_number = raw_page.get("page_number")
                if (
                    isinstance(raw_page_number, bool)
                    or not isinstance(raw_page_number, int)
                    or raw_page_number not in page_numbers
                    or raw_page_number in raw_by_page
                ):
                    raise JobConflict(
                        "Insight batch result contains an unexpected or duplicate page"
                    )
                raw_by_page[raw_page_number] = raw_page

            run_extra_entities = get_run_extra_entities(run_id, self.engine)
            successes: dict[str, dict[str, Any]] = {}
            failures: dict[str, str] = {}
            for pending_target, page_number in zip(
                pending_targets,
                page_numbers,
                strict=True,
            ):
                target_page_id = str(pending_target["page_id_snapshot"])
                raw_page = raw_by_page.get(page_number)
                if raw_page is None:
                    failures[target_page_id] = f"批量分析结果缺少第 {page_number} 页"
                    continue
                try:
                    successes[target_page_id] = normalize_page_analysis(
                        {"pages": [dict(raw_page)]},
                        page_id=target_page_id,
                        source_asset_id=str(pending_target["source_asset_id"]),
                        source_checksum=str(pending_target["source_checksum"]),
                        page_number=page_number,
                        extra_entities=run_extra_entities,
                    )
                except Exception as exc:
                    failures[target_page_id] = self.jobs.redact_attempt_message(
                        fence,
                        str(exc) or exc.__class__.__name__,
                    )
            checkpoint = {
                "runId": run_id,
                "pageId": page_id,
                "batchStartOrdinal": batch_first_ordinal,
                "batchPageIds": [
                    str(value["page_id_snapshot"]) for value in pending_targets
                ],
            }

            def publish(connection) -> None:
                result_ids: dict[str, str] = {}
                for pending_target, page_number in zip(
                    pending_targets,
                    page_numbers,
                    strict=True,
                ):
                    target_page_id = str(pending_target["page_id_snapshot"])
                    canonical = successes.get(target_page_id)
                    if canonical is not None:
                        result_ids[target_page_id] = (
                            InsightRepository.publish_page_success(
                                connection,
                                run_id=run_id,
                                scope=scope,
                                page_id=target_page_id,
                                source_asset_id=str(
                                    pending_target["source_asset_id"]
                                ),
                                source_checksum=str(
                                    pending_target["source_checksum"]
                                ),
                                page_number=page_number,
                                payload=canonical,
                            )
                        )
                    else:
                        InsightRepository.publish_page_failure(
                            connection,
                            run_id=run_id,
                            page_id=target_page_id,
                            code="INSIGHT_PAGE_FAILED",
                            message=failures[target_page_id],
                        )
                checkpoint["analysisResultIds"] = result_ids

            current_failure = failures.get(page_id)
            if current_failure is None:
                self.jobs.complete_step(
                    fence,
                    step_id=step_id,
                    checkpoint=checkpoint,
                    publisher=publish,
                )
            else:
                self.jobs.fail_step(
                    fence,
                    step_id=step_id,
                    code="INSIGHT_PAGE_FAILED",
                    message=current_failure,
                    publisher=publish,
                )
            if successes:
                log_result(
                    "本批漫画分析结果",
                    json_details({"pages": list(successes.values())}),
                )
            return {
                **checkpoint,
                **({"failed": True} if current_failure is not None else {}),
                "__already_published__": True,
            }
        except AttemptFenced:
            raise
        except Exception as exc:
            message = self.jobs.redact_attempt_message(
                fence,
                str(exc) or exc.__class__.__name__,
            )

            def publish_failure(connection) -> None:
                for pending_target in pending_targets:
                    InsightRepository.publish_page_failure(
                        connection,
                        run_id=run_id,
                        page_id=str(pending_target["page_id_snapshot"]),
                        code="INSIGHT_BATCH_FAILED",
                        message=message,
                    )

            self.jobs.fail_step(
                fence,
                step_id=step_id,
                code="INSIGHT_BATCH_FAILED",
                message=message,
                publisher=publish_failure,
            )
            user_log(
                "error",
                f"本批漫画分析失败｜{message}",
            )
            return {
                "runId": run_id,
                "pageId": page_id,
                "failed": True,
                "__already_published__": True,
            }

    def _validate_run(
        self,
        fence: AttemptFence,
        step: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        config = _required_mapping(
            step.get("config"),
            "frozen Insight job config",
        )
        run_id = _required_string(config.get("runId"), "Insight runId")
        step_id = _required_string(step.get("stepId"), "Insight stepId")
        checkpoint: dict[str, Any] = {}
        try:
            def publish(connection) -> None:
                checkpoint.update(
                    InsightRepository.validate_run_sources(
                        connection,
                        run_id=run_id,
                    )
                )
                if checkpoint["successCount"] == 0:
                    raise InsightConflict(
                        "analysis run has no successful pages"
                    )

            self.jobs.complete_step(
                fence,
                step_id=step_id,
                checkpoint=checkpoint,
                publisher=publish,
            )
            log_result(
                "分析结果校验完成",
                (
                    f"成功页：{checkpoint.get('successCount', 0)}",
                    f"失败页：{checkpoint.get('failureCount', 0)}",
                ),
            )
            return {**checkpoint, "__already_published__": True}
        except AttemptFenced:
            raise
        except Exception as exc:
            message = self.jobs.redact_attempt_message(
                fence,
                str(exc) or exc.__class__.__name__,
            )

            def publish_failure(connection) -> None:
                InsightRepository.mark_run_failed(
                    connection,
                    run_id=run_id,
                )

            self.jobs.fail_step(
                fence,
                step_id=step_id,
                code="INSIGHT_VALIDATION_FAILED",
                message=message,
                publisher=publish_failure,
            )
            user_log(
                "error",
                f"分析结果校验失败｜{message}",
            )
            return {
                "runId": run_id,
                "failed": True,
                "__already_published__": True,
            }

    def _publish_run(
        self,
        fence: AttemptFence,
        step: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        config = _required_mapping(
            step.get("config"),
            "frozen Insight job config",
        )
        run_id = _required_string(config.get("runId"), "Insight runId")
        step_id = _required_string(step.get("stepId"), "Insight stepId")
        checkpoint: dict[str, Any] = {"runId": run_id}
        try:
            def publish(connection) -> None:
                checkpoint.update(
                    InsightRepository.finalize_run(
                        connection,
                        run_id=run_id,
                    )
                )

            self.jobs.complete_step(
                fence,
                step_id=step_id,
                checkpoint=checkpoint,
                publisher=publish,
            )
            log_result(
                "漫画分析结果已发布",
                (
                    f"成功页：{checkpoint.get('successCount', checkpoint.get('publishedCount', 0))}",
                    f"失败页：{checkpoint.get('failureCount', 0)}",
                ),
            )
            return {**checkpoint, "__already_published__": True}
        except AttemptFenced:
            raise
        except Exception as exc:
            message = self.jobs.redact_attempt_message(
                fence,
                str(exc) or exc.__class__.__name__,
            )

            def publish_failure(connection) -> None:
                InsightRepository.mark_run_failed(
                    connection,
                    run_id=run_id,
                )

            self.jobs.fail_step(
                fence,
                step_id=step_id,
                code="INSIGHT_PUBLISH_FAILED",
                message=message,
                publisher=publish_failure,
            )
            user_log(
                "error",
                f"漫画分析发布失败｜{message}",
            )
            return {
                "runId": run_id,
                "failed": True,
                "__already_published__": True,
            }

    def _with_vlm_credentials(
        self,
        config: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            return self.credentials.resolve_credential_sections(
                config,
                ("vlm",),
            )
        except LookupError as exc:
            raise JobConflict(
                "frozen Insight credential version no longer exists"
            ) from exc


def _required_mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise JobConflict(f"{field} must be an object")
    return dict(value)


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JobConflict(f"{field} must be a non-empty string")
    return value


def _required_positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise JobConflict(f"{field} must be a positive integer")
    return value


def _required_nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise JobConflict(f"{field} must be a non-negative integer")
    return value


def _analysis_batch_grouping(
    *,
    scope: str,
    analysis_config: Mapping[str, Any],
) -> str:
    if scope == "chapter":
        return "chapter"
    if scope in {"incremental", "page"}:
        return "contiguous"
    if scope != "full":
        raise JobConflict("frozen Insight scope is invalid")
    layers = analysis_config.get("layers")
    if not isinstance(layers, list) or not layers:
        raise JobConflict("frozen Insight analysis layers are invalid")
    first_layer = layers[0]
    if not isinstance(first_layer, Mapping):
        raise JobConflict("frozen Insight first analysis layer is invalid")
    align_to_chapter = first_layer.get("alignToChapter")
    if not isinstance(align_to_chapter, bool):
        raise JobConflict("frozen Insight first-layer alignment is invalid")
    return "chapter" if align_to_chapter else "global"


def _format_previous_batches(
    batches: Sequence[Sequence[Mapping[str, Any]]],
) -> str:
    formatted: list[str] = []
    total = len(batches)
    for index, batch in enumerate(batches, start=1):
        pages = [
            page for page in batch
            if isinstance(page.get("page_number_snapshot"), int)
        ]
        if not pages:
            continue
        page_numbers = [int(page["page_number_snapshot"]) for page in pages]
        lines = [
            f"【前第{total - index + 1}批：第{min(page_numbers)}-{max(page_numbers)}页】"
        ]
        for page in pages:
            summary = str(page.get("page_summary", "")).strip()
            if summary:
                lines.append(
                    f"第{page['page_number_snapshot']}页：{summary[:600]}"
                )
            events = page.get("key_events")
            if isinstance(events, list):
                event_summaries = [
                    str(event.get("summary", "")).strip()
                    for event in events[:3]
                    if isinstance(event, Mapping)
                    and str(event.get("summary", "")).strip()
                ]
                if event_summaries:
                    lines.append(f"事件：{'; '.join(event_summaries)}")
        formatted.append("\n".join(lines))
    return "\n\n".join(formatted)
