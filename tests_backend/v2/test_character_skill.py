"""角色 Skill 蒸馏：服务面与 HTTP 面测试（不联网、不调用模型）。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

import pytest
from flask import Flask
from sqlalchemy import insert, update

from src.backend_v2.insight.character_skill import (
    CHARACTER_SKILL_KIND,
    CharacterSkillService,
)
from src.backend_v2.insight.repository import InsightNotFound
from src.backend_v2.insight.routes import create_insight_blueprint
from src.backend_v2.runtime_profile import resolve_runtime_profile
from src.backend_v2.storage.database import create_sqlite_engine
from src.backend_v2.storage.schema import analysis_artifacts, books, metadata
from src.backend_v2.storage.seeding import seed_system_records


CHARACTER_ID = "character_hu_zhang_you_ren"
ITEM_ID = "item_su_ku_na_finger"


def _alignment_payload() -> str:
    return json.dumps(
        {
            "alias_map": {"虎杖": CHARACTER_ID},
            "auto_merge_log": [],
            "canonical_entities": [
                {
                    "entity_id": CHARACTER_ID,
                    "canonical_name": "虎杖悠仁",
                    "category": "characters",
                    "aliases": ["虎杖"],
                    "alias_details": [{"alias": "虎杖", "count": 3}],
                    "total_frequency": 7,
                    "pages": [1, 2, 5],
                    "valid_chapters": [""],
                    "confidence": 0.92,
                    "concept_types": [],
                    "original_names": [],
                },
                {
                    "entity_id": ITEM_ID,
                    "canonical_name": "宿傩手指",
                    "category": "items",
                    "aliases": [],
                    "alias_details": [],
                    "total_frequency": 2,
                    "pages": [1],
                    "valid_chapters": [""],
                    "confidence": 0.8,
                    "concept_types": [],
                    "original_names": [],
                },
            ],
            "slug_mode": "pinyin",
            "version": 1,
            "updated_at": "2026-10-20T00:00:00+00:00",
        }
    )


def _insert_artifact(
    connection,
    *,
    book_id: str,
    kind: str,
    template: str,
    payload: str,
    revision: int,
    status: str = "ready",
) -> str:
    artifact_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    connection.execute(
        insert(analysis_artifacts).values(
            id=artifact_id,
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
    return artifact_id


@pytest.fixture()
def skill_platform(tmp_path: Path):
    data_root = tmp_path / "data-v2"
    data_root.mkdir()
    engine = create_sqlite_engine(data_root / "saber.sqlite3")
    metadata.create_all(engine)
    seed_system_records(engine)
    book_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
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
        _insert_artifact(
            connection,
            book_id=book_id,
            kind="entity_alignment",
            template="default",
            payload=_alignment_payload(),
            revision=5,
        )
    try:
        yield {
            "engine": engine,
            "data_root": data_root,
            "book_id": book_id,
        }
    finally:
        engine.dispose()


def test_character_skills_list_only_character_entities(skill_platform) -> None:
    platform = skill_platform
    service = CharacterSkillService(platform["engine"], platform["data_root"])

    characters = service.characters(book_id=platform["book_id"])

    assert [item["entityId"] for item in characters] == [CHARACTER_ID]
    only = characters[0]
    assert only["name"] == "虎杖悠仁"
    assert only["aliases"] == ["虎杖"]
    assert only["pageCount"] == 3
    assert only["chapterCount"] == 0
    assert only["frequency"] == 7
    assert only["hasSkill"] is False
    assert only["skillRevision"] is None
    assert only["updatedAt"] is None


def test_character_skill_status_reports_idle_then_ready(skill_platform) -> None:
    platform = skill_platform
    service = CharacterSkillService(platform["engine"], platform["data_root"])

    idle = service.status(book_id=platform["book_id"], entity_id=CHARACTER_ID)
    assert idle["status"] == "idle"
    assert idle["done"] == 0 and idle["total"] == 0

    with platform["engine"].begin() as connection:
        _insert_artifact(
            connection,
            book_id=platform["book_id"],
            kind=CHARACTER_SKILL_KIND,
            template=CHARACTER_ID,
            payload=json.dumps(
                {
                    "bookId": platform["book_id"],
                    "entityId": CHARACTER_ID,
                    "name": "虎杖悠仁",
                    "pageCount": 3,
                    "visionPages": [1],
                    "chapters": [{"ordinal": 1, "title": "第1話", "pages": 3}],
                    "profile": {"summary": "intro", "traits": []},
                    "persona": {"speech": [], "temperament": []},
                    "timeline": [],
                    "limits": ["仅依据知识库"],
                    "citations": [],
                }
            ),
            revision=2,
        )

    ready = service.status(book_id=platform["book_id"], entity_id=CHARACTER_ID)
    assert ready["status"] == "ready"
    assert ready["revision"] == 2
    assert ready["updatedAt"]

    listed = service.characters(book_id=platform["book_id"])[0]
    assert listed["hasSkill"] is True
    assert listed["skillRevision"] == 2

    detail = service.get(book_id=platform["book_id"], entity_id=CHARACTER_ID)
    assert detail["name"] == "虎杖悠仁"
    assert detail["revision"] == 2
    assert detail["status"] == "ready"


def test_character_skill_detail_is_not_found_before_generation(skill_platform) -> None:
    platform = skill_platform
    service = CharacterSkillService(platform["engine"], platform["data_root"])

    with pytest.raises(InsightNotFound):
        service.get(book_id=platform["book_id"], entity_id=CHARACTER_ID)


def test_character_skill_routes_expose_list_detail_and_status(skill_platform) -> None:
    platform = skill_platform
    app = Flask("character-skill-route-test")
    app.register_blueprint(
        create_insight_blueprint(
            engine=platform["engine"],
            data_root=platform["data_root"],
            profile=resolve_runtime_profile("local"),
        )
    )
    client = app.test_client()
    base = f"/api/v2/insight/books/{platform['book_id']}/character-skills"

    listed = client.get(base)
    assert listed.status_code == 200
    assert [item["entityId"] for item in listed.get_json()["characters"]] == [CHARACTER_ID]

    status = client.get(f"{base}/{CHARACTER_ID}/status")
    assert status.status_code == 200
    assert status.get_json()["status"] == "idle"

    missing = client.get(f"{base}/{CHARACTER_ID}")
    assert missing.status_code == 404
    assert missing.get_json()["error"]["code"] == "not_found"

    unknown = client.post(
        base,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"entityId": "character_not_a_character"},
    )
    assert unknown.status_code == 404

    without_key = client.post(base, json={"entityId": CHARACTER_ID})
    assert without_key.status_code == 422

    blank = client.post(
        base,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"entityId": "  "},
    )
    assert blank.status_code == 422

    extra = client.post(
        base,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"entityId": CHARACTER_ID, "unexpected": 1},
    )
    assert extra.status_code == 422
