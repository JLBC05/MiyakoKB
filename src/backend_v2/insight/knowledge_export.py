"""把整本书的知识库打成 zip：实体网络、时间线、概览、页面分析与角色资料。

设计要点（与压测报告发现 #1 同一教训）：
- 只做读取，不依赖 job 队列，导出是同步的纯函数，便于测试。
- 产物结构固定且自描述（README.md + manifest.json），便于外部工具消费。
- 页面级分析按「章序 + 页序」排序输出，不依赖 run 内部顺序。
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, func, select

from src.backend_v2.insight.character_skill import (
    CHARACTER_CARD_KIND,
    CHARACTER_SKILL_KIND,
    LOREBOOK_KIND,
)
from src.backend_v2.storage.schema import (
    analysis_artifacts,
    analysis_page_results,
    analysis_runs,
    books,
    chapters,
    pages,
    timeline_characters,
    timeline_events,
    timeline_versions,
)
from src.backend_v2.insight.repository import InsightNotFound

_ARTIFACT_KINDS = (
    "overview",
    "compressed_context",
    "entity_alignment",
    "timeline",
)
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def _safe_name(value: str, fallback: str = "knowledge") -> str:
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", value).strip(" ._")
    return cleaned or fallback


def _load_payload(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw


def _archive_filename(book_title: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{_safe_name(book_title, 'knowledge')}-知识库-{stamp}.zip"


def _readme(book_title: str, summary: Mapping[str, Any]) -> str:
    lines = [
        f"# {book_title} · 知识库导出",
        "",
        "本压缩包由 MiyakoKB 导出，内容全部来自该书的页面级 AI 分析与派生产物。",
        "",
        "## 目录结构",
        "",
        "| 路径 | 内容 |",
        "| --- | --- |",
        "| `manifest.json` | 导出清单（书名、页数、各文件条目数与生成时间） |",
        "| `book.json` | 书籍与章节元数据 |",
        "| `entities.json` | 实体对齐结果：规范实体、别名映射、自动归并日志 |",
        "| `timeline.json` | 剧情时间线：事件、人物、故事概要 |",
        "| `overviews/` | 概览产物（无剧透 / 剧情总结） |",
        "| `compressed_context.json` | 压缩上下文产物 |",
        "| `pages/` | 逐页分析结果（按章分文件，JSON 数组） |",
        "| `character_skills/` | 已蒸馏的角色资料与世界书（若已生成） |",
        "",
        "## 本次导出统计",
        "",
        f"- 章节数：{summary.get('chapterCount', 0)}",
        f"- 页数：{summary.get('pageCount', 0)}",
        f"- 已有页面分析的页数：{summary.get('analyzedPageCount', 0)}",
        f"- 规范实体数：{summary.get('entityCount', 0)}",
        f"- 别名数：{summary.get('aliasCount', 0)}",
        f"- 时间线事件数：{summary.get('eventCount', 0)}",
        "",
        "> 页码为全书连续页码（章序 + 页序），与时间线事件里的 `page_numbers` 一致。",
        "",
    ]
    return "\n".join(lines)


def _book_metadata(
    connection: Any,
    book_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    row = connection.execute(
        select(books.c.id, books.c.title, books.c.created_at, books.c.updated_at).where(
            books.c.id == book_id
        )
    ).mappings().first()
    if row is None:
        raise InsightNotFound("book not found")
    chapter_rows = list(
        connection.execute(
            select(chapters.c.id, chapters.c.ordinal, chapters.c.title)
            .where(chapters.c.book_id == book_id)
            .order_by(chapters.c.ordinal)
        ).mappings()
    )
    page_counts: dict[str, int] = {}
    if chapter_rows:
        counts = connection.execute(
            select(pages.c.chapter_id, func.count().label("total"))
            .where(pages.c.chapter_id.in_([row["id"] for row in chapter_rows]))
            .group_by(pages.c.chapter_id)
        ).mappings()
        page_counts = {row["chapter_id"]: int(row["total"]) for row in counts}
    chapter_entries = [
        {
            "chapterId": chapter["id"],
            "ordinal": int(chapter["ordinal"]),
            "title": chapter["title"],
            "pageCount": page_counts.get(chapter["id"], 0),
        }
        for chapter in chapter_rows
    ]
    meta = {
        "bookId": row["id"],
        "title": row["title"],
        "createdAt": str(row["created_at"]),
        "updatedAt": str(row["updated_at"]),
        "pageCount": sum(page_counts.values()),
        "chapterCount": len(chapter_entries),
        "chapters": chapter_entries,
    }
    return meta, chapter_entries


def _active_artifacts(connection: Any, book_id: str) -> dict[tuple[str, str], Any]:
    rows = connection.execute(
        select(
            analysis_artifacts.c.kind,
            analysis_artifacts.c.template,
            analysis_artifacts.c.status,
            analysis_artifacts.c.revision,
            analysis_artifacts.c.run_id,
            analysis_artifacts.c.payload_json,
        )
        .where(
            analysis_artifacts.c.book_id == book_id,
            analysis_artifacts.c.is_active.is_(True),
        )
        .order_by(analysis_artifacts.c.kind, analysis_artifacts.c.template)
    ).mappings()
    artifacts: dict[tuple[str, str], Any] = {}
    for row in rows:
        artifacts[(str(row["kind"]), str(row["template"]))] = {
            "kind": row["kind"],
            "template": row["template"],
            "status": row["status"],
            "revision": row["revision"],
            "runId": row["run_id"],
            "payload": _load_payload(row["payload_json"]),
        }
    return artifacts


def _page_analysis_rows(
    connection: Any,
    book_id: str,
) -> tuple[list[dict[str, Any]], int]:
    """按章序 + 页序取该页最新一次已发布分析。"""
    published = analysis_page_results.c.status == "published"
    latest_run = connection.execute(
        select(
            analysis_page_results.c.run_id,
            func.count().label("total"),
        )
        .join(analysis_runs, analysis_runs.c.id == analysis_page_results.c.run_id)
        .where(
            analysis_runs.c.book_id == book_id,
            published,
        )
        .group_by(analysis_page_results.c.run_id)
        .order_by(func.count().desc())
        .limit(1)
    ).mappings().first()
    if latest_run is None:
        return [], 0

    rows = list(
        connection.execute(
            select(
                chapters.c.ordinal.label("chapter_ordinal"),
                chapters.c.title.label("chapter_title"),
                pages.c.ordinal.label("page_ordinal"),
                analysis_page_results.c.id.label("result_id"),
                analysis_page_results.c.page_number_snapshot,
                analysis_page_results.c.payload_json,
                analysis_page_results.c.created_at,
            )
            .select_from(analysis_page_results)
            .join(pages, pages.c.id == analysis_page_results.c.page_id)
            .join(chapters, chapters.c.id == pages.c.chapter_id)
            .where(
                analysis_page_results.c.run_id == latest_run["run_id"],
                published,
                chapters.c.book_id == book_id,
            )
            .order_by(chapters.c.ordinal, pages.c.ordinal)
        ).mappings()
    )
    entries: list[dict[str, Any]] = []
    for row in rows:
        entries.append(
            {
                "resultId": row["result_id"],
                "pageNumber": row["page_number_snapshot"],
                "chapterOrdinal": int(row["chapter_ordinal"]),
                "chapterTitle": row["chapter_title"],
                "pageOrdinal": int(row["page_ordinal"]),
                "analyzedAt": str(row["created_at"]),
                "result": _load_payload(row["payload_json"]),
            }
        )
    return entries, int(latest_run["total"])


def _active_timeline(connection: Any, book_id: str) -> dict[str, Any] | None:
    """时间线存放在 timeline_versions / timeline_events / timeline_characters 三张表。"""
    version = connection.execute(
        select(
            timeline_versions.c.id,
            timeline_versions.c.run_id,
            timeline_versions.c.mode,
            timeline_versions.c.status,
            timeline_versions.c.content_json,
            timeline_versions.c.dependency_fingerprint,
        ).where(
            timeline_versions.c.book_id == book_id,
            timeline_versions.c.is_active.is_(True),
        )
    ).mappings().first()
    if version is None:
        return None
    events = list(
        connection.execute(
            select(timeline_events.c.ordinal, timeline_events.c.payload_json)
            .where(timeline_events.c.timeline_version_id == version["id"])
            .order_by(timeline_events.c.ordinal)
        ).mappings()
    )
    characters = list(
        connection.execute(
            select(timeline_characters.c.name, timeline_characters.c.payload_json)
            .where(timeline_characters.c.timeline_version_id == version["id"])
            .order_by(timeline_characters.c.name)
        ).mappings()
    )
    return {
        "version": {
            "timelineVersionId": version["id"],
            "runId": version["run_id"],
            "mode": version["mode"],
            "status": version["status"],
            "dependencyFingerprint": version["dependency_fingerprint"],
        },
        "content": _load_payload(version["content_json"]),
        "events": [
            {"ordinal": int(row["ordinal"]), "payload": _load_payload(row["payload_json"])}
            for row in events
        ],
        "characters": [
            {"name": row["name"], "payload": _load_payload(row["payload_json"])}
            for row in characters
        ],
    }


def _distilled_documents(connection: Any, book_id: str) -> list[dict[str, Any]]:
    """已蒸馏的角色资料 / 角色卡 / 世界书（存在才导出）。"""
    known = {CHARACTER_SKILL_KIND, CHARACTER_CARD_KIND, LOREBOOK_KIND}
    rows = connection.execute(
        select(
            analysis_artifacts.c.id,
            analysis_artifacts.c.kind,
            analysis_artifacts.c.template,
            analysis_artifacts.c.payload_json,
        )
        .where(
            analysis_artifacts.c.book_id == book_id,
            analysis_artifacts.c.is_active.is_(True),
            analysis_artifacts.c.kind.in_(sorted(known)),
        )
        .order_by(analysis_artifacts.c.kind, analysis_artifacts.c.template)
    ).mappings()
    documents: list[dict[str, Any]] = []
    for row in rows:
        payload = _load_payload(row["payload_json"])
        if not isinstance(payload, Mapping):
            continue
        files = payload.get("files")
        if not isinstance(files, Mapping) or not files:
            continue
        documents.append(
            {
                "artifactId": row["id"],
                "kind": row["kind"],
                "template": row["template"],
                "files": {str(key): value for key, value in files.items()},
            }
        )
    return documents


def build_knowledge_archive(engine: Engine, book_id: str) -> tuple[str, bytes]:
    """返回（下载文件名, zip 字节）。整库导出不依赖 job 队列。"""
    with engine.connect() as connection:
        meta, chapter_entries = _book_metadata(connection, book_id)
        artifacts = _active_artifacts(connection, book_id)
        page_entries, analyzed_total = _page_analysis_rows(connection, book_id)
        documents = _distilled_documents(connection, book_id)
        timeline_data = _active_timeline(connection, book_id)

    alignment = artifacts.get(("entity_alignment", "default"))
    alignment_payload: Mapping[str, Any] = (
        alignment["payload"]
        if alignment and isinstance(alignment["payload"], Mapping)
        else {}
    )
    entities = alignment_payload.get("canonical_entities") or []
    aliases = alignment_payload.get("alias_map") or []

    event_count = len(timeline_data["events"]) if timeline_data else 0

    files: dict[str, bytes] = {}
    files["book.json"] = _json_bytes(meta)
    files["entities.json"] = _json_bytes(
        {
            "entityAlignment": {
                key: value for key, value in alignment.items() if key != "payload"
            }
            if alignment
            else None,
            "slugMode": alignment_payload.get("slug_mode"),
            "updatedAt": alignment_payload.get("updated_at"),
            "entities": entities,
            "aliasMap": aliases,
            "autoMergeLog": alignment_payload.get("auto_merge_log") or [],
        }
    )
    files["timeline.json"] = _json_bytes(
        timeline_data
        if timeline_data is not None
        else {"version": None, "content": None, "events": [], "characters": []}
    )
    for (kind, template), artifact in artifacts.items():
        if kind != "overview":
            continue
        files[f"overviews/{_safe_name(template, 'overview')}.json"] = _json_bytes(
            {
                "status": artifact["status"],
                "revision": artifact["revision"],
                "payload": artifact["payload"],
            }
        )
    compressed = artifacts.get(("compressed_context", "default"))
    files["compressed_context.json"] = _json_bytes(
        {
            "present": bool(compressed),
            "status": compressed["status"] if compressed else None,
            "revision": compressed["revision"] if compressed else None,
            "payload": compressed["payload"] if compressed else None,
        }
    )
    for document in documents:
        folder = _safe_name(document["template"] or document["kind"], "document")
        for path, content in document["files"].items():
            entry = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            files[f"character_skills/{folder}/{_safe_name(Path(path).as_posix().replace('/', '-'), 'file')}"] = (
                entry.encode("utf-8")
            )

    by_chapter: dict[int, list[dict[str, Any]]] = {}
    for entry in page_entries:
        by_chapter.setdefault(entry["chapterOrdinal"], []).append(entry)
    for ordinal, entries in sorted(by_chapter.items()):
        files[f"pages/chapter-{ordinal:03d}.json"] = _json_bytes(entries)

    stats = {
        "chapterCount": meta["chapterCount"],
        "pageCount": meta["pageCount"],
        "analyzedPageCount": analyzed_total,
        "entityCount": len(entities) if isinstance(entities, list) else 0,
        "aliasCount": len(aliases) if isinstance(aliases, (list, Mapping)) else 0,
        "eventCount": event_count,
    }
    files["README.md"] = _readme(str(meta["title"]), stats).encode("utf-8")
    files["manifest.json"] = _json_bytes(
        {
            "format": "miyako-knowledge-archive",
            "version": 1,
            "exportedAt": datetime.now(timezone.utc).isoformat(),
            "bookId": meta["bookId"],
            "title": meta["title"],
            "stats": stats,
            "artifacts": [
                {
                    "kind": artifact["kind"],
                    "template": artifact["template"],
                    "status": artifact["status"],
                    "revision": artifact["revision"],
                }
                for artifact in artifacts.values()
            ],
            "files": sorted(files) + ["manifest.json"],
        }
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.writestr(path, files[path])
    return _archive_filename(str(meta["title"])), buffer.getvalue()
