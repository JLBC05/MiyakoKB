"""角色 Skill 蒸馏：服务面与 HTTP 面测试（不联网、不调用模型）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import time
import uuid
from urllib.parse import unquote
import zipfile

import pytest
from flask import Flask
from sqlalchemy import insert, update

from src.backend_v2.insight.character_skill import (
    CHARACTER_SKILL_KIND,
    CharacterSkillRepository,
    CharacterSkillService,
    PageEvidence,
    _Facts,
    _render_files,
    build_skill_archive,
)
from src.backend_v2.insight.repository import InsightConflict, InsightNotFound
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
)
from src.backend_v2.storage.seeding import seed_system_records


CHARACTER_ID = "character_hu_zhang_you_ren"
ITEM_ID = "item_su_ku_na_finger"
SECOND_CHARACTER_ID = "character_fu_hei_hui"


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
                {
                    "entity_id": SECOND_CHARACTER_ID,
                    "canonical_name": "伏黑惠",
                    "category": "characters",
                    "aliases": [],
                    "alias_details": [],
                    "total_frequency": 5,
                    "pages": [1, 2],
                    "valid_chapters": [""],
                    "confidence": 0.9,
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

    assert [item["entityId"] for item in characters] == [CHARACTER_ID, SECOND_CHARACTER_ID]
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


def test_character_skill_refuses_a_stale_entity_alignment(skill_platform) -> None:
    engine = skill_platform["engine"]
    book_id = skill_platform["book_id"]
    with engine.begin() as connection:
        connection.execute(
            update(analysis_artifacts)
            .where(
                analysis_artifacts.c.book_id == book_id,
                analysis_artifacts.c.kind == "entity_alignment",
            )
            .values(status="stale")
        )

    service = CharacterSkillService(engine, skill_platform["data_root"])
    with pytest.raises(InsightConflict, match="已过期"):
        service.characters(book_id=book_id)


def test_character_skill_still_accepts_a_degraded_entity_alignment(
    skill_platform,
) -> None:
    engine = skill_platform["engine"]
    book_id = skill_platform["book_id"]
    with engine.begin() as connection:
        connection.execute(
            update(analysis_artifacts)
            .where(
                analysis_artifacts.c.book_id == book_id,
                analysis_artifacts.c.kind == "entity_alignment",
            )
            .values(status="degraded")
        )

    service = CharacterSkillService(engine, skill_platform["data_root"])
    # degraded 只表示覆盖不全，仍然是这一轮的证据，可以继续用。
    assert [item["entityId"] for item in service.characters(book_id=book_id)]


def test_latest_run_prefers_the_richest_evidence_over_the_newest_run(
    skill_platform,
) -> None:
    """P3-6 的判据锁：count 最大优先，不是 created_at 最新优先。

    真实反例：full 构建发布 1457 页，随后的 incremental 只发布 2 页。
    若改成按时间取最新，角色档案的证据会从 1457 页塌到 2 页。
    """
    engine = skill_platform["engine"]
    book_id = skill_platform["book_id"]
    rich_run = "run-rich"
    thin_run = "run-thin"
    with engine.begin() as connection:
        chapter_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        # run_id / source_asset_id 都有外键，先造出被引用的行。
        asset_id = str(uuid.uuid4())
        connection.execute(
            insert(assets).values(
                id=asset_id,
                owner_user_id=str(uuid.uuid4()),
                relative_path=f"objects/{asset_id}.webp",
                mime_type="image/webp",
                checksum="0" * 64,
                byte_size=1,
                integrity_status="ok",
                created_at=now,
                updated_at=now,
            )
        )
        for run_id in (rich_run, thin_run):
            connection.execute(
                insert(analysis_runs).values(
                    id=run_id,
                    owner_user_id=str(uuid.uuid4()),
                    book_id=book_id,
                    job_id=None,
                    scope="full",
                    status="completed",
                    config_json="{}",
                    created_at=now,
                    updated_at=now,
                )
            )
        connection.execute(
            insert(chapters).values(
                id=chapter_id,
                book_id=book_id,
                ordinal=1,
                title="第1話",
                page_order_revision=1,
                settings_memory_json="{}",
                settings_memory_revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        page_ids: list[str] = []
        for ordinal in range(1, 5):
            page_id = str(uuid.uuid4())
            page_ids.append(page_id)
            connection.execute(
                insert(pages).values(
                    id=page_id,
                    chapter_id=chapter_id,
                    ordinal=ordinal,
                    logical_source_path=f"chapter-1/{ordinal:03d}.webp",
                    source_revision=1,
                    document_revision=1,
                    render_status="not_rendered",
                    detection_state="unprocessed",
                    page_style_defaults_json="{}",
                    warnings_json="[]",
                    created_at=now,
                    updated_at=now,
                )
            )
        # thin_run 更新（时间更晚），但只有 1 页已发布。
        for index, page_id in enumerate(page_ids):
            connection.execute(
                insert(analysis_page_results).values(
                    id=str(uuid.uuid4()),
                    run_id=rich_run,
                    page_id=page_id,
                    source_asset_id=asset_id,
                    source_checksum="0" * 64,
                    page_id_snapshot=page_id,
                    page_number_snapshot=index + 1,
                    payload_json="{}",
                    status="published",
                    created_at=now - timedelta(days=1),
                    updated_at=now - timedelta(days=1),
                )
            )
        connection.execute(
            insert(analysis_page_results).values(
                id=str(uuid.uuid4()),
                run_id=thin_run,
                page_id=page_ids[0],
                source_asset_id=asset_id,
                source_checksum="0" * 64,
                page_id_snapshot=page_ids[0],
                page_number_snapshot=1,
                payload_json="{}",
                status="published",
                created_at=now,
                updated_at=now,
            )
        )

    repository = CharacterSkillRepository(engine, skill_platform["data_root"])
    with engine.connect() as connection:
        assert repository.latest_run_id(connection, book_id) == rich_run


def test_character_skill_list_hides_characters_below_the_page_threshold(skill_platform) -> None:
    platform = skill_platform
    service = CharacterSkillService(platform["engine"], platform["data_root"])

    assert [item["entityId"] for item in service.characters(
        book_id=platform["book_id"], min_pages=3
    )] == [CHARACTER_ID]
    assert service.characters(book_id=platform["book_id"], min_pages=4) == []

    app = Flask("character-skill-threshold-test")
    app.register_blueprint(
        create_insight_blueprint(
            engine=platform["engine"],
            data_root=platform["data_root"],
            profile=resolve_runtime_profile("local"),
        )
    )
    client = app.test_client()
    base = f"/api/v2/insight/books/{platform['book_id']}/character-skills"

    assert [item["entityId"] for item in client.get(base).get_json()["characters"]] == [
        CHARACTER_ID,
        SECOND_CHARACTER_ID,
    ]
    filtered = client.get(f"{base}?minPages=4")
    assert filtered.status_code == 200
    assert filtered.get_json()["characters"] == []
    kept = client.get(f"{base}?minPages=3")
    assert [item["entityId"] for item in kept.get_json()["characters"]] == [CHARACTER_ID]

    invalid = client.get(f"{base}?minPages=-1")
    assert invalid.status_code == 422
    non_numeric = client.get(f"{base}?minPages=abc")
    assert non_numeric.status_code == 422


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
    assert [item["entityId"] for item in listed.get_json()["characters"]] == [
        CHARACTER_ID,
        SECOND_CHARACTER_ID,
    ]

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


def _insert_generated_skill(platform, *, files: dict[str, str]) -> None:
    with platform["engine"].begin() as connection:
        _insert_artifact(
            connection,
            book_id=platform["book_id"],
            kind=CHARACTER_SKILL_KIND,
            template=CHARACTER_ID,
            payload=json.dumps(
                {
                    "bookId": platform["book_id"],
                    "bookTitle": "测试漫画",
                    "entityId": CHARACTER_ID,
                    "characterName": "虎杖悠仁",
                    "files": files,
                },
                ensure_ascii=False,
            ),
            revision=1,
        )


def test_skill_scale_line_separates_appearances_from_pages_with_evidence() -> None:
    """出场页来自实体知识网络，证据页来自已发布的页面分析；两者数量不同时必须分开写。"""

    entity = {
        "entity_id": "character-test",
        "canonical_name": "虎杖悠仁",
        "category": "characters",
        "aliases": ["虎杖"],
        "pages": [3, 4, 9, 12],
    }
    evidence = [
        PageEvidence(
            global_page=global_page,
            chapter_title="第1话",
            chapter_ordinal=1,
            page_ordinal=global_page,
            page_id=f"page-{global_page}",
            page_summary="",
            key_events=[],
            characters=["虎杖悠仁"],
            other_entities={},
            asset_id=f"asset-{global_page}",
            asset_path=Path("unused"),
        )
        for global_page in (3, 9, 12)
    ]
    facts = _Facts(
        chapter_distribution={"第1话": 3},
        co_occurrence=[],
        concepts=[],
        events=[],
        solo_pages=[],
        importance={},
    )

    files = _render_files(
        book_title="测试漫画",
        character=entity,
        facts=facts,
        evidence=evidence,
        content={"identity": {"full_name": "虎杖悠仁"}, "one_liner": "一句话"},
        used_vision=[],
        vlm_model="",
    )

    assert "- 出场：4 页，其中 3 页有分析证据 / 1 个章节单元" in files["SKILL.md"]
    assert "- 出场：4 页 / " not in files["SKILL.md"]


def test_character_skill_archive_zips_every_generated_file(skill_platform) -> None:
    platform = skill_platform
    files = {
        "SKILL.md": "# 虎杖悠仁\n",
        "references/persona.md": "人格层\n",
        "references/citations.json": "{}\n",
    }
    _insert_generated_skill(platform, files=files)

    app = Flask("character-skill-archive-test")
    app.register_blueprint(
        create_insight_blueprint(
            engine=platform["engine"],
            data_root=platform["data_root"],
            profile=resolve_runtime_profile("local"),
        )
    )
    client = app.test_client()

    response = client.get(
        f"/api/v2/insight/books/{platform['book_id']}/character-skills/{CHARACTER_ID}/archive"
    )

    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    disposition = response.headers["Content-Disposition"]
    assert 'filename="character-skill.zip"' in disposition
    assert "filename*=UTF-8''" in disposition
    assert unquote(disposition.split("filename*=UTF-8''", 1)[1]) == "测试漫画-虎杖悠仁-skill.zip"

    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert sorted(archive.namelist()) == sorted(files)
        for path, content in files.items():
            assert archive.read(path).decode("utf-8") == content


def test_character_skill_archive_is_not_found_before_generation(skill_platform) -> None:
    platform = skill_platform
    app = Flask("character-skill-archive-missing-test")
    app.register_blueprint(
        create_insight_blueprint(
            engine=platform["engine"],
            data_root=platform["data_root"],
            profile=resolve_runtime_profile("local"),
        )
    )
    client = app.test_client()

    response = client.get(
        f"/api/v2/insight/books/{platform['book_id']}/character-skills/{CHARACTER_ID}/archive"
    )

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"


def test_skill_archive_filename_falls_back_and_sanitizes() -> None:
    name, payload = build_skill_archive(
        {
            "entityId": "character_a/b",
            "files": {"SKILL.md": "x"},
        }
    )

    assert name == "character_a_b-skill.zip"
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.namelist() == ["SKILL.md"]

    with pytest.raises(InsightNotFound):
        build_skill_archive({"entityId": CHARACTER_ID, "files": {}})


# ---- 批量蒸馏 ---------------------------------------------------------


def _wait_batch(service, book_id: str, *, timeout: float = 15.0) -> dict:
    """轮询批次状态直到离开 running/queued，避免依赖线程名。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = service.batch_status(book_id=book_id)
        if status.get("status") not in ("running", "queued"):
            return status
        time.sleep(0.02)
    raise AssertionError(f"batch did not finish in {timeout}s: {service.batch_status(book_id=book_id)}")


