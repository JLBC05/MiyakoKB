"""角色 Skill 蒸馏：把知识库里的角色证据与原画观察整理成可加载的角色档案。

数据来源三处，全部在本地知识库内：

1. 活跃的 ``entity_alignment`` 产物（角色实体、别名、出场页）；
2. 已发布的页面级分析（``analysis_page_results.payload_json`` 的 ``page_summary`` 与 ``key_events``）；
3. 少量关键页的原画观察（经配置的 VLM 直读画面，用于外貌与口吻）。

渲染出来的每一条结论都必须带 ``《第X话》第Y页（全书第Z页）`` 形式的引用；
引用无法回落到知识库的条目会被 ``_sanitize`` 丢弃，绝不编造。
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import re
import threading
from typing import Any
import uuid

from sqlalchemy import Engine, func, insert, select, update
from sqlalchemy.engine import Connection

from src.backend_v2.insight.provider_runtime import frozen_chat_config, frozen_vlm_config
from src.backend_v2.insight.repository import InsightConflict, InsightNotFound
from src.backend_v2.serialization import canonical_json as _json
from src.backend_v2.settings.resolver import SettingsResolver
from src.backend_v2.storage.platform_repositories import SettingsRepository
from src.backend_v2.storage.schema import (
    analysis_artifacts,
    analysis_page_results,
    assets,
    books,
    chapters,
    page_assets,
    pages,
)
from src.backend_v2.timestamps import utcnow

LOGGER = logging.getLogger("saber.worker.insight_character_skill")

CHARACTER_SKILL_KIND = "character_skill"
CHARACTER_CATEGORY = "characters"
DEFAULT_MAX_VISION_PAGES = 15
MAX_VISION_PAGES = 30
VISION_BATCH_SIZE = 5
MAX_EVENT_LINES = 150
MAX_FACT_PAGES = 400

CITATION_RE = re.compile(r"《(?P<chapter>[^》]+)》第(?P<page>\d+)页（全书第(?P<global>\d+)页）")
CHAPTER_NUMBER_RE = re.compile(r"第(?P<number>\d+)[话話]")
VOLUME_APPENDIX_RE = re.compile(r"(?P<volume>\d+)[卷巻](?:附录|附錄|附録)")

SYNTH_SYSTEM = (
    "你是漫画角色档案编辑。你只能使用给定的知识库资料与画面观察记录，"
    "严禁引入外部设定、严禁编造剧情或台词；资料不足就写进 unknowns。"
)

SYNTH_PROMPT = """请为漫画《{book_title}》中的角色「{character}」建立一份角色档案，用于让 AI 扮演或检索该角色。

【硬性规则】
1. 只能使用下面提供的资料；不得引入任何未被资料支持的外部知识或推测。
2. 每条结论都必须带 citation，且 citation 只能从下面出现过的引用里原样复制，格式为 `《第01话》第12页（全书第12页）`。
3. 台词引用（example）必须逐字来自「画面文字」记录，不得改写；没有画面文字证据就不要给台词例子。
4. 纯推断的内容放 confidence=low，并在 note 里写明是推断。

【角色基础信息】
{meta}

【出场分布】
{chapters}

【共现角色（同页出现次数）】
{cooccurrence}

【高频关联项】
{concepts}

【按时间顺序的事件资料（每条含引用）】
{events}

【画面观察记录（来自原画页，含画面台词原文）】
{vision}

请只输出 JSON，结构：
{{
 "one_liner": "一句话概括这个角色（不超过40字）",
 "identity": {{"full_name": "", "aliases": [], "role_in_story": "", "age_impression": "", "first_appearance": "引用", "appearance_pages": 数字}},
 "appearance": [{{"trait": "外貌或服装特征", "confidence": "high|medium|low", "citation": "引用"}}],
 "personality": [{{"trait": "性格特征", "evidence": "资料里的行为依据", "confidence": "high|medium|low", "citation": "引用"}}],
 "speech_style": [{{"pattern": "说话方式或口头禅特征", "example": "台词原文或空字符串", "citation": "引用"}}],
 "relationships": [{{"name": "对方", "relation": "关系", "dynamic": "相处方式", "citation": "引用"}}],
 "timeline": [{{"citation": "引用", "event": "发生了什么", "importance": "high|medium|normal"}}],
 "topics": [{{"name": "能力/话题/道具/概念", "note": "与该角色的关系", "citation": "引用"}}],
 "persona_prompt": "一段可直接给 AI 使用的扮演提示（第三人称，含语气、态度、口癖倾向，不超过200字）",
 "unknowns": ["资料里没有、但读者可能会问的信息"]
}}
时间线请覆盖开端、转折、结局各阶段，最多 40 条；personality 最多 8 条；relationships 最多 10 条。"""

VISION_PROMPT = """你是漫画角色资料整理员。下面会给你 {count} 张同一部漫画（《{book_title}》）的页面图片，按顺序标注为：
{labels}

请只依据画面上真实可见的内容回答，绝对不要推测剧情、不要补写没画出来的设定。
画面中可能有多个角色，请对每个出现的角色分别描述；特别留意名为「{character}」的角色（本作目标角色，可能的称呼：{aliases}）。

只输出一个 JSON 对象，结构必须是：
{{"pages": [{{"page_number": 数字, "characters": [{{"name_hint": "画面或对白里出现的称呼，没有就写未知", "is_target": true/false, "hair": "发型与发色", "eyes": "眼睛特征", "build_age": "体型与年龄感", "face": "脸型与其他面部特征", "outfit": "服装：上衣/下装/外套/配饰", "color_palette": ["画面中该角色主要配色"], "expression": "表情", "posture_habit": "姿态或动作习惯", "panel_text": [{{"speaker": "说话人，未知就写未知", "text": "画面文字原文"}}], "visual_notes": "其他标志性视觉元素"}}], "scene": "场景一句话"}}]}}

