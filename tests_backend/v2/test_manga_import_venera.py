"""漫画导入：Venera 数据目录设置与封面读取。

这些用例覆盖两件事：
1. Venera 数据目录既可以是系统默认位置，也可以是用户在前端设置里填的目录；
2. 前端需要的封面字节必须由后端提供（Venera 的 cover 只是 local.db 里的相对路径）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from flask import Flask
from sqlalchemy import select, update

from src.backend_v2.manga_import.routes import create_manga_import_blueprint
from src.backend_v2.settings.resolver import SettingsResolver
from src.backend_v2.settings.validation import validate_setting_payload
from src.backend_v2.storage.database import create_sqlite_engine
from src.backend_v2.storage.schema import app_settings, metadata
from src.backend_v2.storage.seeding import seed_system_records

_COVER_BYTES = b"\xff\xd8\xff\xe0venera-cover-bytes"


def _write_venera_library(base_dir: Path, *, directory: str, cover: str = "cover.jpg") -> None:
    """在 base_dir 下造一个 Venera 库：local.db + local/<directory>/<cover>。"""
    base_dir.mkdir(parents=True, exist_ok=True)
    comic_dir = base_dir / "local" / directory
    comic_dir.mkdir(parents=True, exist_ok=True)
    (comic_dir / cover).write_bytes(_COVER_BYTES)
    connection = sqlite3.connect(base_dir / "local.db")
    try:
        connection.execute(
            "CREATE TABLE comics ("
            "id TEXT, title TEXT, subtitle TEXT, tags TEXT, directory TEXT, "
            "chapters TEXT, cover TEXT, comic_type TEXT, downloadedChapters TEXT, "
            "created_at TEXT)"
        )
        connection.execute(
            "INSERT INTO comics VALUES "
            "('comic-1', '测试漫画', '', '[]', ?, '[]', ?, '0', '[]', '0')",
            (directory, cover),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.fixture()
def venera_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """默认 APPDATA 指向临时目录，避免测试读真实 Venera 安装。"""
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    monkeypatch.setenv("APPDATA", str(appdata))
    default_base = appdata / "com.github.wgh136" / "venera"

    data_root = tmp_path / "data"
    data_root.mkdir()
    engine = create_sqlite_engine(data_root / "saber.sqlite3")
    metadata.create_all(engine)
    seed_system_records(engine)

    app = Flask("manga-import-venera-test")
    app.register_blueprint(
        create_manga_import_blueprint(data_root=data_root, engine=engine)
    )
    return {
        "app": app,
        "client": app.test_client(),
        "engine": engine,
        "appdata": appdata,
        "default_base": default_base,
        "tmp_path": tmp_path,
    }


def _save_base_dir(engine, value: str) -> None:
    payload = {"veneraBaseDir": value}
    validate_setting_payload("manga_import", payload)
    with engine.begin() as connection:
        changed = connection.execute(
            update(app_settings)
            .where(app_settings.c.domain == "manga_import")
            .values(payload_json=json.dumps(payload))
        ).rowcount
    assert changed == 1


def test_manga_import_settings_are_seeded_and_validated(venera_platform) -> None:
    engine = venera_platform["engine"]
    with engine.connect() as connection:
        domains = set(connection.execute(select(app_settings.c.domain)).scalars())
    assert "manga_import" in domains

    assert SettingsResolver(engine).resolve_manga_import()["venera_base_dir"] == ""

    for broken in (
        {"veneraBaseDir": 1},
        {"veneraBaseDir": "a\x00b"},
        {"unexpected": "x"},
        {},
    ):
        with pytest.raises(ValueError):
            validate_setting_payload("manga_import", broken)


def test_library_reports_the_default_directory_and_cover_availability(
    venera_platform,
) -> None:
    _write_venera_library(venera_platform["default_base"], directory="测试漫画")
    client = venera_platform["client"]

    response = client.get("/api/v2/import/venera/library")
    assert response.status_code == 200
    body = response.get_json()
    assert body["veneraFound"] is True
    assert body["baseDirSource"] == "default"
    assert Path(body["baseDir"]) == venera_platform["default_base"]
    assert body["total"] == 1
    assert body["comics"][0]["hasCover"] is True
    assert body["comics"][0]["coverPath"] == "local/测试漫画/cover.jpg"


def test_configured_directory_wins_over_the_default_one(venera_platform) -> None:
    custom_base = venera_platform["tmp_path"] / "custom-venera"
    _write_venera_library(custom_base, directory="自定义漫画")
    _save_base_dir(venera_platform["engine"], str(custom_base))

    body = venera_platform["client"].get("/api/v2/import/venera/library").get_json()
    assert body["baseDirSource"] == "setting"
    assert Path(body["baseDir"]) == custom_base
    assert [comic["directory"] for comic in body["comics"]] == ["自定义漫画"]
    assert body["comics"][0]["hasCover"] is True


def test_setting_also_accepts_the_local_subdirectory(venera_platform) -> None:
    custom_base = venera_platform["tmp_path"] / "custom-venera"
    _write_venera_library(custom_base, directory="自定义漫画")
    _save_base_dir(venera_platform["engine"], str(custom_base / "local"))

    body = venera_platform["client"].get("/api/v2/import/venera/library").get_json()
    assert Path(body["baseDir"]) == custom_base
    assert body["total"] == 1


def test_cover_endpoint_streams_the_image_bytes(venera_platform) -> None:
    _write_venera_library(venera_platform["default_base"], directory="测试漫画")
    response = venera_platform["client"].get(
        "/api/v2/import/venera/cover", query_string={"comicId": "comic-1"}
    )
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("image/jpeg")
    assert response.headers["Cache-Control"] == "private, max-age=3600"
    assert response.data == _COVER_BYTES


def test_cover_endpoint_reports_missing_inputs_and_missing_files(venera_platform) -> None:
    _write_venera_library(venera_platform["default_base"], directory="测试漫画")
    client = venera_platform["client"]

    assert client.get("/api/v2/import/venera/cover").status_code == 400
    assert (
        client.get(
            "/api/v2/import/venera/cover", query_string={"comicId": "unknown"}
        ).status_code
        == 404
    )

    (venera_platform["default_base"] / "local" / "测试漫画" / "cover.jpg").unlink()
    response = client.get(
        "/api/v2/import/venera/cover", query_string={"comicId": "comic-1"}
    )
    assert response.status_code == 404
    # P1-2：错误体统一为 {"error": {"code", "message"}}，不再返回扁平字符串。
    assert response.get_json()["error"]["code"] == "not_found"


def test_library_and_cover_reject_directory_traversal(venera_platform) -> None:
    base = venera_platform["default_base"]
    _write_venera_library(base, directory="../../outside")
    client = venera_platform["client"]

    body = client.get("/api/v2/import/venera/library").get_json()
    assert body["total"] == 1
    assert body["comics"][0]["hasCover"] is False
    assert (
        client.get(
            "/api/v2/import/venera/cover", query_string={"comicId": "comic-1"}
        ).status_code
        == 404
    )
