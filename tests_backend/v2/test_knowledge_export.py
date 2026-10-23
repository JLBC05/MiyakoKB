from __future__ import annotations

from datetime import datetime, timezone
import io
import json
from pathlib import Path
import uuid
from urllib.parse import unquote
import zipfile

import pytest
from flask import Flask
from sqlalchemy import insert

from src.backend_v2.insight.character_skill import CHARACTER_SKILL_KIND
from src.backend_v2.insight.knowledge_export import build_knowledge_archive
from src.backend_v2.insight.repository import InsightNotFound
from src.backend_v2.insight.routes import create_insight_blueprint
from src.backend_v2.runtime_profile import resolve_runtime_profile
from src.backend_v2.storage.database import create_sqlite_engine
from src.backend_v2.storage.schema import (
    analysis_artifacts,
    analysis_page_results,
    analysis_runs,
    assets,
    books,
    chapters,
    metadata,
    pages,
    timeline_characters,
    timeline_events,
    timeline_versions,
)
from src.backend_v2.storage.seeding import seed_system_records

CHARACTER_ID = "character_hu_zhang_you_ren"


def _alignment_payload() -> str:
    return json.dumps(
        {
            "alias_map": {"虎杖": CHARACTER_ID},
            "auto_merge_log": [
                {
                    "raw_name": "虎杖",
                    "canonical_name": "虎杖悠仁",
                    "rule": "merge_rule_hit",
                    "confidence": "high",
                }
            ],
            "canonical_entities": [
                {
                    "entity_id": CHARACTER_ID,
                    "canonical_name": "虎杖悠仁",
                    "category": "characters",
                    "aliases": ["虎杖"],
                    "alias_details": [{"alias": "虎杖", "count": 3}],
                    "total_frequency": 7,
                    "pages": [1],
                    "valid_chapters": [""],
                    "confidence": 0.92,
                    "concept_types": [],
                    "original_names": [],
                }
            ],
            "slug_mode": "pinyin",
            "version": 1,
            "updated_at": "2026-10-20T00:00:00+00:00",
        },
        ensure_ascii=False,
    )


def _insert_artifact(
    connection,
    *,
    book_id: str,
    kind: str,
    template: str,
    payload: str,
    revision: int = 1,
    status: str = "ready",
) -> None:
    now = datetime.now(timezone.utc)
    connection.execute(
        insert(analysis_artifacts).values(
            id=str(uuid.uuid4()),
            book_id=book_id,
            run_id=None,
            kind=kind,
            template=template,
            status=status,
            revision=revision,
            is_active=True,
            dependency_fingerprint="0" * 64,
            payload_json=payload,
            asset_id=None,
            created_at=now,
            updated_at=now,
        )
    )


