from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import datetime, timezone
from urllib.parse import unquote

from flask import Flask

from src.backend_v2.insight.character_skill import (
    CHARACTER_CARD_KIND,
    LOREBOOK_KIND,
)
from src.backend_v2.insight.routes import create_insight_blueprint
from src.backend_v2.runtime_profile import resolve_runtime_profile
from src.backend_v2.storage.database import create_sqlite_engine
from src.backend_v2.storage.schema import analysis_artifacts, books, metadata
from src.backend_v2.storage.seeding import seed_system_records
from sqlalchemy import insert


def _insert_artifact(connection, *, book_id, kind, template, payload, revision):
    now = datetime.now(timezone.utc)
    connection.execute(
        insert(analysis_artifacts).values(
            id=str(uuid.uuid4()),
            book_id=book_id,
            run_id=None,
            kind=kind,
            template=template,
            status="ready",
            revision=revision,
            is_active=True,
            dependency_fingerprint="0" * 64,
            payload_json=payload,
            asset_id=None,
            created_at=now,
            updated_at=now,
        )
    )


def _platform(tmp_path):
    data_root = tmp_path / "data-v2"
    data_root.mkdir()
    engine = create_sqlite_engine(data_root / "saber.sqlite3")
    metadata.create_all(engine)
    seed_system_records(engine)
    book_id = str(uuid.uuid4())
    with engine.begin() as connection:
        connection.execute(
            insert(books).values(
                id=book_id,
                owner_user_id=str(uuid.uuid4()),
                kind="library",
                title="测试漫画",
                chapter_order_revision=1,
                cover_asset_id=None,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
    app = Flask("distill-kinds-route-test")
    app.register_blueprint(
        create_insight_blueprint(
            engine=engine,
            data_root=data_root,
            profile=resolve_runtime_profile("local"),
        )
    )
    return {"book_id": book_id, "engine": engine, "client": app.test_client()}


def _card_files() -> dict[str, str]:
    return {
        "character-card-v3.json": '{"spec": "chara_card_v3"}',
        "character-card-v2.json": '{"spec": "chara_card_v2"}',
        "character-card.md": "# 久留美\n",
    }


def test_character_card_kind_is_served_and_archived_separately(tmp_path) -> None:
    platform = _platform(tmp_path)
    book_id = platform["book_id"]
    client = platform["client"]
    with platform["engine"].begin() as connection:
        _insert_artifact(
            connection,
            book_id=book_id,
            kind=CHARACTER_CARD_KIND,
            template="character_jiu_liu_mei",
            payload=json.dumps(
                {
                    "bookId": book_id,
                    "bookTitle": "测试漫画",
                    "entityId": "character_jiu_liu_mei",
                    "characterName": "久留美",
                    "files": _card_files(),
                },
                ensure_ascii=False,
            ),
            revision=2,
        )

    base = f"/api/v2/insight/books/{book_id}/character-skills"

    skill_status = client.get(f"{base}/character_jiu_liu_mei/status")
    assert skill_status.get_json()["status"] == "idle"

    card_status = client.get(f"{base}/character_jiu_liu_mei/status?kind=card")
    assert card_status.get_json()["status"] == "ready"
    assert card_status.get_json()["revision"] == 2

    card = client.get(f"{base}/character_jiu_liu_mei?kind=card")
    assert card.status_code == 200
    assert card.get_json()["files"]["character-card.md"].startswith("# 久留美")

    archive = client.get(f"{base}/character_jiu_liu_mei/archive?kind=card")
    assert archive.status_code == 200
    disposition = archive.headers["Content-Disposition"]
    assert unquote(disposition.split("filename*=UTF-8''", 1)[1]) == "测试漫画-久留美-card.zip"
    with zipfile.ZipFile(io.BytesIO(archive.data)) as bundle:
        assert sorted(bundle.namelist()) == sorted(_card_files())

    unknown_kind = client.get(f"{base}/character_jiu_liu_mei?kind=poster")
    assert unknown_kind.status_code == 422


def test_lorebook_routes_report_status_and_export_a_worldbook(tmp_path) -> None:
    platform = _platform(tmp_path)
    book_id = platform["book_id"]
    client = platform["client"]
    base = f"/api/v2/insight/books/{book_id}/lorebooks"

    assert client.get(f"{base}/status").get_json()["status"] == "idle"
    assert client.get(base).status_code == 404
    assert client.get(f"{base}/archive").status_code == 404

    without_key = client.post(base, json={})
    assert without_key.status_code == 422

    bad_limit = client.post(
        base,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={"maxEntities": 0},
    )
    assert bad_limit.status_code == 422

    with platform["engine"].begin() as connection:
        _insert_artifact(
            connection,
            book_id=book_id,
            kind=LOREBOOK_KIND,
            template="default",
            payload=json.dumps(
                {
                    "bookId": book_id,
                    "bookTitle": "测试漫画",
                    "files": {"lorebook.json": "{}", "lorebook.md": "# 世界书\n"},
                },
                ensure_ascii=False,
            ),
            revision=1,
        )

    status = client.get(f"{base}/status").get_json()
    assert status["status"] == "ready"
    assert status["kind"] == LOREBOOK_KIND

    archive = client.get(f"{base}/archive")
    assert archive.status_code == 200
    disposition = archive.headers["Content-Disposition"]
    assert unquote(disposition.split("filename*=UTF-8''", 1)[1]) == "测试漫画-worldbook.zip"
    with zipfile.ZipFile(io.BytesIO(archive.data)) as bundle:
        assert sorted(bundle.namelist()) == ["lorebook.json", "lorebook.md"]