def _stub_configs(service) -> None:
    """夹具没有供应商凭据；批量编排逻辑与凭据无关，直接打桩。"""
    service._runtime_configs = lambda book_id: (object(), object())  # type: ignore[assignment]


def _install_fake_run(service, *, failing: set[str] | None = None, seen: list[str] | None = None):
    """把单角色执行体换成打桩版本：只记录顺序，不联网、不写产物。"""
    boom = set(failing or ())

    def fake_run(key, book_id, entity_id, limit, vlm_config, chat_config, kind) -> None:
        if seen is not None:
            seen.append(entity_id)
        if entity_id in boom:
            service._tasks[key] = {
                "bookId": book_id,
                "entityId": entity_id,
                "kind": kind,
                "status": "failed",
                "stage": "failed",
                "done": 0,
                "total": 0,
                "error": "boom",
            }
            return
        service._tasks[key] = {
            "bookId": book_id,
            "entityId": entity_id,
            "kind": kind,
            "status": "ready",
            "stage": "done",
            "done": 1,
            "total": 1,
            "revision": 1,
        }

    service._run = fake_run  # type: ignore[assignment]


def test_batch_runs_characters_one_by_one_in_the_given_order(skill_platform) -> None:
    platform = skill_platform
    service = CharacterSkillService(platform["engine"], platform["data_root"])
    _stub_configs(service)
    seen: list[str] = []
    _install_fake_run(service, seen=seen)

    started = service.start_batch(
        book_id=platform["book_id"],
        entity_ids=[SECOND_CHARACTER_ID, CHARACTER_ID],
    )

    # 打桩执行体是瞬时的，start_batch 返回时批次可能已经跑完；只断言入参形状。
    assert started["total"] == 2
    assert started["entityIds"] == [SECOND_CHARACTER_ID, CHARACTER_ID]

    # 串行且保持传入顺序：不能并发，也不能重排。
    final = _wait_batch(service, platform["book_id"])
    assert seen == [SECOND_CHARACTER_ID, CHARACTER_ID]
    assert final["status"] == "completed"
    assert final["done"] == 2
    assert final["succeeded"] == 2
    assert final["failed"] == 0
    assert final["currentEntityId"] is None