@pytest.fixture()
def knowledge_platform(tmp_path: Path):
    data_root = tmp_path / "data-v2"
    data_root.mkdir()
    engine = create_sqlite_engine(data_root / "saber.sqlite3")
    metadata.create_all(engine)
    seed_system_records(engine)

    book_id = str(uuid.uuid4())
    chapter_id = str(uuid.uuid4())
    page_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    timeline_id = str(uuid.uuid4())
    source_asset_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    with engine.begin() as connection:
        connection.execute(
            insert(assets).values(
                id=source_asset_id,
                owner_user_id=str(uuid.uuid4()),
                relative_path="objects/test/source.webp",
                mime_type="image/webp",
                checksum="0" * 64,
                byte_size=1024,
                width=None,
                height=None,
                integrity_status="ok",
                gc_marked_at=None,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(books).values(
                id=book_id,
                owner_user_id=str(uuid.uuid4()),
                kind="library",
                title="测试漫画",
                chapter_order_revision=1,
                cover_asset_id=None,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(chapters).values(
                id=chapter_id,
                book_id=book_id,
                ordinal=1,
                title="第1话",
                page_order_revision=1,
                settings_memory_json="{}",
                settings_memory_revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(pages).values(
                id=page_id,
                chapter_id=chapter_id,
                ordinal=1,
                logical_source_path="ch-1/001.webp",
                source_revision=1,
                document_revision=1,
                rendered_revision=None,
                render_status="not_rendered",
                detection_state="unprocessed",
                default_font_id=None,
                page_style_defaults_json="{}",
                warnings_json="[]",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(analysis_runs).values(
                id=run_id,
                owner_user_id=str(uuid.uuid4()),
                book_id=book_id,
                job_id=None,
                scope="full",
                status="completed",
                config_json="{}",
                missing_page_ids_json="[]",
                target_count=1,
                success_count=1,
                failed_count=0,
                published_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(analysis_page_results).values(
                id=str(uuid.uuid4()),
                run_id=run_id,
                page_id=page_id,
                source_asset_id=source_asset_id,
                source_checksum="0" * 64,
                page_id_snapshot=page_id,
                page_number_snapshot=1,
                payload_json=json.dumps(
                    {
                        "characters": [{"name": "虎杖悠仁", "description": "主角"}],
                        "locations": [],
                        "abilities": [],
                        "items": [],
                        "concepts": [],
                    },
                    ensure_ascii=False,
                ),
                status="published",
                created_at=now,
                updated_at=now,
            )
        )
        _insert_artifact(
            connection,
            book_id=book_id,
            kind="entity_alignment",
            template="default",
            payload=_alignment_payload(),
            revision=5,
        )
        _insert_artifact(
            connection,
            book_id=book_id,
            kind="overview",
            template="no_spoiler",
            payload=json.dumps({"content": "无剧透概览"}, ensure_ascii=False),
            status="degraded",
        )
        _insert_artifact(
            connection,
            book_id=book_id,
            kind="compressed_context",
            template="default",
            payload=json.dumps({"segments": []}, ensure_ascii=False),
        )
        _insert_artifact(
            connection,
            book_id=book_id,
            kind=CHARACTER_SKILL_KIND,
            template=CHARACTER_ID,
            payload=json.dumps(
                {
                    "entityId": CHARACTER_ID,
                    "characterName": "虎杖悠仁",
                    "files": {"SKILL.md": "# 虎杖悠仁\n"},
                },
                ensure_ascii=False,
            ),
        )
        connection.execute(
            insert(timeline_versions).values(
                id=timeline_id,
                book_id=book_id,
                run_id=run_id,
                mode="compressed",
                status="ready",
                content_json=json.dumps({"story_summary": "概要"}, ensure_ascii=False),
                dependency_fingerprint="1" * 64,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            insert(timeline_events).values(
                id=str(uuid.uuid4()),
                timeline_version_id=timeline_id,
                ordinal=1,
                payload_json=json.dumps(
                    {"summary": "虎杖吞下手指", "page_numbers": [1]},
                    ensure_ascii=False,
                ),
            )
        )
        connection.execute(
            insert(timeline_characters).values(
                id=str(uuid.uuid4()),
                timeline_version_id=timeline_id,
                name="虎杖悠仁",
                payload_json=json.dumps({"name": "虎杖悠仁"}, ensure_ascii=False),
            )
        )

    try:
        yield {"engine": engine, "data_root": data_root, "book_id": book_id}
    finally:
        engine.dispose()


def test_knowledge_archive_contains_every_section(knowledge_platform) -> None:
    platform = knowledge_platform
    filename, payload = build_knowledge_archive(platform["engine"], platform["book_id"])

    assert filename.startswith("测试漫画-知识库-")
    assert filename.endswith(".zip")

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())
        assert {
            "README.md",
            "manifest.json",
            "book.json",
            "entities.json",
            "timeline.json",
            "compressed_context.json",
            "overviews/no_spoiler.json",
            "pages/chapter-001.json",
            "character_skills/character_hu_zhang_you_ren/SKILL.md",
        } <= names

        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        assert manifest["format"] == "miyako-knowledge-archive"
        assert manifest["bookId"] == platform["book_id"]
        assert manifest["stats"]["chapterCount"] == 1
        assert manifest["stats"]["pageCount"] == 1
        assert manifest["stats"]["analyzedPageCount"] == 1
        assert manifest["stats"]["entityCount"] == 1
        assert manifest["stats"]["aliasCount"] == 1
        assert manifest["stats"]["eventCount"] == 1

        book = json.loads(archive.read("book.json").decode("utf-8"))
        assert book["title"] == "测试漫画"
        assert book["chapters"][0]["title"] == "第1话"
        assert book["chapters"][0]["pageCount"] == 1

        entities = json.loads(archive.read("entities.json").decode("utf-8"))
        assert entities["entityAlignment"]["revision"] == 5
        assert entities["entities"][0]["canonical_name"] == "虎杖悠仁"
        assert entities["aliasMap"] == {"虎杖": CHARACTER_ID}
        assert entities["autoMergeLog"][0]["raw_name"] == "虎杖"

        page_rows = json.loads(archive.read("pages/chapter-001.json").decode("utf-8"))
        assert len(page_rows) == 1
        assert page_rows[0]["pageNumber"] == 1
        assert page_rows[0]["chapterOrdinal"] == 1
        assert page_rows[0]["result"]["characters"][0]["name"] == "虎杖悠仁"

        timeline = json.loads(archive.read("timeline.json").decode("utf-8"))
        assert timeline["version"]["mode"] == "compressed"
        assert timeline["events"][0]["payload"]["summary"] == "虎杖吞下手指"
        assert timeline["characters"][0]["name"] == "虎杖悠仁"

        overview = json.loads(archive.read("overviews/no_spoiler.json").decode("utf-8"))
        assert overview["status"] == "degraded"

        readme = archive.read("README.md").decode("utf-8")
        assert "测试漫画 · 知识库导出" in readme


def test_knowledge_archive_route_streams_a_zip(knowledge_platform) -> None:
    platform = knowledge_platform
    app = Flask("knowledge-archive-test")
    app.register_blueprint(
        create_insight_blueprint(
            engine=platform["engine"],
            data_root=platform["data_root"],
            profile=resolve_runtime_profile("local"),
        )
    )
    client = app.test_client()

    response = client.get(
        f"/api/v2/insight/books/{platform['book_id']}/knowledge-archive"
    )

    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    disposition = response.headers["Content-Disposition"]
    assert 'filename="knowledge-archive.zip"' in disposition
    assert "filename*=UTF-8''" in disposition
    assert unquote(disposition.split("filename*=UTF-8''", 1)[1]).startswith(
        "测试漫画-知识库-"
    )
    assert int(response.headers["Content-Length"]) == len(response.data)
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert "manifest.json" in archive.namelist()


def test_knowledge_archive_rejects_unknown_book(knowledge_platform) -> None:
    with pytest.raises(InsightNotFound):
        build_knowledge_archive(
            knowledge_platform["engine"],
            "00000000-0000-0000-0000-0000000000ff",
        )


def test_knowledge_archive_without_analysis_still_exports_metadata(knowledge_platform) -> None:
    platform = knowledge_platform
    book_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with platform["engine"].begin() as connection:
        connection.execute(
            insert(books).values(
                id=book_id,
                owner_user_id=str(uuid.uuid4()),
                kind="library",
                title="空书",
                chapter_order_revision=1,
                cover_asset_id=None,
                created_at=now,
                updated_at=now,
            )
        )

    filename, payload = build_knowledge_archive(platform["engine"], book_id)

    assert filename.startswith("空书-知识库-")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        assert manifest["stats"]["entityCount"] == 0
        assert manifest["stats"]["eventCount"] == 0
        assert "pages/chapter-001.json" not in archive.namelist()
