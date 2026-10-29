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
import io
import json
import logging
from pathlib import Path
import re
import threading
from typing import Any
import uuid
import zipfile

from sqlalchemy import Engine, func, insert, select, update
from sqlalchemy.engine import Connection

from src.backend_v2.insight.errors import InsightPrecondition
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

# 一个实体可以蒸馏出三种产物（都以 analysis_artifacts 落库，kind 区分）。
CHARACTER_CARD_KIND = "character_card"
LOREBOOK_KIND = "lorebook"
DISTILL_KINDS: dict[str, str] = {
    "skill": CHARACTER_SKILL_KIND,
    "card": CHARACTER_CARD_KIND,
}
DISTILL_KIND_LABELS: dict[str, str] = {
    "skill": "角色档案 Skill",
    "card": "角色卡 Character Card",
    "lorebook": "世界书 Lorebook",
}
KIND_LABEL_KEYS: dict[str, str] = {value: key for key, value in DISTILL_KINDS.items()}
KIND_LABEL_KEYS[LOREBOOK_KIND] = "lorebook"


def _label_key(kind: str) -> str:
    return KIND_LABEL_KEYS.get(kind, kind)
CARD_MIN_VISION_PAGES = 4
CARD_MAX_VISION_PAGES = 16
DEFAULT_LOREBOOK_ENTITIES = 60
MAX_LOREBOOK_ENTITIES = 200
LOREBOOK_PREVIEW_PAGES = 3
LOREBOOK_ENTITY_ID = "default"
CATEGORY_LABELS: dict[str, str] = {
    "characters": "角色",
    "locations": "地点",
    "items": "物品",
    "concepts": "概念",
    "abilities": "能力",
}

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
            select(analysis_artifacts.c.payload_json, analysis_artifacts.c.status).where(
                analysis_artifacts.c.book_id == book_id,
                analysis_artifacts.c.kind == "entity_alignment",
                analysis_artifacts.c.is_active.is_(True),
            )
        ).first()
        if row is None or not row[0]:
            raise InsightPrecondition("该书还没有实体知识网络，请先完成一次知识库构建")
        # stale 表示「产物是旧一轮分析的」，把它当正常证据会把过期实体写进
        # 角色档案；ready / degraded 仍可用（degraded 只是覆盖不全）。
        if str(row[1]) == "stale":
            raise InsightPrecondition(
                "实体知识网络已过期（书籍重新分析过），请先重新生成实体知识网络"
            )
        return json.loads(row[0])

    def characters(self, *, book_id: str, min_pages: int = 0) -> list[dict[str, Any]]:
        """角色列表（带是否已生成 Skill 的标记）。

        `min_pages` 用来隐藏出场页数不足的角色：实体知识网络里大量角色只在一两页
        被顺带提到，全部列出来会让可选列表不可用。
        """
        with self.engine.connect() as connection:
            payload = self.alignment(connection, book_id)
            generated: dict[str, dict[str, dict[str, Any]]] = {}
            for row in connection.execute(
                select(
                    analysis_artifacts.c.template,
                    analysis_artifacts.c.kind,
                    analysis_artifacts.c.revision,
                    analysis_artifacts.c.updated_at,
                ).where(
                    analysis_artifacts.c.book_id == book_id,
                    analysis_artifacts.c.kind.in_(tuple(DISTILL_KINDS.values())),
                    analysis_artifacts.c.is_active.is_(True),
                )
            ).all():
                generated.setdefault(str(row.template), {})[str(row.kind)] = {
                    "revision": int(row.revision),
                    "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
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
            if len(chapter_pages) < min_pages:
                continue
            artifacts = generated.get(entity_id) or {}
            skill = artifacts.get(CHARACTER_SKILL_KIND)
            card = artifacts.get(CHARACTER_CARD_KIND)
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
                    "hasCard": card is not None,
                    "cardRevision": card["revision"] if card else None,
                    "cardUpdatedAt": card["updatedAt"] if card else None,
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

    def get_skill(
        self,
        *,
        book_id: str,
        entity_id: str,
        kind: str = CHARACTER_SKILL_KIND,
    ) -> dict[str, Any]:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    analysis_artifacts.c.payload_json,
                    analysis_artifacts.c.revision,
                    analysis_artifacts.c.updated_at,
                    analysis_artifacts.c.status,
                ).where(
                    analysis_artifacts.c.book_id == book_id,
                    analysis_artifacts.c.kind == kind,
                    analysis_artifacts.c.template == entity_id,
                    analysis_artifacts.c.is_active.is_(True),
                )
            ).mappings().one_or_none()
        if row is None or not row["payload_json"]:
            raise InsightNotFound(f"该角色还没有生成{DISTILL_KIND_LABELS.get(_label_key(kind), kind)}")
        payload = json.loads(row["payload_json"])
        payload["revision"] = int(row["revision"])
        payload["updatedAt"] = row["updated_at"].isoformat() if row["updated_at"] else None
        payload["status"] = str(row["status"])
        return payload

    def latest_run_id(self, connection: Connection, book_id: str) -> str:
        """挑出「证据最多的那一轮已发布分析」。

        故意的判据：按已发布页数取最大，**不是**按 created_at 取最新。
        实测反例（FX战士久留美）：一轮 full 构建发布 1457 页，之后一轮
        incremental 只发布了 2 页——按时间取「最新」会把 1457 页的证据
        换成 2 页，蒸馏出来的角色档案直接失真。用 count 判据时那一轮
        2 页的增量永远不会被选成主证据源。

        这有意牺牲一个场景：某轮次的 book 被裁过页数后，旧轮次的 count
        可能仍然更大。当前 UI 不提供裁页，真要支持得改成「按 run 的
        target_count 判定」，不能简单换成时间排序。
        """
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
            raise InsightPrecondition("该书还没有已发布的页面分析结果")
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
        raise InsightPrecondition(f"页面原图缺失：{evidence.citation()}")

    # ---- 写入 ----------------------------------------------------------

    def save_skill(
        self,
        *,
        book_id: str,
        entity_id: str,
        run_id: str,
        fingerprint: str,
        payload: Mapping[str, Any],
        kind: str = CHARACTER_SKILL_KIND,
    ) -> dict[str, Any]:
        now = utcnow()
        with self.engine.begin() as connection:
            revision = int(
                connection.execute(
                    select(func.coalesce(func.max(analysis_artifacts.c.revision), 0) + 1).where(
                        analysis_artifacts.c.book_id == book_id,
                        analysis_artifacts.c.kind == kind,
                        analysis_artifacts.c.template == entity_id,
                    )
                ).scalar_one()
            )
            connection.execute(
                update(analysis_artifacts)
                .where(
                    analysis_artifacts.c.book_id == book_id,
                    analysis_artifacts.c.kind == kind,
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
                    kind=kind,
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
        kind: str = CHARACTER_SKILL_KIND,
    ) -> dict[str, Any]:
        return asyncio.run(
            self.build_async(
                book_id=book_id,
                entity_id=entity_id,
                max_vision_pages=max_vision_pages,
                progress=progress,
                kind=kind,
            )
        )

    async def build_async(
        self,
        *,
        book_id: str,
        entity_id: str,
        max_vision_pages: int = DEFAULT_MAX_VISION_PAGES,
        progress: Callable[[str, int, int], None] | None = None,
        kind: str = CHARACTER_SKILL_KIND,
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
            raise InsightPrecondition("该角色在已发布的页面分析里没有可解析的出场页")

        facts = _aggregate(entity, evidence, alias_map)
        report("evidence", 1, 3)

        if kind == CHARACTER_CARD_KIND:
            return await self._build_card(
                book_id=book_id,
                book_title=title,
                character=entity,
                evidence=evidence,
                facts=facts,
                run_id=run_id,
                limit=limit,
                report=report,
            )
        if kind != CHARACTER_SKILL_KIND:
            raise InsightPrecondition(f"不支持的蒸馏类型：{kind}")

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
            kind=kind,
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

    async def _chat_json(self, prompt: str, system: str = SYNTH_SYSTEM) -> dict[str, Any]:
        from src.core.manga_insight.embedding_client import ChatClient

        client = ChatClient(self.chat_config)
        content = await client.generate_json(
            prompt,
            system=system,
            validator=lambda parsed: parsed,
        )
        if not isinstance(content, Mapping):
            raise InsightConflict("角色档案需要 JSON 对象，模型返回了其它结构")
        return dict(content)

    # ---- 角色卡（SillyTavern V2/V3）----------------------------------

    async def _build_card(
        self,
        *,
        book_id: str,
        book_title: str,
        character: Mapping[str, Any],
        evidence: Sequence[PageEvidence],
        facts: _Facts,
        run_id: str,
        limit: int,
        report: Callable[[str, int, int], None],
    ) -> dict[str, Any]:
        card_limit = max(CARD_MIN_VISION_PAGES, min(int(limit or CARD_MIN_VISION_PAGES), CARD_MAX_VISION_PAGES))
        selected = _select_vision_pages(evidence, facts, card_limit)
        observations = await self._observe(
            book_title=book_title,
            character=str(character.get("canonical_name") or ""),
            aliases=[str(item) for item in character.get("aliases") or []],
            pages=selected,
            progress=lambda done, total: report("vision", done, total),
        )
        report("vision", len(selected), len(selected) or 1)
        report("synthesis", 0, 1)

        prompt, used_vision = _card_prompt(
            book_title=book_title,
            character=character,
            facts=facts,
            observations=observations,
        )
        raw = await self._chat_json(prompt, CARD_SYSTEM)
        invalid: Counter[str] = Counter()
        allowed = {(page.chapter_title, page.page_ordinal, page.global_page) for page in evidence}
        card = _sanitize(raw, allowed, invalid)
        report("synthesis", 1, 1)

        payload = _build_card_payload(
            book_id=book_id,
            book_title=book_title,
            character=character,
            run_id=run_id,
            facts=facts,
            evidence=evidence,
            observations=used_vision,
            card=card,
            dropped=sum(invalid.values()),
            vlm_model=str(getattr(self.vlm_config, "model", "") or ""),
            chat_model=str(getattr(self.chat_config, "model", "") or ""),
        )
        fingerprint = _fingerprint(
            book_id=book_id,
            entity_id=str(character.get("entity_id") or ""),
            run_id=run_id,
            pages=evidence,
        )
        stored = self.repository.save_skill(
            book_id=book_id,
            entity_id=str(character.get("entity_id") or ""),
            run_id=run_id,
            fingerprint=fingerprint,
            payload=payload,
            kind=CHARACTER_CARD_KIND,
        )
        payload.update(stored)
        return payload

    # ---- 世界书（SillyTavern World Info）-----------------------------

    def build_lorebook(
        self,
        *,
        book_id: str,
        max_entities: int = DEFAULT_LOREBOOK_ENTITIES,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, Any]:
        report = progress or (lambda stage, done, total: None)
        limit = max(1, min(int(max_entities or DEFAULT_LOREBOOK_ENTITIES), MAX_LOREBOOK_ENTITIES))

        with self.repository.engine.connect() as connection:
            title = self.repository.book_title(connection, book_id)
            alignment = self.repository.alignment(connection, book_id)
            run_id = self.repository.latest_run_id(connection, book_id)

        report("evidence", 0, 2)
        entities = [dict(item) for item in alignment.get("canonical_entities") or []]
        entities = [item for item in entities if item.get("entity_id") and item.get("pages")]
        entities.sort(key=lambda item: (-len(item.get("pages") or []), str(item.get("canonical_name") or "")))
        entities = entities[:limit]
        if not entities:
            raise InsightPrecondition("该书没有可用的实体知识，无法生成世界书")

        preview: dict[str, list[int]] = {}
        wanted: set[int] = set()
        for entity in entities:
            numbers = [_page_number(item) for item in entity.get("pages") or []]
            numbers = [number for number in dict.fromkeys(numbers) if number > 0]
            picks = _spread_picks(numbers, LOREBOOK_PREVIEW_PAGES)
            preview[str(entity.get("entity_id"))] = picks
            wanted.update(picks)
        report("evidence", 1, 2)

        evidence = self.repository.page_evidence(
            book_id=book_id,
            run_id=run_id,
            page_numbers=sorted(wanted),
        )
        by_page = {page.global_page: page for page in evidence}
        report("evidence", 2, 2)
        report("synthesis", 0, 1)

        entries = _lorebook_entries(
            book_title=title,
            entities=entities,
            preview=preview,
            by_page=by_page,
        )
        report("synthesis", 1, 1)

        payload = _build_lorebook_payload(
            book_id=book_id,
            book_title=title,
            run_id=run_id,
            entries=entries,
            entity_total=len(entities),
            page_total=len(evidence),
        )
        fingerprint = hashlib.sha256(
            f"{book_id}|{run_id}|{len(entries)}|{_entries_digest(entries)}".encode("utf-8")
        ).hexdigest()[:32]
        stored = self.repository.save_skill(
            book_id=book_id,
            entity_id="default",
            run_id=run_id,
            fingerprint=fingerprint,
            payload=payload,
            kind=LOREBOOK_KIND,
        )
        payload.update(stored)
        return payload


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


CARD_SYSTEM = (
    "你是漫画角色卡的整理员。你只根据给定的知识库证据填写 SillyTavern 角色卡字段，"
    "任何无法从证据推出的设定宁可不写；引用一律使用《第X话》第Y页（全书第Z页）的格式。"
    "只输出严格 JSON，不要代码块。"
)

CARD_PROMPT = """请为漫画《{book_title}》中的角色「{character}」填写一张 SillyTavern 角色卡。

## 角色元信息

{meta}

## 章节分布

{chapters}

## 同页共现的角色

{cooccurrence}

## 相关概念

{concepts}

## 关键事件（每条都带引用）

{events}

## 原画观察（外貌与口吻的直接证据）

{vision}

## 输出格式（严格 JSON）

{{"name": "角色名",
 "description": "外貌、身份、处境与性格的连续描述，尽量带引用",
 "personality": "性格关键词与行为习惯，一句话一段",
 "scenario": "对话开始的处境；只写知识库能支撑的内容",
 "first_mes": "以该角色口吻写的开场白（第一人称，不要旁白）",
 "mes_example": "<START>\\n{{{{user}}}}: ...\\n{{{{char}}}}: ...",
 "creator_notes": "这份卡片的证据来源与边界说明",
 "system_prompt": "扮演该角色时的系统提示",
 "post_history_instructions": "写作风格与禁忌",
 "alternate_greetings": ["另一句开场白"],
 "tags": ["标签"],
 "evidence": [{{"claim": "结论", "citation": "《第1话》第3页（全书第3页）"}}]}}

规则：
1. `mes_example` 里的台词只能逐字取自「原画观察」中记录的画面文字；没有就写成符合其说话方式的短句，并在 creator_notes 里注明。
2. `evidence` 至少 5 条，覆盖外貌、性格、关系；引用必须严格来自上面的证据。
3. 不要写书里没有的设定（年龄、生日、血型等）——拿不准就省略。
"""


def _nl_join(lines: Iterable[str]) -> str:
    return "\n".join(lines)


def _card_prompt(
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
        f"- 出场页数：{len(character.get('pages') or [])}\n"
        f"- 事件总数：{len(facts.events)}，其中 high={facts.importance.get('high', 0)}"
    )
    chapters = _nl_join(
        f"- {title}：{count} 页" for title, count in facts.chapter_distribution.items()
    )
    cooccurrence = (
        _nl_join(f"- {name_}: {pages_} 页" for name_, pages_ in facts.co_occurrence[:10])
        or "- 无"
    )
    concepts = (
        _nl_join(f"- {name_}: {count} 次" for name_, count in facts.concepts[:20]) or "- 无"
    )
    vision_text, used = _vision_lines(observations)
    prompt = CARD_PROMPT.format(
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


def _card_text(card: Mapping[str, Any], key: str) -> str:
    value = card.get(key)
    return value.strip() if isinstance(value, str) else ""


def _card_list(card: Mapping[str, Any], key: str) -> list[str]:
    value = card.get(key)
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _card_data(
    *,
    book_id: str,
    book_title: str,
    character: Mapping[str, Any],
    run_id: str,
    card: Mapping[str, Any],
    facts: _Facts,
    used_vision: Sequence[Mapping[str, Any]],
    models: Mapping[str, str],
) -> dict[str, Any]:
    """SillyTavern data 段（V2 与 V3 共用）。"""
    name = str(character.get("canonical_name") or "")
    evidence = [dict(item) for item in card.get("evidence") or [] if isinstance(item, Mapping)]
    appendix = _nl_join(
        f"- {item.get('claim')}（{item.get('citation')}）" for item in evidence
    )
    description = _card_text(card, "description") or f"《{book_title}》中的{name}。"
    if appendix:
        description = f"{description}\n\n证据：\n{appendix}"
    return {
        "name": _card_text(card, "name") or name,
        "description": description,
        "personality": _card_text(card, "personality"),
        "scenario": _card_text(card, "scenario"),
        "first_mes": _card_text(card, "first_mes"),
        "mes_example": _card_text(card, "mes_example"),
        "creator_notes": _card_text(card, "creator_notes"),
        "system_prompt": _card_text(card, "system_prompt"),
        "post_history_instructions": _card_text(card, "post_history_instructions"),
        "alternate_greetings": _card_list(card, "alternate_greetings"),
        "tags": _card_list(card, "tags"),
        "creator": "MiyakoKB",
        "character_version": f"run-{run_id[:8]}" if run_id else "1.0",
        "extensions": {
            "miyako": {
                "bookId": book_id,
                "bookTitle": book_title,
                "entityId": str(character.get("entity_id") or ""),
                "runId": run_id,
                "pages": len(character.get("pages") or []),
                "chapters": len(facts.chapter_distribution),
                "visionPages": len(used_vision),
                "models": dict(models),
            }
        },
    }


_CARD_V3_ONLY = ("group_only_greetings", "nickname", "creator_notes_multilingual", "source")


def _render_card_files(
    *,
    data: Mapping[str, Any],
    character: Mapping[str, Any],
    facts: _Facts,
    used_vision: Sequence[Mapping[str, Any]],
    evidence_pages: int,
) -> dict[str, str]:
    # 卡片里的台词来自台版原画，先统一转简体再落文件。
    data = _simplify_card_data(data)
    v3 = {"spec": "chara_card_v3", "spec_version": "3.0", "data": dict(data)}
    v2_data = {key: value for key, value in data.items() if key not in _CARD_V3_ONLY}
    v2 = {"spec": "chara_card_v2", "spec_version": "2.0", "data": v2_data}

    name = str(character.get("canonical_name") or "")
    preview = [
        f"# {name} · 角色卡",
        "",
        f"- 规格：SillyTavern Character Card V3（同目录另有 V2 版本）",
        f"- 出场：{len(character.get('pages') or [])} 页，其中 {evidence_pages} 页有分析证据"
        f" / {len(facts.chapter_distribution)} 个章节单元",
        f"- 原画观察：{len(used_vision)} 页",
        "",
        "## 描述",
        "",
        str(data.get("description") or ""),
        "",
        "## 性格",
        "",
        str(data.get("personality") or ""),
        "",
        "## 场景",
        "",
        str(data.get("scenario") or ""),
        "",
        "## 开场白",
        "",
        str(data.get("first_mes") or ""),
        "",
        "## 对话示例",
        "",
        "```",
        str(data.get("mes_example") or ""),
        "```",
        "",
        "## 扮演系统提示",
        "",
        str(data.get("system_prompt") or ""),
        "",
        "## 写作禁忌",
        "",
        str(data.get("post_history_instructions") or ""),
        "",
        "## 证据与边界",
        "",
        str(data.get("creator_notes") or ""),
        "",
    ]
    return {
        "character-card-v3.json": json.dumps(v3, ensure_ascii=False, indent=2),
        "character-card-v2.json": json.dumps(v2, ensure_ascii=False, indent=2),
        "character-card.md": "\n".join(preview),
    }


def _build_card_payload(
    *,
    book_id: str,
    book_title: str,
    character: Mapping[str, Any],
    run_id: str,
    facts: _Facts,
    evidence: Sequence[PageEvidence],
    observations: Sequence[Mapping[str, Any]],
    card: Mapping[str, Any],
    dropped: int,
    vlm_model: str,
    chat_model: str,
) -> dict[str, Any]:
    models = {"vlm": vlm_model, "chat": chat_model}
    data = _card_data(
        book_id=book_id,
        book_title=book_title,
        character=character,
        run_id=run_id,
        card=card,
        facts=facts,
        used_vision=observations,
        models=models,
    )
    files = _render_card_files(
        data=data,
        character=character,
        facts=facts,
        used_vision=observations,
        evidence_pages=len(evidence),
    )
    generated = utcnow()
    pages = len(character.get("pages") or [])
    return {
        "kind": CHARACTER_CARD_KIND,
        "bookId": book_id,
        "bookTitle": book_title,
        "entityId": str(character.get("entity_id") or ""),
        "characterName": str(character.get("canonical_name") or ""),
        "runId": run_id,
        "generatedAt": generated.isoformat(timespec="seconds"),
        "models": models,
        "stats": {
            "pages": pages,
            "chapters": len(facts.chapter_distribution),
            "visionPages": len(observations),
            "evidencePages": len(evidence),
            "droppedCitations": int(dropped),
        },
        "summary": dict(card),
        "card": {"spec": "chara_card_v3", "specVersion": "3.0", "data": data},
        "files": files,
        "status": "ready",
    }


def _spread_picks(numbers: Sequence[int], limit: int) -> list[int]:
    """从有序页码里均匀挑 limit 个（含首尾），用于世界书的剧情摘录。"""
    unique = list(dict.fromkeys(int(number) for number in numbers if int(number) > 0))
    if limit <= 0 or not unique:
        return []
    if len(unique) <= limit:
        return unique
    picks: list[int] = []
    for index in range(limit):
        position = round(index * (len(unique) - 1) / (limit - 1)) if limit > 1 else 0
        picks.append(unique[position])
    return list(dict.fromkeys(picks))


def _lorebook_entry(
    *,
    index: int,
    book_title: str,
    entity: Mapping[str, Any],
    preview_pages: Sequence[PageEvidence],
) -> dict[str, Any]:
    name = str(entity.get("canonical_name") or "").strip()
    category = str(entity.get("category") or "")
    label = CATEGORY_LABELS.get(category, category or "实体")
    aliases = [str(item).strip() for item in entity.get("aliases") or [] if str(item or "").strip()]
    pages = [int(number) for number in entity.get("pages") or [] if int(number or 0) > 0]
    lines = [f"《{book_title}》{label}：「{name}」。"]
    if pages:
        lines.append(f"出场 {len(pages)} 页（全书第 {min(pages)}–{max(pages)} 页）。")
    if aliases:
        lines.append(f"别名：{'、'.join(aliases[:8])}。")
    if preview_pages:
        lines.append("相关剧情：")
        for page in preview_pages:
            summary = (page.page_summary or "").strip().replace("\n", " ")
            if not summary:
                continue
            lines.append(f"- （{page.citation()}）{summary[:90]}")
    keys = [name] + [alias for alias in aliases if len(alias) <= 12 and alias != name]
    return {
        "uid": index,
        "key": keys[:12],
        "keysecondary": [],
        "comment": f"{label}：{name}",
        "content": "\n".join(lines),
        "constant": False,
        "vectorized": False,
        "selective": True,
        "selectiveLogic": 0,
        "addMemories": True,
        "addEmoji": True,
        "order": 100,
        "position": 0,
        "disable": False,
        "excludeRecursion": False,
        "preventRecursion": False,
        "delayUntilRecursion": False,
        "probability": 100,
        "useProbability": True,
        "depth": 4,
        "group": label,
        "groupOverride": False,
        "groupWeight": 100,
        "scanDepth": None,
        "caseSensitive": None,
        "matchWholeWords": None,
        "useGroupScoring": None,
        "automationId": "",
        "role": 0,
        "sticky": 0,
        "cooldown": 0,
        "delay": 0,
        "triggers": [],
        "displayIndex": index,
        "extensions": {
            "miyako": {
                "entityId": str(entity.get("entity_id") or ""),
                "category": category,
                "pages": len(pages),
            }
        },
    }


def _lorebook_entries(
    *,
    book_title: str,
    entities: Sequence[Mapping[str, Any]],
    preview: Mapping[str, Sequence[int]],
    by_page: Mapping[int, PageEvidence],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, entity in enumerate(entities):
        picks = [by_page[number] for number in preview.get(str(entity.get("entity_id")), []) if number in by_page]
        entries.append(
            _lorebook_entry(
                index=index,
                book_title=book_title,
                entity=entity,
                preview_pages=picks,
            )
        )
    return entries


def _render_lorebook_files(
    *,
    book_title: str,
    entries: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    world = {
        "name": f"{book_title} · 世界书",
        "description": "由 MiyakoKB 从实体知识网络与页面级分析生成；每条内容都带页码引用。",
        "entries": {str(entry["uid"]): dict(entry) for entry in entries},
        "extensions": {"miyako": {"generator": "MiyakoKB", "bookTitle": book_title}},
    }
    lines = [f"# {book_title} · 世界书", "", f"共 {len(entries)} 条条目。", ""]
    for entry in entries:
        lines.append(f"## {entry['comment']}")
        lines.append("")
        lines.append(f"- 触发关键词：{'、'.join(entry['key'])}")
        lines.append("")
        lines.append(str(entry["content"]))
        lines.append("")
    return {
        "lorebook.json": json.dumps(world, ensure_ascii=False, indent=2),
        "lorebook.md": "\n".join(lines),
    }


def _entries_digest(entries: Sequence[Mapping[str, Any]]) -> str:
    payload = [{"uid": entry.get("uid"), "key": entry.get("key"), "content": entry.get("content")} for entry in entries]
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _build_lorebook_payload(
    *,
    book_id: str,
    book_title: str,
    run_id: str,
    entries: Sequence[Mapping[str, Any]],
    entity_total: int,
    page_total: int,
) -> dict[str, Any]:
    files = _render_lorebook_files(book_title=book_title, entries=entries)
    generated = utcnow()
    by_category: Counter[str] = Counter()
    for entry in entries:
        by_category[str((entry.get("extensions") or {}).get("miyako", {}).get("category") or "unknown")] += 1
    return {
        "kind": LOREBOOK_KIND,
        "bookId": book_id,
        "bookTitle": book_title,
        "runId": run_id,
        "generatedAt": generated.isoformat(timespec="seconds"),
        "stats": {
            "entries": len(entries),
            "entities": entity_total,
            "previewPages": page_total,
            "byCategory": dict(by_category),
        },
        "summary": {
            "entryCount": len(entries),
            "categories": dict(by_category),
            "triggers": len({key for entry in entries for key in entry.get("key") or []}),
        },
        "files": files,
        "status": "ready",
    }

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
            f"- 出场：{len(character.get('pages') or [])} 页，其中 {len(evidence)} 页有分析证据"
            f" / {len(facts.chapter_distribution)} 个章节单元",
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


def _task_key(book_id: str, kind: str, entity_id: str) -> str:
    return f"{book_id}:{kind}:{entity_id}"


def _batch_key(book_id: str, kind: str) -> str:
    """批量蒸馏的键：一本书同一 kind 同时只允许一个批次在跑。"""
    return f"{book_id}:{kind}"


_UNSAFE_FILENAME_CHARS = frozenset('\\/:*?"<>|')


def _simplify_chinese(text: str) -> str:
    """把繁体文本转成简体。

    角色卡里的 ``first_mes`` / ``mes_example`` / ``alternate_greetings`` 是**逐字摘录**
    的原画台词，台版漫画正文是繁体，所以这几个字段会混进繁体字。用户要求卡片统一简体，
    因此在渲染时做一次 t2s；opencc 缺失时原样返回（不阻断导出）。
    """
    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    converter = _chinese_converter()
    if converter is None:
        return text
    try:
        return converter.convert(text)
    except Exception:  # pragma: no cover - 转换库异常不应阻断导出
        LOGGER.warning("繁体转简体失败，保留原文", exc_info=True)
        return text


_SIMPLIFIER_CACHE: list[Any] = []
_SIMPLIFIER_LOCK = threading.Lock()


def _chinese_converter():
    """惰性构造 OpenCC t2s 转换器；依赖缺失时返回 None（导出仍可用）。"""
    if _SIMPLIFIER_CACHE:
        return _SIMPLIFIER_CACHE[0]
    with _SIMPLIFIER_LOCK:
        if _SIMPLIFIER_CACHE:
            return _SIMPLIFIER_CACHE[0]
        try:
            import opencc  # noqa: PLC0415 - 惰性导入，避免拖慢模块加载

            converter = opencc.OpenCC("t2s")
        except Exception:
            LOGGER.warning(
                "未安装 opencc，角色卡台词保持原文（如需简体请安装 opencc-python-reimplemented）",
            )
            converter = None
        _SIMPLIFIER_CACHE.append(converter)
        return converter


def _simplify_card_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """对角色卡里可能带繁体台词的字段做 t2s，其余字段原样保留。"""
    simplified = dict(data)
    for key in ("first_mes", "mes_example", "creator_notes", "system_prompt",
                "post_history_instructions", "description", "personality", "scenario"):
        value = simplified.get(key)
        if isinstance(value, str) and value:
            simplified[key] = _simplify_chinese(value)
    greetings = simplified.get("alternate_greetings")
    if isinstance(greetings, list):
        simplified["alternate_greetings"] = [
            _simplify_chinese(item) if isinstance(item, str) else item for item in greetings
        ]
    tags = simplified.get("tags")
    if isinstance(tags, list):
        simplified["tags"] = [
            _simplify_chinese(item) if isinstance(item, str) else item for item in tags
        ]
    return simplified



def _archive_stem(document: Mapping[str, Any], kind: str) -> str:
    book_title = str(document.get("bookTitle") or "").strip()
    character = str(document.get("characterName") or "").strip()
    entity_id = str(document.get("entityId") or "").strip()
    if kind == LOREBOOK_KIND:
        suffix = "worldbook"
        subject = ""
    elif kind == CHARACTER_CARD_KIND:
        suffix = "card"
        subject = character or entity_id
    else:
        suffix = "skill"
        subject = character or entity_id
    if subject:
        stem = f"{book_title}-{subject}-{suffix}" if book_title else f"{subject}-{suffix}"
    elif book_title:
        stem = f"{book_title}-{suffix}"
    else:
        stem = f"character-{suffix}"
    cleaned = "".join("_" if char in _UNSAFE_FILENAME_CHARS else char for char in stem)
    return " ".join(cleaned.split()).strip(" .")


def _archive_filename(document: Mapping[str, Any], kind: str = CHARACTER_SKILL_KIND) -> str:
    "\u4e0b\u8f7d\u6587\u4ef6\u540d\uff1a\u4e66\u540d\u89d2\u8272\u540d\u5728 Windows \u8d44\u6e90\u7ba1\u7406\u5668\u91cc\u4e5f\u80fd\u843d\u5730\u3002"
    stem = _archive_stem(document, kind)
    return f"{stem or 'character-skill'}.zip"


def _card_filename(document: Mapping[str, Any]) -> str:
    """角色卡单独导出一个 JSON 文件（不再打包成 zip）。"""
    stem = _archive_stem(document, CHARACTER_CARD_KIND)
    return f"{stem or 'character-card'}.json"


# 问答注入角色档案时，只取对「这个人是谁、怎么说话、和谁什么关系」有用的一小部分，
# 并把条目数量硬性截断——档案里的 timeline 可能有几十条，全塞进问答提示词会挤掉
# 页面证据（实测角色档案正文能到 2 万字符，而页面资料通常只有 2-4 千）。
PROFILE_TRAIT_LIMIT = 6
PROFILE_RELATION_LIMIT = 8
PROFILE_SPEECH_LIMIT = 4


def _profile_item_text(value: Any) -> str:
    """把档案里的一条记录压成一行可读文本；无有效内容返回空串。"""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, Mapping):
        return ""
    for key in ("trait", "pattern", "dynamic", "relation", "name", "event", "note"):
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def character_profile_digest(document: Mapping[str, Any]) -> dict[str, Any]:
    """从角色档案产物里抽出「给问答用」的摘要。

    刻意不返回完整档案：完整档案是给「扮演」和「导出」用的，条目多、引用密，
    放进问答提示词会淹没页面证据。这里只保留一句话概括、性格、说话方式、
    人际关系和扮演提示——恰好覆盖「这个角色是个什么样的人」这类提问。
    """
    if not isinstance(document, Mapping):
        return {}
    summary = document.get("summary")
    if not isinstance(summary, Mapping):
        summary = {}

    name = str(document.get("characterName") or "").strip()
    lines: list[str] = []
    one_liner = summary.get("one_liner")
    if isinstance(one_liner, str) and one_liner.strip():
        lines.append(f"一句话概括：{one_liner.strip()}")

    traits: list[str] = []
    for value in summary.get("personality") or []:
        text = _profile_item_text(value)
        if text and text not in traits:
            traits.append(text)
        if len(traits) >= PROFILE_TRAIT_LIMIT:
            break
    if traits:
        lines.append("性格：" + "；".join(traits))

    speech: list[str] = []
    for value in summary.get("speech_style") or []:
        text = _profile_item_text(value)
        if text and text not in speech:
            speech.append(text)
        if len(speech) >= PROFILE_SPEECH_LIMIT:
            break
    if speech:
        lines.append("说话方式：" + "；".join(speech))

    relations: list[str] = []
    for value in summary.get("relationships") or []:
        if not isinstance(value, Mapping):
            continue
        other = value.get("name")
        relation = value.get("relation") or value.get("dynamic")
        if isinstance(other, str) and other.strip():
            text = other.strip()
            if isinstance(relation, str) and relation.strip():
                text = f"{text}（{relation.strip()}）"
            if text not in relations:
                relations.append(text)
        if len(relations) >= PROFILE_RELATION_LIMIT:
            break
    if relations:
        lines.append("人际关系：" + "、".join(relations))

    persona = summary.get("persona_prompt")
    if isinstance(persona, str) and persona.strip():
        lines.append(f"扮演提示：{persona.strip()}")

    if not lines:
        return {}
    stats = document.get("stats") if isinstance(document.get("stats"), Mapping) else {}
    return {
        "entityId": str(document.get("entityId") or ""),
        "name": name,
        "lines": lines,
        "revision": document.get("revision"),
        "updatedAt": document.get("updatedAt"),
        "pages": stats.get("pages") if isinstance(stats, Mapping) else None,
    }


def build_card_document(document: Mapping[str, Any]) -> tuple[str, bytes]:
    """把角色卡导出为**单个** SillyTavern V3 JSON 文件。

    用户要求：角色卡只要一个 json 就够了，不要 zip 里塞 V2/V3/Markdown 三份。
    V3 规范向下兼容（V2 读取器认 ``spec: chara_card_v3`` 的 data 段），因此只给 V3；
    Markdown 预览仍可在界面上看到，无需再导出。
    """
    files = document.get("files")
    if not isinstance(files, Mapping) or not files:
        raise InsightNotFound("该角色卡没有可导出的内容")
    payload = files.get("character-card-v3.json")
    if payload is None:
        # 兼容旧产物：只有 V2 时用它拼一个 V3 外壳。
        v2_text = files.get("character-card-v2.json")
        if v2_text is None:
            raise InsightNotFound("该角色卡没有可导出的内容")
        raw = json.loads(v2_text if isinstance(v2_text, str) else str(v2_text))
        payload = json.dumps(
            {
                "spec": "chara_card_v3",
                "spec_version": "3.0",
                "data": raw.get("data") or {},
            },
            ensure_ascii=False,
            indent=2,
        )
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    # 兜底：确保落盘内容是简体（正常路径已在 _render_card_files 转过）。
    text = _simplify_chinese(text)
    return _card_filename(document), text.encode("utf-8")


def build_skill_archive(
    document: Mapping[str, Any],
    kind: str = CHARACTER_SKILL_KIND,
) -> tuple[str, bytes]:
    "\u628a\u4e00\u4efd\u84b8\u998f\u4ea7\u7269\u7684\u5168\u90e8\u6587\u4ef6\u6253\u6210 zip\uff0c\u8fd4\u56de\uff08\u4e0b\u8f7d\u6587\u4ef6\u540d, \u538b\u7f29\u5305\u5b57\u8282\uff09\u3002"
    files = document.get("files")
    if not isinstance(files, Mapping) or not files:
        raise InsightNotFound("该蒸馏产物没有可导出的文件")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(str(key) for key in files):
            content = files[path]
            entry = content if isinstance(content, str) else str(content)
            archive.writestr(path, entry.encode("utf-8"))
    return _archive_filename(document, kind), buffer.getvalue()


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
        self._batches: dict[str, dict[str, Any]] = {}
        self._batch_stop: set[str] = set()
        self._guard = threading.Lock()

    # ---- 查询 ----------------------------------------------------------

    def characters(self, *, book_id: str, min_pages: int = 0) -> list[dict[str, Any]]:
        return self.repository.characters(book_id=book_id, min_pages=min_pages)

    def get(
        self,
        *,
        book_id: str,
        entity_id: str,
        kind: str = CHARACTER_SKILL_KIND,
    ) -> dict[str, Any]:
        return self.repository.get_skill(book_id=book_id, entity_id=entity_id, kind=kind)

    def status(
        self,
        *,
        book_id: str,
        entity_id: str,
        kind: str = CHARACTER_SKILL_KIND,
    ) -> dict[str, Any]:
        key = _task_key(book_id, kind, entity_id)
        with self._guard:
            task = self._tasks.get(key)
            if task is not None:
                return dict(task)
        try:
            document = self.repository.get_skill(book_id=book_id, entity_id=entity_id, kind=kind)
        except InsightNotFound:
            return {
                "bookId": book_id,
                "entityId": entity_id,
                "kind": kind,
                "status": "idle",
                "stage": None,
                "done": 0,
                "total": 0,
            }
        return {
            "bookId": book_id,
            "entityId": entity_id,
            "kind": kind,
            "status": "ready",
            "stage": "done",
            "done": 1,
            "total": 1,
            "revision": document.get("revision"),
            "updatedAt": document.get("updatedAt"),
        }

    # ---- 触发 ----------------------------------------------------------

    def start(
        self,
        *,
        book_id: str,
        entity_id: str,
        max_vision_pages: int = DEFAULT_MAX_VISION_PAGES,
        kind: str = CHARACTER_SKILL_KIND,
    ) -> dict[str, Any]:
        if kind not in (CHARACTER_SKILL_KIND, CHARACTER_CARD_KIND):
            raise ValueError(f"不支持的蒸馏类型：{kind}")
        limit = max(1, min(int(max_vision_pages or DEFAULT_MAX_VISION_PAGES), MAX_VISION_PAGES))
        label = DISTILL_KIND_LABELS.get(kind, kind)
        with self._guard:
            key = _task_key(book_id, kind, entity_id)
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
                "kind": kind,
                "label": label,
                "status": "running",
                "stage": "queued",
                "done": 0,
                "total": 3,
                "startedAt": utcnow().isoformat(timespec="seconds"),
                "error": None,
            }
            self._tasks[key] = task
        threading.Thread(
            target=self._run,
            args=(key, book_id, entity_id, limit, vlm_config, chat_config, kind),
            name=f"distill-{kind}",
            daemon=True,
        ).start()
        return dict(task)

    def start_lorebook(
        self,
        *,
        book_id: str,
        max_entities: int = DEFAULT_LOREBOOK_ENTITIES,
    ) -> dict[str, Any]:
        limit = max(1, min(int(max_entities or DEFAULT_LOREBOOK_ENTITIES), MAX_LOREBOOK_ENTITIES))
        entity_id = LOREBOOK_ENTITY_ID
        with self._guard:
            key = _task_key(book_id, LOREBOOK_KIND, entity_id)
            existing = self._tasks.get(key)
            if existing is not None and existing.get("status") == "running":
                return dict(existing)
            vlm_config, chat_config = self._runtime_configs(book_id)
            task = {
                "bookId": book_id,
                "entityId": entity_id,
                "kind": LOREBOOK_KIND,
                "label": DISTILL_KIND_LABELS.get(LOREBOOK_KIND, LOREBOOK_KIND),
                "status": "running",
                "stage": "queued",
                "done": 0,
                "total": 2,
                "startedAt": utcnow().isoformat(timespec="seconds"),
                "error": None,
            }
            self._tasks[key] = task
        threading.Thread(
            target=self._run,
            args=(key, book_id, entity_id, limit, vlm_config, chat_config, LOREBOOK_KIND),
            name="distill-lorebook",
            daemon=True,
        ).start()
        return dict(task)

    def start_batch(
        self,
        *,
        book_id: str,
        entity_ids: list[str],
        max_vision_pages: int = DEFAULT_MAX_VISION_PAGES,
        kind: str = CHARACTER_SKILL_KIND,
    ) -> dict[str, Any]:
        """批量蒸馏：串行逐个执行，同一本书同一 kind 同时只允许一个批次。"""
        if kind not in (CHARACTER_SKILL_KIND, CHARACTER_CARD_KIND):
            raise ValueError(f"不支持的蒸馏类型：{kind}")
        targets = [str(item) for item in entity_ids if str(item).strip()]
        if not targets:
            raise ValueError("entityIds 不能为空")
        limit = max(1, min(int(max_vision_pages or DEFAULT_MAX_VISION_PAGES), MAX_VISION_PAGES))
        known = {item["entityId"] for item in self.repository.characters(book_id=book_id)}
        unknown = [item for item in targets if item not in known]
        if unknown:
            raise InsightNotFound(f"角色不存在：{', '.join(unknown[:3])}")
        batch_key = _batch_key(book_id, kind)
        with self._guard:
            existing = self._batches.get(batch_key)
            if existing is not None and existing.get("status") == "running":
                return dict(existing)
            vlm_config, chat_config = self._runtime_configs(book_id)
            batch = {
                "bookId": book_id,
                "kind": kind,
                "label": DISTILL_KIND_LABELS.get(kind, kind),
                "status": "running",
                "stage": "queued",
                "done": 0,
                "total": len(targets),
                "succeeded": 0,
                "failed": 0,
                "currentEntityId": None,
                "entityIds": list(targets),
                "failures": [],
                "startedAt": utcnow().isoformat(timespec="seconds"),
                "error": None,
            }
            self._batches[batch_key] = batch
        threading.Thread(
            target=self._run_batch,
            args=(batch_key, book_id, targets, limit, vlm_config, chat_config, kind),
            name=f"distill-batch-{kind}",
            daemon=True,
        ).start()
        return dict(batch)

    def batch_status(self, *, book_id: str, kind: str = CHARACTER_SKILL_KIND) -> dict[str, Any]:
        batch_key = _batch_key(book_id, kind)
        with self._guard:
            batch = self._batches.get(batch_key)
            if batch is not None:
                return dict(batch)
        return {
            "bookId": book_id,
            "kind": kind,
            "status": "idle",
            "stage": None,
            "done": 0,
            "total": 0,
        }

    def stop_batch(self, *, book_id: str, kind: str = CHARACTER_SKILL_KIND) -> dict[str, Any]:
        """请求停止批次：只设置标记，当前角色跑完后在下一个角色前退出。"""
        batch_key = _batch_key(book_id, kind)
        with self._guard:
            batch = self._batches.get(batch_key)
            if batch is None:
                return self.batch_status(book_id=book_id, kind=kind)
            if batch.get("status") == "running":
                self._batch_stop.add(batch_key)
            return dict(batch)

    # ---- 内部 ----------------------------------------------------------

    def _run_batch(
        self,
        batch_key: str,
        book_id: str,
        entity_ids: list[str],
        limit: int,
        vlm_config: Any,
        chat_config: Any,
        kind: str,
    ) -> None:
        builder = CharacterSkillBuilder(
            self.repository,
            vlm_config=vlm_config,
            chat_config=chat_config,
        )
        for index, entity_id in enumerate(entity_ids):
            with self._guard:
                if batch_key in self._batch_stop:
                    self._batch_stop.discard(batch_key)
                    batch = self._batches.get(batch_key) or {}
                    batch.update(
                        {
                            "status": "stopped",
                            "stage": "stopped",
                            "currentEntityId": None,
                            "finishedAt": utcnow().isoformat(timespec="seconds"),
                        }
                    )
                    self._batches[batch_key] = batch
                    return
                batch = self._batches.get(batch_key)
                if batch is not None:
                    batch.update({"stage": "running", "done": index, "currentEntityId": entity_id})
            key = _task_key(book_id, kind, entity_id)
            with self._guard:
                self._tasks[key] = {
                    "bookId": book_id,
                    "entityId": entity_id,
                    "kind": kind,
                    "label": DISTILL_KIND_LABELS.get(kind, kind),
                    "status": "running",
                    "stage": "queued",
                    "done": 0,
                    "total": 3,
                    "startedAt": utcnow().isoformat(timespec="seconds"),
                    "error": None,
                }
            # 复用单角色执行体：它负责写 task 终态；串行调用保证不会并发打供应商。
            self._run(key, book_id, entity_id, limit, vlm_config, chat_config, kind)
            with self._guard:
                result = self._tasks.get(key) or {}
                batch = self._batches.get(batch_key) or {}
                if result.get("status") == "ready":
                    batch["succeeded"] = int(batch.get("succeeded") or 0) + 1
                else:
                    batch["failed"] = int(batch.get("failed") or 0) + 1
                    failures = list(batch.get("failures") or [])
                    failures.append(
                        {"entityId": entity_id, "error": result.get("error") or "unknown"}
                    )
                    batch["failures"] = failures[:50]
                batch["done"] = index + 1
                self._batches[batch_key] = batch
        with self._guard:
            batch = self._batches.get(batch_key) or {}
            batch.update(
                {
                    "status": "completed",
                    "stage": "done",
                    "currentEntityId": None,
                    "finishedAt": utcnow().isoformat(timespec="seconds"),
                }
            )
            self._batches[batch_key] = batch

    def _run(
        self,
        key: str,
        book_id: str,
        entity_id: str,
        limit: int,
        vlm_config: Any,
        chat_config: Any,
        kind: str,
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
            if kind == LOREBOOK_KIND:
                payload = builder.build_lorebook(
                    book_id=book_id,
                    max_entities=limit,
                    progress=progress,
                )
            else:
                payload = builder.build(
                    book_id=book_id,
                    entity_id=entity_id,
                    max_vision_pages=limit,
                    progress=progress,
                    kind=kind,
                )
        except Exception as error:  # noqa: BLE001 - 结果通过任务状态回传
            LOGGER.warning(
                "distill %s failed for %s/%s: %s",
                kind,
                book_id,
                entity_id,
                error,
                exc_info=True,
            )
            with self._guard:
                self._tasks[key] = {
                    "bookId": book_id,
                    "entityId": entity_id,
                    "kind": kind,
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
                "kind": kind,
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
