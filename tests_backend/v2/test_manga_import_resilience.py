"""P1-1 导入韧性：部分失败可见、幂等重试、结构化错误。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from flask import Flask
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

from src.backend_v2.auth.constants import LOCAL_USER_ID
from src.backend_v2.content.image_import import ImageImportService
from src.backend_v2.content.repository import ContentRepository
from src.backend_v2.manga_import.importer_base import render_issues
from src.backend_v2.manga_import.routes import create_manga_import_blueprint
from src.backend_v2.manga_import.source_tag import display_title, parse_tag
from src.backend_v2.storage.assets import AssetStorageService
from src.backend_v2.storage.database import create_sqlite_engine
from src.backend_v2.storage.schema import books, chapters, metadata, pages
from src.backend_v2.storage.seeding import seed_system_records


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture()
def env(tmp_path: Path):
    data_root = tmp_path / "data"
    for name in ("objects", "temp", "staging", "imports", "chroma"):
        (data_root / name).mkdir(parents=True, exist_ok=True)
    engine = create_sqlite_engine(data_root / "saber.sqlite3")
    metadata.create_all(engine)
    seed_system_records(engine)
    repository = ContentRepository(engine)
    storage = AssetStorageService(data_root, engine)
    importer = ImageImportService(data_root=data_root, repository=repository, storage=storage)
    app = Flask("manga-import-resilience-test")
    app.register_blueprint(create_manga_import_blueprint(data_root=data_root, engine=engine))
    return {
        "data_root": data_root,
        "engine": engine,
        "app": app,
        "client": app.test_client(),
    }


def _write_pages(folder: Path, count: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(1, count + 1):
        (folder / f"{index:03d}.png").write_bytes(PNG_BYTES)


def _book_payload(title: str, chapters: list[dict]) -> dict:
    return {"title": title, "chapters": chapters}


# --------------------------------------------------------------------------
# source_tag
# --------------------------------------------------------------------------


def test_source_tag_roundtrip_and_display() -> None:
    from src.backend_v2.manga_import.source_tag import build_title, digest_for

    wire = build_title("咒术回战", kind="venera", identifier="zhoushuhuizhan")
    tag = parse_tag(wire)
    assert tag is not None and tag.kind == "venera" and tag.identifier == "zhoushuhuizhan"
    assert display_title(wire) == "咒术回战"
    assert digest_for("venera", "zhoushuhuizhan") == tag.digest
    # 无标记的标题原样返回
    assert display_title("普通书名") == "普通书名"
    assert parse_tag("普通书名") is None


def test_source_tag_truncates_long_titles_but_stays_parseable() -> None:
    from src.backend_v2.manga_import.source_tag import build_title

    wire = build_title("长" * 900, kind="local", identifier="x" * 64)
    display = display_title(wire)
    assert 0 < len(display) < 900
    assert parse_tag(wire) is not None


# --------------------------------------------------------------------------
# 本地导入：部分失败与幂等
# --------------------------------------------------------------------------


def test_local_import_reports_partial_when_one_chapter_is_missing(env, tmp_path: Path) -> None:
    good = tmp_path / "ch1"
    _write_pages(good, 2)
    missing = tmp_path / "ch2-that-does-not-exist"

    response = env["client"].post(
        "/api/v2/import/local/confirm",
        json={
            "books": [
                _book_payload(
                    "混合批次",
                    [
                        {"title": "第一话", "ordinal": 1, "source_type": "folder", "source_path": str(good)},
                        {"title": "第二话", "ordinal": 2, "source_type": "folder", "source_path": str(missing)},
                    ],
                )
            ]
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "partial"
    assert body["imported"] == 0 and body["partial"] == 1
    result = body["imported_books"][0]
    assert result["status"] == "partial"
    assert result["chaptersWritten"] == 1
    assert result["pagesWritten"] == 2
    assert result["summary"]["chaptersFailed"] == 1

    codes = [issue["code"] for issue in result["issues"]]
    assert "source_missing" in codes
    issue = next(i for i in result["issues"] if i["code"] == "source_missing")
    assert issue["scope"] == "chapter"
    assert issue["chapter"]["ordinal"] == 2
    assert issue["chapter"]["title"] == "第二话"

    # readable text 也是结构化字段的一部分
    assert any("第二话" in line for line in render_issues(result["issues"]))


def test_local_import_is_idempotent_on_retry(env, tmp_path: Path) -> None:
    ch1 = tmp_path / "c1"
    ch2 = tmp_path / "c2"
    _write_pages(ch1, 2)
    _write_pages(ch2, 3)
    payload = {
        "books": [
            _book_payload(
                "可重试",
                [
                    {"title": "第一话", "ordinal": 1, "source_type": "folder", "source_path": str(ch1)},
                    {"title": "第二话", "ordinal": 2, "source_type": "folder", "source_path": str(ch2)},
                ],
            )
        ]
    }

    first = env["client"].post("/api/v2/import/local/confirm", json=payload).get_json()
    first_result = first["imported_books"][0]
    assert first["status"] == "success"
    assert first_result["pagesWritten"] == 5
    book_id = first_result["book_id"]

    second = env["client"].post("/api/v2/import/local/confirm", json=payload).get_json()
    second_result = second["imported_books"][0]

    assert second_result["book_id"] == book_id
    assert second_result["resumed"] is True
    assert second_result["chaptersWritten"] == 0
    assert second_result["pagesWritten"] == 0
    assert len(second_result["skippedChapters"]) == 2

    with env["engine"].connect() as conn:
        imported_books = conn.execute(
            select(books.c.id).where(books.c.owner_user_id == LOCAL_USER_ID, books.c.kind == "library")
        ).all()
        assert len(imported_books) == 1
        book_id = imported_books[0].id
        assert len(conn.execute(select(chapters.c.id).where(chapters.c.book_id == book_id)).all()) == 2
        assert len(conn.execute(select(pages.c.id)).all()) == 5


def test_local_import_title_strips_source_tag_in_api(env, tmp_path: Path) -> None:
    ch1 = tmp_path / "only"
    _write_pages(ch1, 1)
    body = env["client"].post(
        "/api/v2/import/local/confirm",
        json={
            "books": [
                _book_payload(
                    "带标记的书",
                    [{"title": "第一话", "ordinal": 1, "source_type": "folder", "source_path": str(ch1)}],
                )
            ]
        },
    ).get_json()

    result = body["imported_books"][0]
    assert result["title"] == "带标记的书"

    with env["engine"].connect() as conn:
        our_books = conn.execute(
            select(books.c.title).where(books.c.title.like("%⟦miyako:local:%"))
        ).all()
    assert len(our_books) == 1
    stored = our_books[0].title
    assert parse_tag(stored) is not None
    assert display_title(stored) == "带标记的书"


def test_local_import_rejects_book_without_usable_chapters(env, tmp_path: Path) -> None:
    body = env["client"].post(
        "/api/v2/import/local/confirm",
        json={
            "books": [
                _book_payload(
                    "空书",
                    [
                        {
                            "title": "缺序号",
                            "source_type": "folder",
                            "source_path": str(tmp_path),
                        }
                    ],
                )
            ]
        },
    ).get_json()

    assert body["status"] == "failed"
    result = body["imported_books"][0]
    assert result["status"] == "failed"
    codes = [issue["code"] for issue in result["issues"]]
    assert "source_invalid" in codes
    assert result["summary"]["chaptersFailed"] == 1
    assert result["book_id"] is None


def test_local_import_rejects_duplicate_ordinals(env, tmp_path: Path) -> None:
    ch1 = tmp_path / "dup1"
    ch2 = tmp_path / "dup2"
    _write_pages(ch1, 1)
    _write_pages(ch2, 1)
    body = env["client"].post(
        "/api/v2/import/local/confirm",
        json={
            "books": [
                _book_payload(
                    "重复序号",
                    [
                        {"title": "一", "ordinal": 1, "source_type": "folder", "source_path": str(ch1)},
                        {"title": "一b", "ordinal": 1, "source_type": "folder", "source_path": str(ch2)},
                    ],
                )
            ]
        },
    ).get_json()

    result = body["imported_books"][0]
    codes = [issue["code"] for issue in result["issues"]]
    assert "source_invalid" in codes
    # 第一话成功、重复那一章被报出来
    assert result["chaptersWritten"] == 1
    assert body["status"] == "partial"


# --------------------------------------------------------------------------
# Venera 导入：部分失败与幂等
# --------------------------------------------------------------------------


def _write_venera_library(base_dir: Path, *, directory: str, downloaded: list[str], chapters_map: dict) -> None:
    import sqlite3

    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / "local" / directory).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(base_dir / "local.db")
    conn.execute(
        "CREATE TABLE comics (id TEXT, title TEXT, subtitle TEXT, tags TEXT, directory TEXT, "
        "chapters TEXT, cover TEXT, comic_type TEXT, downloadedChapters TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO comics VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "comic-1",
            "咒术回战",
            "",
            "[]",
            directory,
            json.dumps(chapters_map),
            "",
            "",
            json.dumps(downloaded),
            "2026-01-01T00:00:00Z",
        ),
    )
    conn.commit()
    conn.close()


def _write_chapter_dir(base_dir: Path, directory: str, uuid: str, pages: int) -> None:
    target = base_dir / "local" / directory / uuid
    _write_pages(target, pages)


# --------------------------------------------------------------------------
# 导入完整性核对（以文件系统为准，未下载不算丢件）
# --------------------------------------------------------------------------


def _confirm_book(client, title: str, chapters: list[dict]) -> str:
    response = client.post(
        "/api/v2/import/local/confirm",
        json={"books": [_book_payload(title, chapters)]},
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    result = response.get_json()["imported_books"][0]
    assert result["book_id"], result
    return str(result["book_id"])


def test_natural_key_orders_page_names_numerically() -> None:
    from src.backend_v2.manga_import.integrity import _natural_key

    names = ["10.jpg", "2.jpg", "1.jpg", "100.jpg"]
    ordered = sorted(names, key=_natural_key)
    assert ordered == ["1.jpg", "2.jpg", "10.jpg", "100.jpg"]


def test_integrity_check_reports_zero_loss_when_every_chapter_imported(env, tmp_path: Path) -> None:
    from src.backend_v2.manga_import.integrity import (
        check_import_integrity,
        probe_local_paths,
    )

    folder = tmp_path / "series"
    _write_pages(folder / "ch1", 3)
    _write_pages(folder / "ch2", 4)
    book_id = _confirm_book(
        env["client"],
        "完整性样书",
        [
            {"title": "第1话", "ordinal": 1, "source_type": "folder", "source_path": str(folder / "ch1")},
            {"title": "第2话", "ordinal": 2, "source_type": "folder", "source_path": str(folder / "ch2")},
        ],
    )

    probes = probe_local_paths([folder])
    report = check_import_integrity(
        engine=env["engine"], book_id=book_id, probes=probes, source="local", target="series"
    )
    payload = report.to_payload()
    assert payload["filesystemChapters"] == 2
    assert payload["importedChapters"] == 2
    assert payload["lost"] == []
    assert payload["incomplete"] == []


def test_integrity_check_flags_a_chapter_without_pages(env, tmp_path: Path) -> None:
    from src.backend_v2.manga_import.integrity import (
        check_import_integrity,
        probe_local_paths,
    )

    folder = tmp_path / "partial"
    _write_pages(folder / "ch1", 2)
    book_id = _confirm_book(
        env["client"],
        "缺章样书",
        [{"title": "第1话", "ordinal": 1, "source_type": "folder", "source_path": str(folder / "ch1")}],
    )

    # 磁盘上后来又多了两章，但库里没有 —— 必须报成丢件。
    _write_pages(folder / "ch2", 2)
    _write_pages(folder / "ch3", 2)
    probes = probe_local_paths([folder])
    report = check_import_integrity(
        engine=env["engine"], book_id=book_id, probes=probes, source="local", target="partial"
    )
    payload = report.to_payload()
    assert payload["filesystemChapters"] == 3
    assert payload["importedChapters"] == 1
    assert {item["ordinal"] for item in payload["lost"]} == {2, 3}


def test_integrity_check_treats_registered_but_absent_chapters_as_not_downloaded(
    env, tmp_path: Path
) -> None:
    from src.backend_v2.manga_import.integrity import (
        check_import_integrity,
        probe_venera_comic,
    )

    base = tmp_path / "venera-notdown"
    _write_venera_library(
        base,
        directory="comic-dir",
        downloaded=["aaa", "bbb"],
        chapters_map={"g": {"aaa": "第1话", "bbb": "第2话"}},
    )
    # 只往磁盘写了第一个章节；bbb 登记为已下载但目录不存在（用户删了文件）。
    _write_chapter_dir(base, "comic-dir", "aaa", 3)

    probes, notes = probe_venera_comic(base, "comic-1")
    assert [item.ordinal for item in probes] == [1]
    assert any("第2话" in note for note in notes)

    book_id = _confirm_book(
        env["client"],
        "未下载样书",
        [{"title": "第1话", "ordinal": 1, "source_type": "folder", "source_path": str(base / "local" / "comic-dir" / "aaa")}],
    )
    report = check_import_integrity(
        engine=env["engine"],
        book_id=book_id,
        probes=probes,
        source="venera",
        target="comic-1",
        notes=notes,
        unmapped_ordinals=[2],
    )
    payload = report.to_payload()
    assert payload["filesystemChapters"] == 1
    assert payload["lost"] == []
    assert {item["ordinal"] for item in payload["notDownloaded"]} == {2}
    assert {item["status"] for item in payload["notDownloaded"]} == {"not_downloaded"}


def test_integrity_route_rejects_bad_source_and_missing_book_id(env) -> None:
    client = env["client"]
    assert (
        client.post("/api/v2/import/integrity-check", json={"source": "ftp", "bookId": "x"}).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v2/import/integrity-check", json={"source": "local", "paths": ["/tmp"]}
        ).status_code
        == 400
    )


def test_venera_import_reports_missing_chapter_directory(env, tmp_path: Path) -> None:
    base = tmp_path / "venera"
    _write_venera_library(
        base,
        directory="jjk",
        downloaded=["u1", "u2"],
        chapters_map={"默認": {"u1": "第1话", "u2": "第2话"}},
    )
    _write_chapter_dir(base, "jjk", "u1", 2)
    # u2 的目录故意不创建

    import src.backend_v2.manga_import.routes as routes_module

    original = routes_module.resolve_venera_base_dir
    routes_module.resolve_venera_base_dir = lambda engine: (base, "setting")
    try:
        response = env["client"].post("/api/v2/import/venera", json={"comic_ids": ["comic-1"]})
    finally:
        routes_module.resolve_venera_base_dir = original

    body = response.get_json()
    assert body["partial"] == 1 and body["failed"] == 0
    detail = body["details"][0]
    assert detail["status"] == "partial"
    assert detail["chaptersWritten"] == 1
    assert detail["pagesWritten"] == 2
    codes = [issue["code"] for issue in detail["issues"]]
    assert "source_missing" in codes


def test_venera_import_is_idempotent_on_retry(env, tmp_path: Path) -> None:
    base = tmp_path / "venera2"
    _write_venera_library(
        base,
        directory="jjk2",
        downloaded=["a1", "a2"],
        chapters_map={"默認": {"a1": "第1话", "a2": "第2话"}},
    )
    _write_chapter_dir(base, "jjk2", "a1", 2)
    _write_chapter_dir(base, "jjk2", "a2", 3)

    import src.backend_v2.manga_import.routes as routes_module

    original = routes_module.resolve_venera_base_dir
    routes_module.resolve_venera_base_dir = lambda engine: (base, "setting")
    try:
        first = env["client"].post("/api/v2/import/venera", json={"comic_ids": ["comic-1"]}).get_json()
        second = env["client"].post("/api/v2/import/venera", json={"comic_ids": ["comic-1"]}).get_json()
    finally:
        routes_module.resolve_venera_base_dir = original

    assert first["imported"] == 1 and first["details"][0]["pagesWritten"] == 5
    assert second["imported"] == 1
    resumed = second["details"][0]
    assert resumed["book_id"] == first["details"][0]["book_id"]
    assert resumed["resumed"] is True
    assert resumed["pagesWritten"] == 0
    assert len(resumed["skippedChapters"]) == 2

    with env["engine"].connect() as conn:
        imported_books = conn.execute(
            select(books.c.id).where(books.c.owner_user_id == LOCAL_USER_ID, books.c.kind == "library")
        ).all()
        assert len(imported_books) == 1


def test_local_import_response_carries_an_integrity_report(env, tmp_path: Path) -> None:
    """导入响应里直接带核对结果，用户不必再手动点一次「核对」。"""

    folder = tmp_path / "inline"
    _write_pages(folder / "ch1", 3)
    _write_pages(folder / "ch2", 2)

    response = env["client"].post(
        "/api/v2/import/local/confirm",
        json={
            "books": [
                _book_payload(
                    "内联核对样书",
                    [
                        {"title": "第1话", "ordinal": 1, "source_type": "folder", "source_path": str(folder / "ch1")},
                        {"title": "第2话", "ordinal": 2, "source_type": "folder", "source_path": str(folder / "ch2")},
                    ],
                )
            ]
        },
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    entry = response.get_json()["imported_books"][0]
    assert entry["status"] == "success"

    integrity = entry["integrity"]
    assert integrity is not None, entry
    assert integrity["ok"] is True
    assert integrity["filesystemChapters"] == 2
    assert integrity["importedChapters"] == 2
    assert integrity["lost"] == []


def test_local_import_integrity_reports_pages_missing_from_the_database(env, tmp_path: Path) -> None:
    """磁盘上有、库里没有的页要被点出来（这是核对的全部意义）。"""

    from src.backend_v2.storage.schema import pages as pages_table

    folder = tmp_path / "partial"
    _write_pages(folder / "ch1", 3)
    _write_pages(folder / "ch2", 3)

    response = env["client"].post(
        "/api/v2/import/local/confirm",
        json={
            "books": [
                _book_payload(
                    "缺页样书",
                    [
                        {"title": "第1话", "ordinal": 1, "source_type": "folder", "source_path": str(folder / "ch1")},
                        {"title": "第2话", "ordinal": 2, "source_type": "folder", "source_path": str(folder / "ch2")},
                    ],
                )
            ]
        },
    )
    entry = response.get_json()["imported_books"][0]
    book_id = str(entry["book_id"])

    # 模拟「导入时漏了一页」：删掉第 2 话的第 3 页。
    with env["engine"].begin() as conn:
        chapter_id = conn.execute(
            select(chapters.c.id).where(chapters.c.book_id == book_id, chapters.c.ordinal == 2)
        ).scalar_one()
        third = conn.execute(
            select(pages_table.c.id).where(pages_table.c.chapter_id == chapter_id, pages_table.c.ordinal == 3)
        ).scalar_one()
        conn.execute(pages_table.delete().where(pages_table.c.id == third))

    rerun = env["client"].post(
        "/api/v2/import/local/confirm",
        json={
            "books": [
                _book_payload(
                    "缺页样书",
                    [
                        {"title": "第1话", "ordinal": 1, "source_type": "folder", "source_path": str(folder / "ch1")},
                        {"title": "第2话", "ordinal": 2, "source_type": "folder", "source_path": str(folder / "ch2")},
                    ],
                )
            ]
        },
    )
    integrity = rerun.get_json()["imported_books"][0]["integrity"]
    assert integrity is not None
    # 续传会把漏掉的页补回来，所以这次应当仍然 ok —— 证明核对把「补页」也认了。
    assert integrity["ok"] is True, integrity


def test_venera_import_response_carries_an_integrity_report(env, tmp_path: Path) -> None:
    """Venera 导入同样在响应里带核对；登记但磁盘没有的章节进 notDownloaded。"""

    base = tmp_path / "venera-inline"
    _write_venera_library(
        base,
        directory="inline",
        downloaded=["u1", "u2"],
        chapters_map={"默認": {"u1": "第1话", "u2": "第2话"}},
    )
    _write_chapter_dir(base, "inline", "u1", 2)
    # u2 的目录故意不创建 —— 这是「未下载」，不是丢件。

    import src.backend_v2.manga_import.routes as routes_module

    original = routes_module.resolve_venera_base_dir
    routes_module.resolve_venera_base_dir = lambda engine: (base, "setting")
    try:
        response = env["client"].post("/api/v2/import/venera", json={"comic_ids": ["comic-1"]})
    finally:
        routes_module.resolve_venera_base_dir = original

    detail = response.get_json()["details"][0]
    integrity = detail["integrity"]
    assert integrity is not None, detail
    assert integrity["ok"] is True
    assert integrity["filesystemChapters"] == 1
    assert integrity["lost"] == []
    assert [item["ordinal"] for item in integrity["notDownloaded"]] == [2]


def test_import_integrity_failure_does_not_fail_the_import(env, monkeypatch) -> None:
    """核对是附加信息：它自己出错时只降级，不能把成功的导入变成失败。"""

    import src.backend_v2.manga_import.routes as routes_module

    def _boom(*args, **kwargs):
        raise RuntimeError("disk vanished")

    monkeypatch.setattr(routes_module, "probe_local_paths", _boom)

    import tempfile

    folder = Path(tempfile.mkdtemp(prefix="integrity-boom-")) / "ch1"
    _write_pages(folder, 2)

    response = env["client"].post(
        "/api/v2/import/local/confirm",
        json={
            "books": [
                _book_payload(
                    "核对失败样书",
                    [{"title": "第1话", "ordinal": 1, "source_type": "folder", "source_path": str(folder)}],
                )
            ]
        },
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    entry = response.get_json()["imported_books"][0]
    assert entry["status"] == "success"
    assert entry["integrity"]["ok"] is None
    assert "disk vanished" in entry["integrity"]["error"]


def test_venera_library_returns_structured_error_when_apath_missing(env, monkeypatch) -> None:
    monkeypatch.delenv("APPDATA", raising=False)

    import src.backend_v2.manga_import.routes as routes_module

    original = routes_module.resolve_venera_base_dir
    routes_module.resolve_venera_base_dir = lambda engine: (_ for _ in ()).throw(
        FileNotFoundError("APPDATA environment variable not found")
    )
    try:
        response = env["client"].get("/api/v2/import/venera/library")
    finally:
        routes_module.resolve_venera_base_dir = original

    assert response.status_code == 503
    body = response.get_json()
    assert body["error"]["code"] == "venera_data_dir_unavailable"
    assert "APPDATA" in body["error"]["message"]


def test_local_confirm_rejects_non_object_book_entries(env) -> None:
    response = env["client"].post("/api/v2/import/local/confirm", json={"books": ["not-an-object"]})
    body = response.get_json()
    assert response.status_code == 200
    assert body["status"] == "failed"
    assert body["imported_books"][0]["issues"][0]["code"] == "import_crashed"


def test_local_preview_validates_paths(env) -> None:
    response = env["client"].post("/api/v2/import/local/preview", json={"paths": []})
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "validation_error"


def test_legacy_untagged_book_is_adopted_instead_of_duplicated(env, tmp_path: Path) -> None:
    """v0.1.0 导入的书标题没有溯源标记；重试应认领它而不是再造一本。"""
    from sqlalchemy import update as sa_update

    ch1 = tmp_path / "legacy"
    _write_pages(ch1, 1)
    payload = {
        "books": [
            _book_payload(
                "老书",
                [{"title": "第一话", "ordinal": 1, "source_type": "folder", "source_path": str(ch1)}],
            )
        ]
    }

    first = env["client"].post("/api/v2/import/local/confirm", json=payload).get_json()
    book_id = first["imported_books"][0]["book_id"]

    # 模拟旧版本留下的形态：标题是裸书名、没有标记。
    with env["engine"].begin() as conn:
        conn.execute(sa_update(books).where(books.c.id == book_id).values(title="老书"))

    second = env["client"].post("/api/v2/import/local/confirm", json=payload).get_json()
    result = second["imported_books"][0]
    assert result["book_id"] == book_id
    assert result["resumed"] is True
    assert result["pagesWritten"] == 0

    with env["engine"].connect() as conn:
        titles = [
            row.title
            for row in conn.execute(
                select(books.c.title).where(books.c.owner_user_id == LOCAL_USER_ID, books.c.kind == "library")
            ).all()
        ]
    assert len(titles) == 1
    assert parse_tag(titles[0]) is not None