要求：
1. 每个 page_number 必须原样使用我给的全书页码；
2. panel_text 只抄写画面上真实可读的文字（台词、旁白、拟声词），读不清就留空数组，不要编造；
3. 如果某张图里没有「{character}」，就把该角色的 is_target 全部设为 false，并如实描述画面里的角色；
4. 不要输出 JSON 以外的任何文字。"""


@dataclass(frozen=True)
class PageEvidence:
    """一页的页面级分析证据。"""

    global_page: int
    chapter_title: str
    chapter_ordinal: int
    page_ordinal: int
    page_id: str
    page_summary: str
    key_events: list[dict[str, Any]]
    characters: list[str]
    other_entities: dict[str, list[str]]
    asset_id: str
    asset_path: Path

    def citation(self) -> str:
        return f"《{self.chapter_title}》第{self.page_ordinal}页（全书第{self.global_page}页）"


def narrative_key(chapter_title: str, page_ordinal: int) -> tuple[int, int, int]:
    """故事时间顺序，而不是书内页码顺序。

    有些来源（例如按下载顺序导入的电子书）章节目录是逆序的，
    所以不能用全书页码排序；正篇按话数、附录按卷号排在正篇之后。
    """
    appendix = VOLUME_APPENDIX_RE.search(chapter_title or "")
    if appendix:
        return (1, int(appendix.group("volume")), page_ordinal)
    main = CHAPTER_NUMBER_RE.search(chapter_title or "")
    if main:
        return (0, int(main.group("number")), page_ordinal)
    return (2, 0, page_ordinal)


class CharacterSkillRepository:
    """角色 Skill 的读写：证据取自知识库，产物落在 ``analysis_artifacts``。"""

    def __init__(self, engine: Engine, data_root: Path) -> None:
        self.engine = engine
        self.data_root = Path(data_root)

    # ---- 读取 ----------------------------------------------------------

    def book_title(self, connection: Connection, book_id: str) -> str:
        row = connection.execute(select(books.c.title).where(books.c.id == book_id)).first()
        if row is None:
            raise InsightNotFound("book not found")
        return str(row[0])

    def alignment(self, connection: Connection, book_id: str) -> dict[str, Any]:
        row = connection.execute(
            select(analysis_artifacts.c.payload_json).where(
                analysis_artifacts.c.book_id == book_id,
                analysis_artifacts.c.kind == "entity_alignment",
                analysis_artifacts.c.is_active.is_(True),
            )
        ).first()
        if row is None or not row[0]:
            raise InsightConflict("该书还没有实体知识网络，请先完成一次知识库构建")
        return json.loads(row[0])

    def characters(self, *, book_id: str) -> list[dict[str, Any]]:
        """角色列表（带是否已生成 Skill 的标记）。"""
        with self.engine.connect() as connection:
            payload = self.alignment(connection, book_id)
            generated = {
                str(row.template): {
                    "revision": int(row.revision),
                    "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
                }
                for row in connection.execute(
                    select(
                        analysis_artifacts.c.template,
                        analysis_artifacts.c.revision,
                        analysis_artifacts.c.updated_at,
                    ).where(
                        analysis_artifacts.c.book_id == book_id,
                        analysis_artifacts.c.kind == CHARACTER_SKILL_KIND,
                        analysis_artifacts.c.is_active.is_(True),
                    )
                ).all()
            }
            chapter_of_page = self._chapter_ordinals_by_page(connection, book_id)
        characters: list[dict[str, Any]] = []
        for entity in payload.get("canonical_entities") or []:
            if entity.get("category") != CHARACTER_CATEGORY:
                continue
            entity_id = str(entity.get("entity_id") or "")
            if not entity_id:
                continue
            chapter_pages = entity.get("pages") or []
            skill = generated.get(entity_id)
            chapter_ordinals: set[int] = set()
            for number in chapter_pages:
                ordinal = chapter_of_page.get(_page_number(number))
                if ordinal is not None:
                    chapter_ordinals.add(ordinal)
            characters.append(
                {
                    "entityId": entity_id,
                    "name": str(entity.get("canonical_name") or ""),
                    "aliases": [str(item) for item in entity.get("aliases") or []],
                    "pageCount": len(chapter_pages),
                    "chapterCount": len(chapter_ordinals),
                    "frequency": int(entity.get("total_frequency") or 0),
                    "hasSkill": skill is not None,
                    "skillRevision": skill["revision"] if skill else None,
                    "skillUpdatedAt": skill["updatedAt"] if skill else None,
                    "updatedAt": skill["updatedAt"] if skill else None,
                }
            )
        characters.sort(key=lambda item: (-item["pageCount"], item["name"]))
        return characters

    def _chapter_ordinals_by_page(self, connection: Connection, book_id: str) -> dict[int, int]:
        """全书页码 → 章节 ordinal（用于统计角色出场话数）。无分析结果时返回空表。"""
        try:
            run_id = self.latest_run_id(connection, book_id)
        except InsightConflict:
            return {}
        rows = connection.execute(
            select(
                analysis_page_results.c.page_number_snapshot,
                chapters.c.ordinal,
            )
            .join(pages, pages.c.id == analysis_page_results.c.page_id_snapshot)
            .join(chapters, chapters.c.id == pages.c.chapter_id)
            .where(
                chapters.c.book_id == book_id,
                analysis_page_results.c.run_id == run_id,
                analysis_page_results.c.status == "published",
            )
        ).all()
        mapping: dict[int, int] = {}
        for row in rows:
            mapping.setdefault(int(row.page_number_snapshot), int(row.ordinal))
        return mapping

    def get_skill(self, *, book_id: str, entity_id: str) -> dict[str, Any]:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    analysis_artifacts.c.payload_json,
                    analysis_artifacts.c.revision,
                    analysis_artifacts.c.updated_at,
                    analysis_artifacts.c.status,
                ).where(
                    analysis_artifacts.c.book_id == book_id,
                    analysis_artifacts.c.kind == CHARACTER_SKILL_KIND,
                    analysis_artifacts.c.template == entity_id,
                    analysis_artifacts.c.is_active.is_(True),
                )
            ).mappings().one_or_none()
        if row is None or not row["payload_json"]:
            raise InsightNotFound("该角色还没有生成 Skill")
        payload = json.loads(row["payload_json"])
        payload["revision"] = int(row["revision"])
        payload["updatedAt"] = row["updated_at"].isoformat() if row["updated_at"] else None
        payload["status"] = str(row["status"])
        return payload

    def latest_run_id(self, connection: Connection, book_id: str) -> str:
        row = connection.execute(
            select(analysis_page_results.c.run_id, func.count().label("total"))
            .join(pages, pages.c.id == analysis_page_results.c.page_id_snapshot)
            .join(chapters, chapters.c.id == pages.c.chapter_id)
            .where(
                chapters.c.book_id == book_id,
                analysis_page_results.c.status == "published",
            )
            .group_by(analysis_page_results.c.run_id)
            .order_by(func.count().desc())
            .limit(1)
        ).first()
        if row is None:
            raise InsightConflict("该书还没有已发布的页面分析结果")
        return str(row[0])

    def page_evidence(
        self,
        *,
        book_id: str,
        run_id: str,
        page_numbers: Iterable[int],
    ) -> list[PageEvidence]:
        wanted = sorted({int(number) for number in page_numbers if int(number) > 0})[: MAX_FACT_PAGES * 4]
        if not wanted:
            return []
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    analysis_page_results.c.page_number_snapshot,
                    analysis_page_results.c.page_id_snapshot,
                    analysis_page_results.c.payload_json,
                    chapters.c.ordinal.label("chapter_ordinal"),
                    chapters.c.title.label("chapter_title"),
                    pages.c.ordinal.label("page_ordinal"),
                    page_assets.c.asset_id,
                    assets.c.relative_path,
                )
                .join(pages, pages.c.id == analysis_page_results.c.page_id_snapshot)
                .join(chapters, chapters.c.id == pages.c.chapter_id)
                .join(
                    page_assets,
                    (page_assets.c.page_id == pages.c.id) & (page_assets.c.role == "source"),
                )
                .join(assets, assets.c.id == page_assets.c.asset_id)
                .where(
                    chapters.c.book_id == book_id,
                    analysis_page_results.c.run_id == run_id,
                    analysis_page_results.c.status == "published",
                    analysis_page_results.c.page_number_snapshot.in_(wanted),
                )
                .order_by(analysis_page_results.c.page_number_snapshot)
            ).all()

        evidence: list[PageEvidence] = []
        seen: set[int] = set()
        for row in rows:
            global_page = int(row.page_number_snapshot)
            if global_page in seen:
                continue
            seen.add(global_page)
            payload = json.loads(row.payload_json)
            grouped: dict[str, list[str]] = {}
            for field in ("characters", "abilities", "items", "locations", "concepts"):
                values = [
                    str(item.get("name"))
                    for item in (payload.get(field) or [])
                    if isinstance(item, Mapping) and item.get("name")
                ]
                if values:
                    grouped[field] = values
            evidence.append(
                PageEvidence(
                    global_page=global_page,
                    chapter_title=str(row.chapter_title),
                    chapter_ordinal=int(row.chapter_ordinal),
                    page_ordinal=int(row.page_ordinal),
                    page_id=str(row.page_id_snapshot),
                    page_summary=str(payload.get("page_summary") or ""),
                    key_events=[
                        dict(item) for item in (payload.get("key_events") or []) if isinstance(item, Mapping)
                    ],
                    characters=[str(item) for item in grouped.get("characters", [])],
                    other_entities={key: value for key, value in grouped.items() if key != "characters"},
                    asset_id=str(row.asset_id),
                    asset_path=self.data_root / str(row.relative_path),
                )
            )
        return evidence

    def image_bytes(self, evidence: PageEvidence) -> bytes:
        if evidence.asset_path.is_file():
            return evidence.asset_path.read_bytes()
        prefix = evidence.asset_id[:2]
        for candidate in sorted((self.data_root / "objects" / prefix).glob(f"{evidence.asset_id}.*")):
            return candidate.read_bytes()
        raise InsightConflict(f"页面原图缺失：{evidence.citation()}")

    # ---- 写入 ----------------------------------------------------------

    def save_skill(
        self,
        *,
        book_id: str,
        entity_id: str,
        run_id: str,
        fingerprint: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = utcnow()
        with self.engine.begin() as connection:
            revision = int(
                connection.execute(
                    select(func.coalesce(func.max(analysis_artifacts.c.revision), 0) + 1).where(
                        analysis_artifacts.c.book_id == book_id,
                        analysis_artifacts.c.kind == CHARACTER_SKILL_KIND,
                        analysis_artifacts.c.template == entity_id,
                    )
                ).scalar_one()
            )
            connection.execute(
                update(analysis_artifacts)
                .where(
                    analysis_artifacts.c.book_id == book_id,
                    analysis_artifacts.c.kind == CHARACTER_SKILL_KIND,
                    analysis_artifacts.c.template == entity_id,
                    analysis_artifacts.c.is_active.is_(True),
                )
                .values(is_active=False, updated_at=now)
            )
            artifact_id = str(uuid.uuid4())
            connection.execute(
                insert(analysis_artifacts).values(
                    id=artifact_id,
                    book_id=book_id,
                    run_id=run_id,
                    kind=CHARACTER_SKILL_KIND,
                    template=entity_id,
                    status="ready",
                    revision=revision,
                    is_active=True,
                    dependency_fingerprint=fingerprint,
                    payload_json=_json(dict(payload)),
                    asset_id=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        return {"artifactId": artifact_id, "revision": revision, "updatedAt": now.isoformat()}


class CharacterSkillBuilder:
    """把一个角色蒸馏成 Skill：事实层来自页面分析，人格层来自原画观察。"""

    def __init__(
        self,
        repository: CharacterSkillRepository,
        *,
        vlm_config: Any,
        chat_config: Any,
    ) -> None:
        self.repository = repository
        self.vlm_config = vlm_config
        self.chat_config = chat_config

    # ---- 公共入口 ------------------------------------------------------

    def build(
        self,
        *,
        book_id: str,
        entity_id: str,
        max_vision_pages: int = DEFAULT_MAX_VISION_PAGES,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, Any]:
        return asyncio.run(
            self.build_async(
                book_id=book_id,
                entity_id=entity_id,
                max_vision_pages=max_vision_pages,
                progress=progress,
            )
        )

    async def build_async(
        self,
        *,
        book_id: str,
        entity_id: str,
        max_vision_pages: int = DEFAULT_MAX_VISION_PAGES,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(max_vision_pages or DEFAULT_MAX_VISION_PAGES), MAX_VISION_PAGES))
        report = progress or (lambda stage, done, total: None)

        report("evidence", 0, 3)
        with self.repository.engine.connect() as connection:
            title = self.repository.book_title(connection, book_id)
            alignment = self.repository.alignment(connection, book_id)
            run_id = self.repository.latest_run_id(connection, book_id)

        entity = _find_character(alignment, entity_id)
        alias_map = _alias_map(alignment)
        evidence = self.repository.page_evidence(
            book_id=book_id,
            run_id=run_id,
            page_numbers=[int(number) for number in entity.get("pages") or []],
        )
        if not evidence:
            raise InsightConflict("该角色在已发布的页面分析里没有可解析的出场页")

        facts = _aggregate(entity, evidence, alias_map)
        report("evidence", 1, 3)

        selected = _select_vision_pages(evidence, facts, limit)
        observations = await self._observe(
            book_title=title,
            character=str(entity.get("canonical_name") or ""),
            aliases=[str(item) for item in entity.get("aliases") or []],
            pages=selected,
            progress=lambda done, total: report("vision", done, total),
        )
        report("vision", len(selected), len(selected) or 1)
        report("synthesis", 0, 1)

        prompt, used_vision = _synth_prompt(
            book_title=title,
            character=entity,
            facts=facts,
            observations=observations,
        )
        raw = await self._chat_json(prompt)
        invalid: Counter[str] = Counter()
        allowed = {(page.chapter_title, page.page_ordinal, page.global_page) for page in evidence}
        content = _sanitize(raw, allowed, invalid)
        report("synthesis", 1, 1)

        payload = _build_payload(
            book_id=book_id,
            book_title=title,
            character=entity,
            run_id=run_id,
            facts=facts,
            evidence=evidence,
            observations=used_vision,
            content=content,
            dropped=sum(invalid.values()),
            vlm_model=str(getattr(self.vlm_config, "model", "") or ""),
            chat_model=str(getattr(self.chat_config, "model", "") or ""),
        )
        fingerprint = _fingerprint(book_id=book_id, entity_id=entity_id, run_id=run_id, pages=evidence)
        stored = self.repository.save_skill(
            book_id=book_id,
            entity_id=entity_id,
            run_id=run_id,
            fingerprint=fingerprint,
            payload=payload,
        )
        payload.update(stored)
        return payload

    # ---- 模型调用 ------------------------------------------------------

    async def _observe(
        self,
        *,
        book_title: str,
        character: str,
        aliases: Sequence[str],
        pages: Sequence[PageEvidence],
        progress: Callable[[int, int], None],
    ) -> list[dict[str, Any]]:
        from src.core.manga_insight.vlm_client import VLMClient

        if not pages:
            return []
        client = VLMClient(self.vlm_config)
        observations: list[dict[str, Any]] = []
        total = (len(pages) + VISION_BATCH_SIZE - 1) // VISION_BATCH_SIZE
        for index, start in enumerate(range(0, len(pages), VISION_BATCH_SIZE), start=1):
            chunk = list(pages[start : start + VISION_BATCH_SIZE])
            labels = "\n".join(
                f"第 {position + 1} 张 = 全书第 {page.global_page} 页（{page.chapter_title} 第 {page.page_ordinal} 页）"
                for position, page in enumerate(chunk)
            )
            prompt = VISION_PROMPT.format(
                count=len(chunk),
                book_title=book_title,
                labels=labels,
                character=character,
                aliases="、".join(aliases[:6]) or "无",
            )
            images = [self.repository.image_bytes(page) for page in chunk]
            result = await client.analyze_batch(
                images,
                [page.global_page for page in chunk],
                prompt,
                validator=lambda parsed: parsed,
            )
            by_page = {int(page.global_page): page for page in chunk}
            for observed in result.get("pages") or []:
                number = int(observed.get("page_number") or 0)
                page = by_page.get(number)
                if page is None:
                    continue
                observations.append(
                    {
                        "global_page": number,
                        "citation": page.citation(),
                        "observation": dict(observed),
                    }
                )
            progress(index, total)
        observations.sort(key=lambda item: int(item["global_page"]))
        return observations

    async def _chat_json(self, prompt: str) -> dict[str, Any]:
        from src.core.manga_insight.embedding_client import ChatClient

        client = ChatClient(self.chat_config)
        content = await client.generate_json(
            prompt,
            system=SYNTH_SYSTEM,
            validator=lambda parsed: parsed,
        )
        if not isinstance(content, Mapping):
            raise InsightConflict("角色档案需要 JSON 对象，模型返回了其它结构")
        return dict(content)


# ---- 纯函数 ------------------------------------------------------------


def _find_character(alignment: Mapping[str, Any], entity_id: str) -> dict[str, Any]:
    for entity in alignment.get("canonical_entities") or []:
        if str(entity.get("entity_id") or "") == entity_id and entity.get("category") == CHARACTER_CATEGORY:
            return dict(entity)
    raise InsightNotFound("角色不存在")


def _alias_map(alignment: Mapping[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for entity in alignment.get("canonical_entities") or []:
        canonical = str(entity.get("canonical_name") or "")
        if not canonical:
            continue
        mapping[canonical] = canonical
        for alias in entity.get("aliases") or []:
            text = str(alias or "").strip()
            if text:
                mapping.setdefault(text, canonical)
    return mapping


@dataclass(frozen=True)
class _Facts:
    chapter_distribution: dict[str, int]
    co_occurrence: list[tuple[str, int]]
    concepts: list[tuple[str, int]]
    events: list[dict[str, Any]]
    solo_pages: list[int]
    importance: dict[str, int]


def _aggregate(
    entity: Mapping[str, Any],
    evidence: Sequence[PageEvidence],
    alias_map: Mapping[str, str],
) -> _Facts:
    own = {str(entity.get("canonical_name") or "")}
    own.update(str(alias) for alias in entity.get("aliases") or [])

    chapter_pages: Counter[str] = Counter()
    co_pages: dict[str, set[int]] = defaultdict(set)
    co_counts: Counter[str] = Counter()
    concepts: Counter[str] = Counter()
    importance: Counter[str] = Counter()
    events: list[dict[str, Any]] = []
    solo: list[int] = []

    for page in evidence:
        chapter_pages[page.chapter_title] += 1
        others = {alias_map.get(name, name) for name in page.characters if name not in own}
        others.discard("")
        if others:
            for name in others:
                co_pages[name].add(page.global_page)
                co_counts[name] += 1
        else:
            solo.append(page.global_page)
        for field in ("abilities", "concepts", "items", "locations"):
            for name in page.other_entities.get(field, []):
                concepts[f"{name}"] += 1
        for event in page.key_events:
            level = str(event.get("importance") or "normal")
            importance[level] += 1
            events.append(
                {
                    "citation": page.citation(),
                    "global_page": page.global_page,
                    "chapter_title": page.chapter_title,
                    "page_ordinal": page.page_ordinal,
                    "summary": str(event.get("summary") or ""),
                    "importance": level,
                }
            )
    return _Facts(
        chapter_distribution=dict(chapter_pages.most_common()),
        co_occurrence=[(name, len(co_pages[name])) for name, _ in co_counts.most_common(20)],
        concepts=concepts.most_common(40),
        events=events,
        solo_pages=solo,
        importance=dict(importance),
    )


def _select_vision_pages(
    evidence: Sequence[PageEvidence],
    facts: _Facts,
    limit: int,
) -> list[PageEvidence]:
    """每话取一张“只有该角色”的页面（靠近本章中段），再补 high 事件页。"""
    by_global = {page.global_page: page for page in evidence}
    solo = set(facts.solo_pages)
    by_chapter: dict[str, list[PageEvidence]] = defaultdict(list)
    for page in evidence:
        by_chapter[page.chapter_title].append(page)

    chosen: dict[int, str] = {}
    for title in sorted(by_chapter, key=lambda name: narrative_key(name, 0)):
        chapter_pages = sorted(by_chapter[title], key=lambda page: page.page_ordinal)
        solo_pages = [page for page in chapter_pages if page.global_page in solo]
        candidates = solo_pages or chapter_pages
        reason = "chapter-solo-shot" if solo_pages else "chapter-representative"
        middle = chapter_pages[len(chapter_pages) // 2]
        pick = min(candidates, key=lambda page: abs(page.page_ordinal - middle.page_ordinal))
        chosen.setdefault(pick.global_page, reason)

    high_pages = sorted(
        {
            int(event["global_page"])
            for event in facts.events
            if event.get("importance") == "high" and int(event.get("global_page") or 0) in by_global
        }
    )
    remaining = max(1, limit - len(chosen))
    if high_pages:
        step = max(1, len(high_pages) // remaining)
        for number in high_pages[::step]:
            if len(chosen) >= limit:
                break
            chosen.setdefault(number, "high-importance-event")

    return [by_global[number] for number in sorted(chosen)][:limit]


def _describe_aliases(aliases: Sequence[str], canonical: str) -> str:
    """把 OCR 变体和真正的称呼分开，避免档案里堆一屏错字。"""
    variants = [
        str(alias)
        for alias in aliases
        if alias and str(alias) != canonical and len(str(alias)) > 4
    ]
    short = [
        str(alias)
        for alias in aliases
        if alias and str(alias) != canonical and len(str(alias)) <= 4
    ]
    parts: list[str] = []
    if short:
        parts.append("、".join(short[:8]))
    if variants:
        sample = "、".join(variants[:3])
        parts.append(f"另有 {len(variants)} 个错字/OCR 变体（{sample} 等）")
    return "；".join(parts) or "无"


def _event_lines(facts: _Facts) -> str:
    ordered = sorted(
        facts.events,
        key=lambda item: narrative_key(str(item.get("chapter_title")), int(item["page_ordinal"])),
    )
    buckets = {
        "high": [item for item in ordered if item["importance"] == "high"],
        "medium": [item for item in ordered if item["importance"] == "medium"],
        "normal": [item for item in ordered if item["importance"] == "normal"],
    }
    picked: list[dict[str, Any]] = []
    for level, quota in (("high", 90), ("medium", 50), ("normal", 10)):
        bucket = buckets[level]
        if not bucket:
            continue
        step = max(1, len(bucket) // quota)
        picked.extend(bucket[::step][:quota])
    picked.sort(key=lambda item: narrative_key(str(item.get("chapter_title")), int(item["page_ordinal"])))
    prefixes = {"high": "[重要]", "medium": "[剧情]", "normal": "[日常]"}
    return "\n".join(
        f"- {item['citation']} {prefixes.get(item['importance'], '[日常]')} {item['summary']}"
        for item in picked[:MAX_EVENT_LINES]
    )


def _vision_lines(observations: Sequence[Mapping[str, Any]]) -> tuple[str, list[Mapping[str, Any]]]:
    lines: list[str] = []
    used: list[Mapping[str, Any]] = []
    for entry in observations:
        observation = entry.get("observation") or {}
        characters = [item for item in observation.get("characters") or [] if isinstance(item, Mapping)]
        target = [item for item in characters if item.get("is_target")]
        if not target and len(characters) == 1:
            target = characters
        if not target:
            continue
        used.append(entry)
        for person in target:
            lines.append(f"- {entry['citation']}（对应画面）")
            for key, label in (
                ("hair", "发型发色"),
                ("eyes", "眼睛"),
                ("build_age", "体型年龄感"),
                ("face", "脸型"),
                ("outfit", "服装"),
                ("expression", "表情"),
                ("posture_habit", "动作习惯"),
                ("visual_notes", "标志性元素"),
            ):
                value = str(person.get(key) or "").strip()
                if value:
                    lines.append(f"  · {label}：{value}")
            palette = [str(item) for item in person.get("color_palette") or [] if str(item).strip()]
            if palette:
                lines.append(f"  · 配色：{'、'.join(palette)}")
            for panel in person.get("panel_text") or []:
                if not isinstance(panel, Mapping):
                    continue
                text = str(panel.get("text") or "").strip()
                if text:
                    speaker = str(panel.get("speaker") or "未知")
                    lines.append(f'  · 画面文字（说话人：{speaker}）：「{text}」')
    return "\n".join(lines), used


def _synth_prompt(
    *,
    book_title: str,
    character: Mapping[str, Any],
    facts: _Facts,
    observations: Sequence[Mapping[str, Any]],
) -> tuple[str, list[Mapping[str, Any]]]:
    name = str(character.get("canonical_name") or "")
    aliases = [str(item) for item in character.get("aliases") or []]
    meta = (
        f"- 姓名：{name}\n"
        f"- 别名：{_describe_aliases(aliases, name)}\n"
        f"- 实体 ID：{character.get('entity_id')}\n"
        f"- 出场页数：{len(character.get('pages') or [])}\n"
        f"- 事件总数：{len(facts.events)}，其中 high={facts.importance.get('high', 0)}、"
        f"medium={facts.importance.get('medium', 0)}"
    )
    chapters = "\n".join(
        f"- {title}：{count} 页" for title, count in facts.chapter_distribution.items()
    )
    cooccurrence = "\n".join(f"- {name_}：{pages_} 页" for name_, pages_ in facts.co_occurrence[:10]) or "- 无"
    concepts = "\n".join(f"- {name_}：{count} 次" for name_, count in facts.concepts[:25]) or "- 无"
    vision_text, used = _vision_lines(observations)
    prompt = SYNTH_PROMPT.format(
        book_title=book_title,
        character=name,
        meta=meta,
        chapters=chapters,
        cooccurrence=cooccurrence,
        concepts=concepts,
        events=_event_lines(facts),
        vision=vision_text or "（本次没有可用的画面观察记录）",
    )
    return prompt, used


def _validate_citation(text: str, allowed: set[tuple[str, int, int]]) -> bool:
    match = CITATION_RE.search(text or "")
    if match is None:
        return False
    return (match.group("chapter"), int(match.group("page")), int(match.group("global"))) in allowed


def _sanitize(value: Any, allowed: set[tuple[str, int, int]], invalid: Counter[str]) -> Any:
    """丢弃引用无法回落到知识库的条目，其余原样保留。"""
    if isinstance(value, list):
        kept: list[Any] = []
        for item in value:
            if isinstance(item, Mapping):
                citation = str(item.get("citation") or item.get("first_appearance") or "")
                if citation and not _validate_citation(citation, allowed):
                    invalid[citation] += 1
                    continue
                kept.append(_sanitize(dict(item), allowed, invalid))
            else:
                kept.append(item)
        return kept
    if isinstance(value, Mapping):
        return {key: _sanitize(item, allowed, invalid) for key, item in value.items()}
    return value


def _render_files(
    *,
    book_title: str,
    character: Mapping[str, Any],
    facts: _Facts,
    evidence: Sequence[PageEvidence],
    content: Mapping[str, Any],
    used_vision: Sequence[Mapping[str, Any]],
    vlm_model: str,
) -> dict[str, str]:
    name = str(character.get("canonical_name") or "")
    aliases = [str(item) for item in character.get("aliases") or []]
    identity = content.get("identity") or {}
    one_liner = str(content.get("one_liner") or "")

    skill_md = "\n".join(
        [
            "---",
            f"name: {_slug(str(character.get('entity_id') or name))}",
            f'description: 漫画《{book_title}》角色「{name}」的档案与扮演资料，全部结论来自本地知识库'
            f"（页面级分析）与原画页面观察，带页码引用。Use when writing as, role-playing, or answering "
            f"questions about {name} ({character.get('entity_id')}).",
            "---",
            "",
            f"# {name}",
            "",
            one_liner,
            "",
            "## 怎么用这份资料",
            "",
            "- 要**扮演**这个角色：先读 `references/persona.md`，用里面的扮演提示与「说话方式」。",
            "- 要**回答事实问题**：查 `references/profile.md` 与 `references/timeline.md`，每条结论都带"
            " `《第X话》第Y页（全书第Z页）` 形式的引用。",
            "- 要**核对引用**：`references/citations.json` 里有每一条引用的页码、页 ID 与画面资产 ID。",
            "- 资料边界见 `references/limits.md`：没有证据的设定一律标注，不要自行补全。",
            "",
            "## 资料规模",
            "",
            f"- 出场：{len(character.get('pages') or [])} 页 / {len(facts.chapter_distribution)} 个章节单元",
            f"- 事件：{len(facts.events)} 条（high {facts.importance.get('high', 0)} 条）",
            f"- 画面观察：{len(used_vision)} 页原画（模型：{vlm_model or 'unknown'}）",
            "",
        ]
    )

    profile = [
        f"# {name} · 档案",
        "",
        f"- 全名：{identity.get('full_name') or name}",
        f"- 别名：{_describe_aliases(identity.get('aliases') or aliases, name)}",
        f"- 故事中的位置：{identity.get('role_in_story') or ''}",
        f"- 年龄感：{identity.get('age_impression') or ''}",
        f"- 首次出场：{identity.get('first_appearance') or ''}",
        "",
        "## 外貌与服装",
        "",
    ]
    for item in content.get("appearance") or []:
        if isinstance(item, Mapping):
            profile.append(
                f"- {item.get('trait')}（{item.get('citation')}，可信度 {item.get('confidence')}）"
            )
    profile += ["", "## 关系", ""]
    for item in content.get("relationships") or []:
        if isinstance(item, Mapping):
            profile.append(
                f"- **{item.get('name')}**：{item.get('relation')}。{item.get('dynamic')}"
                f"（{item.get('citation')}）"
            )
    profile += ["", "## 关联的能力 / 话题 / 道具", ""]
    for item in content.get("topics") or []:
        if isinstance(item, Mapping):
            profile.append(f"- {item.get('name')}：{item.get('note')}（{item.get('citation')}）")

    persona = [
        f"# {name} · 人格与口吻",
        "",
        "## 一句话人格",
        "",
        one_liner,
        "",
        "## 可直接使用的扮演提示",
        "",
        str(content.get("persona_prompt") or ""),
        "",
        "## 性格特征",
        "",
    ]
    for item in content.get("personality") or []:
        if isinstance(item, Mapping):
            persona.append(
                f"- **{item.get('trait')}**：{item.get('evidence')}"
                f"（{item.get('citation')}，可信度 {item.get('confidence')}）"
            )
    persona += ["", "## 说话方式", ""]
    for item in content.get("speech_style") or []:
        if not isinstance(item, Mapping):
            continue
        example = str(item.get("example") or "")
        suffix = f"；例：「{example}」" if example else ""
        persona.append(f"- {item.get('pattern')}{suffix}（{item.get('citation')}）")

    timeline = [
        f"# {name} · 行为年表",
        "",
        "按故事时间顺序排列，引用格式为《第X话》第Y页（全书第Z页）。",
        "",
    ]
    for item in content.get("timeline") or []:
        if not isinstance(item, Mapping):
            continue
        marker = {"high": "**[重要]** ", "medium": "", "normal": "[日常] "}.get(
            str(item.get("importance")), ""
        )
        timeline.append(f"- {item.get('citation')} {marker}{item.get('event')}")

    limits = [
        f"# {name} · 资料边界",
        "",
        "以下信息在本地知识库中没有可靠证据，回答时应明确说「资料不足」：",
        "",
    ]
    unknowns = [str(item) for item in content.get("unknowns") or [] if str(item).strip()]
    limits.extend(f"- {item}" for item in unknowns or ["（模型未列出，仍需按「无证据不说」处理）"])
    limits += [
        "",
        "## 证据类型说明",
        "",
        "- 事实类结论来自页面级多模态分析（page_summary / key_events）。",
        "- 外貌与口吻来自原画页面观察，属画面直读，不等同于作者设定。",
        "- 画面文字是画面内可见文字，可能与原文有翻译差异。",
        f"- 引用里的「全书第Z页」是本电子书的全局页序，共 {len(evidence)} 页已解析。",
        "",
    ]

    citations = {
        "bookTitle": book_title,
        "character": {
            "entityId": character.get("entity_id"),
            "name": name,
            "aliases": aliases,
        },
        "visionPages": [
            {"citation": entry.get("citation"), "globalPage": entry.get("global_page")}
            for entry in used_vision
        ],
        "pageIndex": [
            {
                "citation": page.citation(),
                "chapterTitle": page.chapter_title,
                "pageOrdinal": page.page_ordinal,
                "globalPage": page.global_page,
                "pageId": page.page_id,
                "assetId": page.asset_id,
            }
            for page in evidence
        ],
    }

    return {
        "SKILL.md": skill_md,
        "references/profile.md": "\n".join(profile) + "\n",
        "references/persona.md": "\n".join(persona) + "\n",
        "references/timeline.md": "\n".join(timeline) + "\n",
        "references/limits.md": "\n".join(limits) + "\n",
        "references/citations.json": json.dumps(citations, ensure_ascii=False, indent=2),
    }


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^0-9a-zA-Z_-]+", "-", name).strip("-").lower()
    return cleaned or "character"


def _build_payload(
    *,
    book_id: str,
    book_title: str,
    character: Mapping[str, Any],
    run_id: str,
    facts: _Facts,
    evidence: Sequence[PageEvidence],
    observations: Sequence[Mapping[str, Any]],
    content: Mapping[str, Any],
    dropped: int,
    vlm_model: str,
    chat_model: str,
) -> dict[str, Any]:
    _, used = _vision_lines(observations)
    files = _render_files(
        book_title=book_title,
        character=character,
        facts=facts,
        evidence=evidence,
        content=content,
        used_vision=used,
        vlm_model=vlm_model,
    )
    return {
        "bookId": book_id,
        "bookTitle": book_title,
        "entityId": str(character.get("entity_id") or ""),
        "characterName": str(character.get("canonical_name") or ""),
        "runId": run_id,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "models": {"vlm": vlm_model, "chat": chat_model},
        "stats": {
            "pages": len(evidence),
            "chapters": len(facts.chapter_distribution),
            "events": len(facts.events),
            "visionPages": len(used),
            "droppedCitations": dropped,
        },
        "summary": content,
        "files": files,
    }


def _fingerprint(*, book_id: str, entity_id: str, run_id: str, pages: Sequence[PageEvidence]) -> str:
    digest = hashlib.sha256()
    digest.update(book_id.encode("utf-8"))
    digest.update(entity_id.encode("utf-8"))
    digest.update(run_id.encode("utf-8"))
    for page in pages:
        digest.update(f"{page.global_page}:{page.page_id}".encode("utf-8"))
    return digest.hexdigest()[:32]


def _task_key(book_id: str, entity_id: str) -> str:
    return f"{book_id}:{entity_id}"


def _page_number(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class CharacterSkillService:
    """API 门面：解析供应商配置、在后台线程执行蒸馏、对外暴露进度。"""

    def __init__(self, engine: Engine, data_root: Path) -> None:
        self.engine = engine
        self.data_root = Path(data_root)
        self.repository = CharacterSkillRepository(engine, self.data_root)
        self._tasks: dict[str, dict[str, Any]] = {}
        self._guard = threading.Lock()

    # ---- 查询 ----------------------------------------------------------

    def characters(self, *, book_id: str) -> list[dict[str, Any]]:
        return self.repository.characters(book_id=book_id)

    def get(self, *, book_id: str, entity_id: str) -> dict[str, Any]:
        return self.repository.get_skill(book_id=book_id, entity_id=entity_id)

    def status(self, *, book_id: str, entity_id: str) -> dict[str, Any]:
        key = _task_key(book_id, entity_id)
        with self._guard:
            task = self._tasks.get(key)
            if task is not None:
                return dict(task)
        try:
            skill = self.repository.get_skill(book_id=book_id, entity_id=entity_id)
        except InsightNotFound:
            return {
                "bookId": book_id,
                "entityId": entity_id,
                "status": "idle",
                "stage": None,
                "done": 0,
                "total": 0,
            }
        return {
            "bookId": book_id,
            "entityId": entity_id,
            "status": "ready",
            "stage": "done",
            "done": 1,
            "total": 1,
            "revision": skill.get("revision"),
            "updatedAt": skill.get("updatedAt"),
        }

    # ---- 触发 ----------------------------------------------------------

    def start(
        self,
        *,
        book_id: str,
        entity_id: str,
        max_vision_pages: int = DEFAULT_MAX_VISION_PAGES,
    ) -> dict[str, Any]:
        key = _task_key(book_id, entity_id)
        with self._guard:
            existing = self._tasks.get(key)
            if existing is not None and existing.get("status") == "running":
                return dict(existing)
            known = {item["entityId"] for item in self.repository.characters(book_id=book_id)}
            if entity_id not in known:
                raise InsightNotFound("角色不存在")
            vlm_config, chat_config = self._runtime_configs(book_id)
            task = {
                "bookId": book_id,
                "entityId": entity_id,
                "status": "running",
                "stage": "queued",
                "done": 0,
                "total": 3,
                "startedAt": utcnow().isoformat(timespec="seconds"),
                "error": None,
            }
            self._tasks[key] = task
        limit = max(1, min(int(max_vision_pages or DEFAULT_MAX_VISION_PAGES), MAX_VISION_PAGES))
        threading.Thread(
            target=self._run,
            args=(key, book_id, entity_id, limit, vlm_config, chat_config),
            name="character-skill",
            daemon=True,
        ).start()
        return dict(task)

    # ---- 内部 ----------------------------------------------------------

    def _run(
        self,
        key: str,
        book_id: str,
        entity_id: str,
        limit: int,
        vlm_config: Any,
        chat_config: Any,
    ) -> None:
        def progress(stage: str, done: int, total: int) -> None:
            with self._guard:
                task = self._tasks.get(key)
                if task is not None and task.get("status") == "running":
                    task.update({"stage": stage, "done": int(done), "total": max(1, int(total))})

        try:
            builder = CharacterSkillBuilder(
                self.repository,
                vlm_config=vlm_config,
                chat_config=chat_config,
            )
            payload = builder.build(
                book_id=book_id,
                entity_id=entity_id,
                max_vision_pages=limit,
                progress=progress,
            )
        except Exception as error:  # noqa: BLE001 - 结果通过任务状态回传
            LOGGER.warning(
                "character skill build failed for %s/%s: %s",
                book_id,
                entity_id,
                error,
                exc_info=True,
            )
            with self._guard:
                self._tasks[key] = {
                    "bookId": book_id,
                    "entityId": entity_id,
                    "status": "failed",
                    "stage": "failed",
                    "done": 0,
                    "total": 0,
                    "error": str(error) or error.__class__.__name__,
                    "finishedAt": utcnow().isoformat(timespec="seconds"),
                }
            return
        with self._guard:
            self._tasks[key] = {
                "bookId": book_id,
                "entityId": entity_id,
                "status": "ready",
                "stage": "done",
                "done": 1,
                "total": 1,
                "revision": payload.get("revision"),
                "updatedAt": payload.get("updatedAt"),
                "finishedAt": utcnow().isoformat(timespec="seconds"),
                "error": None,
            }

    def _runtime_configs(self, book_id: str) -> tuple[Any, Any]:
        config = SettingsResolver(self.engine).resolve_insight(book_id=book_id, scope="full")
        resolved = SettingsRepository(self.engine).resolve_credential_sections(
            config, ["vlm", "chat"]
        )
        return frozen_vlm_config(resolved), frozen_chat_config(resolved)