def test_batch_keeps_going_after_one_character_fails(skill_platform) -> None:
    platform = skill_platform
    service = CharacterSkillService(platform["engine"], platform["data_root"])
    _stub_configs(service)
    seen: list[str] = []
    _install_fake_run(service, failing={CHARACTER_ID}, seen=seen)

    service.start_batch(
        book_id=platform["book_id"],
        entity_ids=[CHARACTER_ID, SECOND_CHARACTER_ID],
    )
    final = _wait_batch(service, platform["book_id"])

    assert final["status"] == "completed"
    assert final["succeeded"] == 1
    assert final["failed"] == 1
    assert final["failures"][0]["entityId"] == CHARACTER_ID
    # 失败不能中断后续角色
    assert seen == [CHARACTER_ID, SECOND_CHARACTER_ID]


def test_batch_status_is_idle_before_any_batch(skill_platform) -> None:
    platform = skill_platform
    service = CharacterSkillService(platform["engine"], platform["data_root"])

    status = service.batch_status(book_id=platform["book_id"])

    assert status["status"] == "idle"
    assert status["total"] == 0


def test_batch_rejects_empty_and_unknown_entities(skill_platform) -> None:
    platform = skill_platform
    service = CharacterSkillService(platform["engine"], platform["data_root"])
    _stub_configs(service)

    with pytest.raises(ValueError):
        service.start_batch(book_id=platform["book_id"], entity_ids=[])
    with pytest.raises(InsightNotFound):
        service.start_batch(
            book_id=platform["book_id"], entity_ids=["character_not_here"]
        )


def test_batch_stop_finishes_after_the_current_character(skill_platform) -> None:
    platform = skill_platform
    service = CharacterSkillService(platform["engine"], platform["data_root"])
    _stub_configs(service)
    seen: list[str] = []

    def fake_run(key, book_id, entity_id, limit, vlm_config, chat_config, kind) -> None:
        seen.append(entity_id)
        # 跑第一个角色时请求停止：当前角色跑完，下一个角色不再开始。
        service.stop_batch(book_id=book_id)
        service._tasks[key] = {
            "bookId": book_id,
            "entityId": entity_id,
            "kind": kind,
            "status": "ready",
            "stage": "done",
            "done": 1,
            "total": 1,
        }

    service._run = fake_run  # type: ignore[assignment]
    service.start_batch(
        book_id=platform["book_id"],
        entity_ids=[CHARACTER_ID, SECOND_CHARACTER_ID],
    )
    final = _wait_batch(service, platform["book_id"])

    assert seen == [CHARACTER_ID]
    assert final["status"] == "stopped"
    assert final["done"] == 1


def test_batch_routes_accept_ids_and_report_status(skill_platform) -> None:
    platform = skill_platform
    app = Flask("character-skill-batch-test")
    app.register_blueprint(
        create_insight_blueprint(
            engine=platform["engine"],
            data_root=platform["data_root"],
            profile=resolve_runtime_profile("local"),
        )
    )
    client = app.test_client()
    base = f"/api/v2/insight/books/{platform['book_id']}/character-skills/batch"

    without_key = client.post(base, json={"entityIds": [CHARACTER_ID]})
    assert without_key.status_code == 422

    blank = client.post(
        base,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"entityIds": []},
    )
    assert blank.status_code == 422

    unknown = client.post(
        base,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"entityIds": ["character_not_here"]},
    )
    assert unknown.status_code == 404

    status = client.get(base)
    assert status.status_code == 200
    assert status.get_json()["status"] == "idle"

    # 成功启动路径在服务层用例覆盖：路由测试的夹具没有供应商凭据，
    # 走到 _runtime_configs 会被 422 拦下（与单角色路由行为一致）。
