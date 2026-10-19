from __future__ import annotations

from datetime import datetime
from io import BytesIO
import json
from pathlib import Path
import uuid

from PIL import Image, ImageFont
import pytest
from sqlalchemy import insert, select, update

from src.backend_v2.api.app import ApiSettings, create_api_app
from src.backend_v2.content.image_import import ImageImportService, ImportSafetyLimits
from src.backend_v2.content.page_style import resolve_new_page_style, rgb_to_hex
from src.backend_v2.content.repository import (
    ContentConflict,
    ContentLocked,
    ContentNotFound,
    ContentRepository,
    IdempotencyConflict,
)
from src.backend_v2.runtime_identity import RuntimeIdentity
from src.backend_v2.runtime_profile import PROFILE_ENV
from src.backend_v2.storage.assets import AssetQuotaExceeded, AssetStorageService
from src.backend_v2.storage.font_files import (
    bundled_font_files,
)
from src.backend_v2.storage.database import create_sqlite_engine
from src.backend_v2.storage.defaults import (
    DEFAULT_FONT_ID,
    DEFAULT_TEXT_STYLE,
)
from src.backend_v2.storage.schema import (
    app_settings,
    assets,
    books,
    chapter_write_locks,
    chapters,
    fonts,
    jobs,
    metadata,
    operations,
    page_assets,
    pages,
    platform_config,
)
from src.backend_v2.storage.seeding import seed_system_records
from src.backend_v2.storage.seeding import QUICK_WORKSPACE_BOOK_ID


def _page_identity(page: dict[str, object]) -> tuple[object, ...]:
    return (
        page["logicalSourcePath"],
        page["sourceRevision"],
        page["documentRevision"],
        page["width"],
        page["height"],
    )


def _stored_job_progress(status: str = "queued") -> str:
    return json.dumps(
        {
            "executionMode": "sequential",
            "jobStatus": status,
            "totalItems": 0,
            "completedItems": 0,
            "failedItems": 0,
            "skippedItems": 0,
            "cancelledItems": 0,
            "pools": [],
        },
        separators=(",", ":"),
    )


@pytest.fixture()
def content_platform(tmp_path: Path):
    data_root = tmp_path / "data-v2"
    data_root.mkdir()
    engine = create_sqlite_engine(data_root / "saber.sqlite3")
    metadata.create_all(engine)
    seed_system_records(engine)
    repository = ContentRepository(engine)
    book = repository.create_book(title="Book")
    chapter = repository.create_chapter(book_id=str(book["id"]), title="Chapter")
    storage = AssetStorageService(data_root, engine)
    importer = ImageImportService(
        data_root=data_root,
        repository=repository,
        storage=storage,
    )
    try:
        yield data_root, engine, repository, storage, importer, book, chapter
    finally:
        engine.dispose()


def _image_bytes(
    size: tuple[int, int],
    *,
    image_format: str = "PNG",
    color: tuple[int, int, int] = (20, 40, 60),
) -> bytes:
    output = BytesIO()
    with Image.new("RGB", size, color) as image:
        image.save(output, format=image_format)
    return output.getvalue()


def test_large_image_policy_has_no_byte_or_pixel_dimension_gate() -> None:
    limits = ImportSafetyLimits()

    assert Image.MAX_IMAGE_PIXELS is None
    assert not hasattr(limits, "max_image_bytes")
    assert not hasattr(limits, "max_container_bytes")
    assert not hasattr(limits, "max_expanded_bytes")
    assert not hasattr(limits, "max_archive_entries")
    assert not hasattr(limits, "max_container_pages")
    assert not hasattr(limits, "max_html_bytes")


def test_public_image_upload_stops_at_the_current_asset_budget(
    content_platform,
    monkeypatch,
) -> None:
    data_root, engine, repository, storage, _importer, _book, _chapter = (
        content_platform
    )
    with engine.begin() as connection:
        connection.execute(
            update(platform_config)
            .where(platform_config.c.singleton_id == 1)
            .values(asset_quota_bytes=128)
        )
    monkeypatch.setenv(PROFILE_ENV, "public")
    importer = ImageImportService(
        data_root=data_root,
        repository=repository,
        storage=storage,
        limits=ImportSafetyLimits(stream_chunk_bytes=256),
    )
    source = BytesIO(b"x" * 1024)

    with pytest.raises(AssetQuotaExceeded):
        importer.publish_standalone_image(source)

    assert source.tell() == 256
    assert not list((data_root / "temp" / "imports").glob("*.upload"))


def test_local_image_upload_ignores_the_public_asset_budget(
    content_platform,
    monkeypatch,
) -> None:
    data_root, engine, repository, storage, _importer, _book, _chapter = (
        content_platform
    )
    with engine.begin() as connection:
        connection.execute(
            update(platform_config)
            .where(platform_config.c.singleton_id == 1)
            .values(asset_quota_bytes=1)
        )
    monkeypatch.setenv(PROFILE_ENV, "local")
    importer = ImageImportService(
        data_root=data_root,
        repository=repository,
        storage=storage,
        limits=ImportSafetyLimits(stream_chunk_bytes=64),
    )
    payload = _image_bytes((20, 20))
    source, thumbnail = importer.publish_standalone_image(BytesIO(payload))

    assert source.byte_size == len(payload)
    assert thumbnail.byte_size > 1


@pytest.mark.parametrize(
    "value",
    [None, [], [0, 0], [0, 0, 0, 0], [0, 0, "0"], [0, 0, True], [-1, 0, 0]],
)
def test_rgb_color_rejects_malformed_algorithm_results(value: object) -> None:
    with pytest.raises(ValueError, match="RGB color"):
        rgb_to_hex(value)


def _import(
    _repository: ContentRepository,
    importer: ImageImportService,
    *,
    chapter_id: str,
    payload: bytes,
    logical_path: str,
    key: str,
    text_style: dict[str, object] | None = None,
):
    return importer.import_page(
        chapter_id=chapter_id,
        logical_path=logical_path,
        text_style=(
            dict(DEFAULT_TEXT_STYLE)
            if text_style is None
            else text_style
        ),
        upload=BytesIO(payload),
        idempotency_key=key,
    )


def test_server_info_uses_the_configured_v2_api_port(content_platform) -> None:
    data_root, engine, *_rest = content_platform
    app = create_api_app(
        ApiSettings(
            data_root=data_root,
            identity=RuntimeIdentity(
                epoch_id="test-api",
                epoch_token="test-token",
                test_mode=True,
            ),
            engine=engine,
            host="0.0.0.0",
            port=5123,
        )
    )

    response = app.test_client().get("/api/v2/system/server-info")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["host"] == "0.0.0.0"
    assert payload["port"] == 5123
    assert payload["lanUrl"].endswith(":5123")


def test_server_info_does_not_advertise_lan_when_bound_to_loopback(
    content_platform,
) -> None:
    data_root, engine, *_rest = content_platform
    app = create_api_app(
        ApiSettings(
            data_root=data_root,
            identity=RuntimeIdentity(
                epoch_id="loopback-server-info-api",
                epoch_token="test-token",
                test_mode=True,
            ),
            engine=engine,
            host="127.0.0.1",
            port=5123,
        )
    )
    try:
        response = app.test_client().get("/api/v2/system/server-info")
    finally:
        app.extensions["saber_v2_runtime"].close()

    assert response.status_code == 200
    assert response.get_json()["lanUrl"] is None


def test_book_creation_rejects_retired_request_fields(content_platform) -> None:
    data_root, engine, *_rest = content_platform
    app = create_api_app(
        ApiSettings(
            data_root=data_root,
            identity=RuntimeIdentity(
                epoch_id="test-strict-book-api",
                epoch_token="test-token",
                test_mode=True,
            ),
            engine=engine,
        )
    )
    try:
        client = app.test_client()
        response = client.post(
            "/api/v2/books",
            headers={"Idempotency-Key": "book-with-retired-field"},
            json={"title": "Book", "description": "retired"},
        )
        multipart_response = client.post(
            "/api/v2/books",
            headers={"Idempotency-Key": "book-with-retired-form-field"},
            data={"title": "Book", "description": "retired"},
            content_type="multipart/form-data",
        )
    finally:
        app.extensions["saber_v2_runtime"].close()

    assert response.status_code == 422
    assert "description" in response.get_data(as_text=True)
    assert multipart_response.status_code == 422
    assert "form.description" in multipart_response.get_data(as_text=True)


def test_content_name_commands_reject_non_string_values(content_platform) -> None:
    data_root, engine, repository, _storage, _importer, book, chapter = (
        content_platform
    )
    tag = repository.create_tag(name="Strict", color="#4466aa")
    app = create_api_app(
        ApiSettings(
            data_root=data_root,
            identity=RuntimeIdentity(
                epoch_id="test-strict-content-names",
                epoch_token="test-token",
                test_mode=True,
            ),
            engine=engine,
        )
    )
    try:
        client = app.test_client()
        responses = [
            client.post(
                "/api/v2/books",
                headers={"Idempotency-Key": "invalid-book-title-type"},
                json={"title": 123},
            ),
            client.post(
                f"/api/v2/books/{book['id']}/chapters",
                headers={"Idempotency-Key": "invalid-chapter-title-type"},
                json={"title": 123},
            ),
            client.put(
                f"/api/v2/chapters/{chapter['id']}",
                headers={"Idempotency-Key": "invalid-chapter-update-type"},
                json={"title": 123},
            ),
            client.post(
                "/api/v2/tags",
                headers={"Idempotency-Key": "invalid-tag-name-type"},
                json={"name": 123, "color": "#4466aa"},
            ),
            client.put(
                f"/api/v2/tags/{tag['id']}",
                headers={"Idempotency-Key": "invalid-tag-color-type"},
                json={"name": "Strict", "color": 123},
            ),
        ]
    finally:
        app.extensions["saber_v2_runtime"].close()

    assert [response.status_code for response in responses] == [422] * 5


def test_book_detail_and_list_serialize_sqlite_timestamps_as_utc(content_platform) -> None:
    _data_root, engine, repository, _storage, _importer, book, _chapter = (
        content_platform
    )
    with engine.begin() as connection:
        connection.execute(
            update(books).where(books.c.id == book["id"]).values(
                created_at=datetime(2026, 9, 24, 11, 31, 18),
                updated_at=datetime(2026, 9, 25, 0, 15, 30),
            )
        )

    summary = next(
        item for item in repository.list_books() if item["id"] == book["id"]
    )
    detail = repository.get_book(str(book["id"]))

    for result in (summary, detail):
        assert result["createdAt"] == "2026-09-24T11:31:18Z"
        assert result["updatedAt"] == "2026-09-25T00:15:30Z"


def test_book_search_matches_titles_and_tag_names(content_platform) -> None:
    _data_root, _engine, repository, _storage, _importer, _book, _chapter = (
        content_platform
    )
    fantasy = repository.create_tag(name="Fantasy", color="#4466aa")
    tagged = repository.create_book(
        title="Unrelated title",
        tag_ids=[str(fantasy["id"])],
    )
    titled = repository.create_book(title="Fantasy collection")

    matched_ids = {
        str(book["id"])
        for book in repository.list_books(search="fAnTaSy")
    }

    assert matched_ids == {str(tagged["id"]), str(titled["id"])}


def test_book_update_only_changes_fields_present_in_the_request(
    content_platform,
) -> None:
    data_root, engine, repository, _storage, _importer, book, _chapter = (
        content_platform
    )
    tag = repository.create_tag(name="Fantasy", color="#4466aa")
    app = create_api_app(
        ApiSettings(
            data_root=data_root,
            identity=RuntimeIdentity(
                epoch_id="test-partial-book-update",
                epoch_token="test-token",
                test_mode=True,
            ),
            engine=engine,
        )
    )
    try:
        client = app.test_client()
        response = client.put(
            f"/api/v2/books/{book['id']}",
            headers={"Idempotency-Key": "partial-book-update"},
            json={"tagIds": [tag["id"]]},
        )
        empty_response = client.put(
            f"/api/v2/books/{book['id']}",
            headers={"Idempotency-Key": "empty-book-update"},
            json={},
        )
        invalid_title_response = client.put(
            f"/api/v2/books/{book['id']}",
            headers={"Idempotency-Key": "invalid-book-title"},
            json={"title": 123},
        )
    finally:
        app.extensions["saber_v2_runtime"].close()

    assert response.status_code == 200
    updated = response.get_json()
    assert updated["title"] == book["title"]
    assert [item["name"] for item in updated["tags"]] == ["Fantasy"]
    assert empty_response.status_code == 422
    assert invalid_title_response.status_code == 422


def test_tag_rename_conflict_is_reported_as_a_domain_conflict(content_platform) -> None:
    _data_root, _engine, repository, _storage, _importer, _book, _chapter = (
        content_platform
    )
    first = repository.create_tag(name="First", color="#4466aa")
    repository.create_tag(name="Second", color="#aa6644")

    with pytest.raises(ContentConflict, match="tag name already exists"):
        repository.update_tag(
            tag_id=str(first["id"]),
            name="Second",
            color="#ffffff",
        )


def test_new_page_style_rejects_invalid_setting_values(content_platform) -> None:
    _root, engine, _repository, _storage, _importer, _book, _chapter = (
        content_platform
    )
    with engine.begin() as connection:
        connection.execute(
            update(app_settings)
            .where(app_settings.c.domain == "text_style_defaults")
            .values(payload_json='{"fontFamily": false}')
        )

    with engine.connect() as connection:
        with pytest.raises(ValueError):
            resolve_new_page_style(connection)


def test_page_import_publishes_source_and_webp_thumbnail_without_base64(
    content_platform,
) -> None:
    data_root, engine, repository, storage, importer, _book, chapter = content_platform
    result, replayed = _import(
        repository,
        importer,
        chapter_id=str(chapter["id"]),
        payload=_image_bytes((1200, 800)),
        logical_path="folder/page 1.png",
        key="page-1",
    )
    assert not replayed
    page = result["page"]
    assert page["width"] == 1200 and page["height"] == 800
    assert page["sourceUrl"].startswith("/api/v2/assets/")
    assert page["thumbnailSourceUrl"].startswith("/api/v2/assets/")
    assert "base64" not in str(result).lower()

    with engine.connect() as connection:
        asset_rows = list(
            connection.execute(
                select(
                    page_assets.c.role,
                    assets.c.relative_path,
                    assets.c.width,
                    assets.c.height,
                    assets.c.mime_type,
                )
                .join(assets, assets.c.id == page_assets.c.asset_id)
                .where(page_assets.c.page_id == page["id"])
                .order_by(page_assets.c.role)
            )
        )
    assert {row.role for row in asset_rows} == {"source", "thumbnail_source"}
    thumbnail = next(row for row in asset_rows if row.role == "thumbnail_source")
    assert thumbnail.mime_type == "image/webp"
    assert (thumbnail.width, thumbnail.height) == (320, 213)
    with Image.open(storage.resolve_relative_path(thumbnail.relative_path)) as decoded:
        assert decoded.format == "WEBP"
        assert decoded.size == (320, 213)


def test_page_import_api_materializes_the_submitted_style_and_replays(
    content_platform,
) -> None:
    data_root, engine, repository, _storage, _importer, _book, chapter = content_platform
    app = create_api_app(
        ApiSettings(
            data_root=data_root,
            identity=RuntimeIdentity(
                epoch_id="test-api",
                epoch_token="test-only",
                test_mode=True,
            ),
            engine=engine,
        )
    )
    client = app.test_client()
    chapter_id = str(chapter["id"])
    payload = _image_bytes((48, 72))
    text_style = {
        **DEFAULT_TEXT_STYLE,
        "fontSize": 37,
    }

    first = client.post(
        f"/api/v2/chapters/{chapter_id}/pages",
        data={
            "file": (BytesIO(payload), "api.png"),
            "logicalPath": "api.png",
            "textStyle": json.dumps(text_style),
        },
        headers={"Idempotency-Key": "api-page-stable-key"},
    )
    replay = client.post(
        f"/api/v2/chapters/{chapter_id}/pages",
        data={
            "file": (BytesIO(payload), "api.png"),
            "logicalPath": "api.png",
            "textStyle": json.dumps(text_style),
        },
        headers={"Idempotency-Key": "api-page-stable-key"},
    )

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.get_json() == first.get_json()
    assert len(repository.list_pages(chapter_id=chapter_id, all_pages=True)["items"]) == 1
    page_id = first.get_json()["page"]["id"]
    with engine.connect() as connection:
        stored = connection.execute(
            select(pages.c.default_font_id, pages.c.page_style_defaults_json).where(
                pages.c.id == page_id
            )
        ).one()
    assert stored.default_font_id == text_style["fontFamily"]
    assert json.loads(stored.page_style_defaults_json) == {
        key: value for key, value in text_style.items() if key != "fontFamily"
    }


def test_page_import_api_requires_a_complete_text_style(content_platform) -> None:
    data_root, engine, repository, _storage, _importer, _book, chapter = content_platform
    app = create_api_app(
        ApiSettings(
            data_root=data_root,
            identity=RuntimeIdentity(
                epoch_id="test-page-style-api",
                epoch_token="test-only",
                test_mode=True,
            ),
            engine=engine,
        )
    )
    response = app.test_client().post(
        f"/api/v2/chapters/{chapter['id']}/pages",
        data={
            "file": (BytesIO(_image_bytes((48, 72))), "api.png"),
            "logicalPath": "api.png",
        },
        headers={"Idempotency-Key": "api-page-missing-style"},
    )

    assert response.status_code == 422
    assert response.get_json()["error"]["message"] == (
        "multipart field 'textStyle' must be a JSON object"
    )
    assert repository.list_pages(
        chapter_id=str(chapter["id"]),
        all_pages=True,
    )["items"] == []


def test_page_import_materializes_the_submitted_font_as_a_foreign_key(
    content_platform,
) -> None:
    _root, engine, repository, _storage, importer, _book, chapter = content_platform
    font_id = "10000000-0000-0000-0000-000000000001"
    with engine.begin() as connection:
        connection.execute(
            insert(fonts).values(
                id=font_id,
                display_name="测试字体",
                owner_user_id=None,
                relative_path="fonts/shared/test-font.ttf",
            )
        )
        style = json.loads(
            connection.execute(
                select(app_settings.c.payload_json).where(
                    app_settings.c.domain == "text_style_defaults"
                )
            ).scalar_one()
        )
        style["fontFamily"] = font_id
        style["layoutDirection"] = "horizontal"
        style["inpaintMethod"] = "litelama"

    result, _replayed = _import(
        repository,
        importer,
        chapter_id=str(chapter["id"]),
        payload=_image_bytes((64, 96)),
        logical_path="font-default.png",
        key="font-default-import",
        text_style=style,
    )
    page_id = str(result["page"]["id"])

    with engine.connect() as connection:
        stored = connection.execute(
            select(
                pages.c.default_font_id,
                pages.c.page_style_defaults_json,
            ).where(pages.c.id == page_id)
        ).mappings().one()
    page_style = json.loads(stored["page_style_defaults_json"])
    assert stored["default_font_id"] == font_id
    assert "fontFamily" not in page_style
    assert page_style["layoutDirection"] == "horizontal"
    assert page_style["inpaintMethod"] == "litelama"


def test_long_strip_thumbnail_uses_width_cap_and_top_crop(content_platform) -> None:
    _root, engine, repository, _storage, importer, _book, chapter = content_platform
    result, _ = _import(
        repository,
        importer,
        chapter_id=str(chapter["id"]),
        payload=_image_bytes((500, 5000)),
        logical_path="webtoon.png",
        key="webtoon",
    )
    with engine.connect() as connection:
        thumbnail_id = connection.execute(
            select(page_assets.c.asset_id).where(
                page_assets.c.page_id == result["page"]["id"],
                page_assets.c.role == "thumbnail_source",
            )
        ).scalar_one()
        dimensions = connection.execute(
            select(assets.c.width, assets.c.height).where(assets.c.id == thumbnail_id)
        ).one()
    assert dimensions == (320, 1280)


def test_import_is_idempotent_and_duplicate_names_are_server_deduplicated(
    content_platform,
) -> None:
    _root, engine, repository, _storage, importer, _book, chapter = content_platform
    chapter_id = str(chapter["id"])
    payload = _image_bytes((30, 40))
    first, _ = _import(
        repository,
        importer,
        chapter_id=chapter_id,
        payload=payload,
        logical_path="page.png",
        key="same",
    )
    replay, replayed = importer.import_page(
        chapter_id=chapter_id,
        logical_path="page.png",
        text_style=dict(DEFAULT_TEXT_STYLE),
        upload=BytesIO(payload),
        idempotency_key="same",
    )
    assert replayed and replay == first
    changed_style = dict(DEFAULT_TEXT_STYLE)
    changed_style["fontSize"] = int(changed_style["fontSize"]) + 1
    with pytest.raises(IdempotencyConflict):
        importer.import_page(
            chapter_id=chapter_id,
            logical_path="page.png",
            text_style=changed_style,
            upload=BytesIO(payload),
            idempotency_key="same",
        )
    with pytest.raises(IdempotencyConflict):
        importer.import_page(
            chapter_id=chapter_id,
            logical_path="page.png",
            text_style=dict(DEFAULT_TEXT_STYLE),
            upload=BytesIO(_image_bytes((31, 40))),
            idempotency_key="same",
        )

    second, _ = _import(
        repository,
        importer,
        chapter_id=chapter_id,
        payload=payload,
        logical_path="page.png",
        key="different",
    )
    assert second["page"]["logicalSourcePath"] == "page (2).png"
    with engine.connect() as connection:
        assert connection.execute(
            select(pages.c.id).where(pages.c.chapter_id == chapter_id)
        ).all().__len__() == 2


def test_page_listing_is_cursor_paginated_metadata_only(content_platform) -> None:
    _root, _engine, repository, _storage, importer, _book, chapter = content_platform
    chapter_id = str(chapter["id"])
    for index in range(3):
        _import(
            repository,
            importer,
            chapter_id=chapter_id,
            payload=_image_bytes((10 + index, 20)),
            logical_path=f"{index}.png",
            key=f"key-{index}",
        )
    first = repository.list_pages(chapter_id=chapter_id, limit=2)
    second = repository.list_pages(
        chapter_id=chapter_id,
        after_ordinal=int(first["nextCursor"]),
        limit=2,
    )
    assert [item["ordinal"] for item in first["items"]] == [1, 2]
    assert [item["ordinal"] for item in second["items"]] == [3]
    assert "base64" not in str(first).lower()

    page_id = str(first["items"][0]["id"])
    summary = repository.get_page_summary(page_id)
    assert summary == first["items"][0]
    assert summary["sourceUrl"].startswith("/api/v2/assets/")


def test_clear_chapter_pages_is_one_guarded_atomic_backend_command(
    content_platform,
) -> None:
    root, engine, repository, _storage, importer, book, chapter = content_platform
    chapter_id = str(chapter["id"])
    for index in range(2):
        _import(
            repository,
            importer,
            chapter_id=chapter_id,
            payload=_image_bytes((16 + index, 24)),
            logical_path=f"clear-{index}.png",
            key=f"clear-{index}",
        )
    with engine.connect() as connection:
        revision_before = connection.execute(
            select(chapters.c.page_order_revision).where(
                chapters.c.id == chapter_id
            )
        ).scalar_one()
    with engine.begin() as connection:
        connection.execute(
            insert(jobs).values(
                id="clear-pages-blocker",
                kind="translation",
                status="queued",
                book_id=str(book["id"]),
                chapter_id=chapter_id,
                config_json="{}",
                latest_progress_json=_stored_job_progress(),
            )
        )

    app = create_api_app(
        ApiSettings(
            data_root=root,
            identity=RuntimeIdentity(
                epoch_id="test-clear-pages-api",
                epoch_token="test-only",
                test_mode=True,
            ),
            engine=engine,
        )
    )
    client = app.test_client()
    blocked = client.delete(
        f"/api/v2/chapters/{chapter_id}/pages",
        headers={"Idempotency-Key": "clear-pages-blocked"},
    )
    assert blocked.status_code == 423
    assert blocked.get_json()["error"] == {
        "code": "chapter_locked",
        "message": "content is still referenced by active work",
        "details": {
            "jobs": [
                {
                    "jobId": "clear-pages-blocker",
                    "status": "queued",
                }
            ],
            "operationIds": [],
        },
    }
    assert len(repository.list_pages(chapter_id=chapter_id, all_pages=True)["items"]) == 2

    with engine.begin() as connection:
        connection.execute(
            jobs.delete().where(jobs.c.id == "clear-pages-blocker")
        )
    cleared = client.delete(
        f"/api/v2/chapters/{chapter_id}/pages",
        headers={"Idempotency-Key": "clear-pages-success"},
    )
    assert cleared.status_code == 200
    assert cleared.get_json() == {"deletedCount": 2}
    assert repository.list_pages(chapter_id=chapter_id, all_pages=True)["items"] == []
    with engine.connect() as connection:
        revision_after = connection.execute(
            select(chapters.c.page_order_revision).where(
                chapters.c.id == chapter_id
            )
        ).scalar_one()
    assert revision_after == revision_before + 1


def test_single_page_summary_route_returns_only_requested_page(content_platform) -> None:
    root, engine, repository, _storage, importer, _book, chapter = content_platform
    imported, _replayed = _import(
        repository,
        importer,
        chapter_id=str(chapter["id"]),
        payload=_image_bytes((14, 21)),
        logical_path="single.png",
        key="single-summary",
    )
    app = create_api_app(
        ApiSettings(
            data_root=root,
            identity=RuntimeIdentity(
                epoch_id="test-page-summary-api",
                epoch_token="test-only",
                test_mode=True,
            ),
            engine=engine,
        )
    )
    response = app.test_client().get(f"/api/v2/pages/{imported['page']['id']}")
    assert response.status_code == 200
    summary = response.get_json()
    assert summary["id"] == imported["page"]["id"]
    assert summary["sourceUrl"] == imported["page"]["sourceUrl"]
    assert summary["thumbnailSourceUrl"] == imported["page"]["thumbnailSourceUrl"]
    assert summary["renderStatus"] == "not_rendered"
    assert "base64" not in response.get_data(as_text=True).lower()


def test_deleting_recreated_chapters_after_reordering_keeps_contiguous_ordinals(
    content_platform,
) -> None:
    _root, _engine, repository, _storage, _importer, book, first = content_platform
    book_id = str(book["id"])
    removed = repository.create_chapter(book_id=book_id, title="Removed")
    last = repository.create_chapter(book_id=book_id, title="Last")
    repository.delete_chapter(str(removed["id"]))
    replacement = repository.create_chapter(book_id=book_id, title="Replacement")
    order = [str(first["id"]), str(replacement["id"]), str(last["id"])]
    revision = repository.list_chapters(book_id)["book"]["chapter_order_revision"]
    revision = repository.reorder_chapters(
        book_id=book_id, ordered_ids=order, base_revision=revision,
    )
    for chapter_id in order:
        repository.delete_chapter(chapter_id)
        revision += 1
        order = [value for value in order if value != chapter_id]
        current = repository.list_chapters(book_id)
        assert [(row["id"], row["ordinal"]) for row in current["chapters"]] == [
            (value, index) for index, value in enumerate(order, start=1)
        ]
        assert current["book"]["chapter_order_revision"] == revision


@pytest.mark.parametrize("deleted_index", [0, 1, 2, 3])
def test_deleting_reordered_pages_keeps_remaining_content_and_ordinals(
    content_platform, deleted_index,
) -> None:
    _root, _engine, repository, _storage, importer, _book, chapter = content_platform
    chapter_id = str(chapter["id"])
    ids = []
    for index in range(4):
        imported, _ = _import(
            repository, importer, chapter_id=chapter_id,
            payload=_image_bytes((20, 30), color=(index, 20, 30)),
            logical_path=f"{index}.png", key=f"page-delete-{index}",
        )
        ids.append(str(imported["page"]["id"]))
    order = ids[::-1]
    revision = repository.list_pages(chapter_id=chapter_id, all_pages=True)["pageOrderRevision"]
    revision = repository.reorder_pages(
        chapter_id=chapter_id, ordered_ids=order, base_revision=revision,
    )
    remaining = [value for index, value in enumerate(order) if index != deleted_index]
    documents = {
        value: _page_identity(repository.get_page_summary(value))
        for value in remaining
    }
    repository.delete_page(order[deleted_index])
    current = repository.list_pages(chapter_id=chapter_id, all_pages=True)
    assert [(row["id"], row["ordinal"]) for row in current["items"]] == [
        (value, index) for index, value in enumerate(remaining, start=1)
    ]
    assert current["pageOrderRevision"] == revision + 1
    assert {
        value: _page_identity(repository.get_page_summary(value))
        for value in remaining
    } == documents
    for page_id in remaining:
        repository.delete_page(page_id)
    assert repository.list_pages(chapter_id=chapter_id, all_pages=True)["items"] == []


def test_page_import_and_chapter_order_cas_enforce_backend_ownership(
    content_platform,
) -> None:
    _root, engine, repository, _storage, importer, book, chapter = content_platform
    chapter_id = str(chapter["id"])
    second = repository.create_chapter(book_id=str(book["id"]), title="Second")
    revision = repository.list_chapters(str(book["id"]))["book"][
        "chapter_order_revision"
    ]
    updated = repository.reorder_chapters(
        book_id=str(book["id"]),
        ordered_ids=[str(second["id"]), chapter_id],
        base_revision=int(revision),
    )
    assert updated == int(revision) + 1
    with pytest.raises(ContentConflict):
        repository.reorder_chapters(
            book_id=str(book["id"]),
            ordered_ids=[chapter_id, str(second["id"])],
            base_revision=int(revision),
        )

    with engine.begin() as connection:
        connection.execute(
            insert(jobs).values(
                id="lock-job",
                kind="translation",
                status="queued",
                chapter_id=chapter_id,
                config_json="{}",
                latest_progress_json=_stored_job_progress(),
            )
        )
        connection.execute(
            insert(chapter_write_locks).values(
                chapter_id=chapter_id,
                job_id="lock-job",
            )
        )
    with pytest.raises(ContentLocked):
        importer.import_page(
            chapter_id=chapter_id,
            logical_path="blocked.png",
            text_style=dict(DEFAULT_TEXT_STYLE),
            upload=BytesIO(_image_bytes((20, 20))),
            idempotency_key="blocked-page",
        )


def test_media_api_streams_immutable_asset_and_honors_conditional_get(
    content_platform,
) -> None:
    data_root, engine, repository, _storage, importer, _book, chapter = content_platform
    result, _ = _import(
        repository,
        importer,
        chapter_id=str(chapter["id"]),
        payload=_image_bytes((20, 20)),
        logical_path="media.png",
        key="media",
    )
    app = create_api_app(
        ApiSettings(
            data_root=data_root,
            identity=RuntimeIdentity(
                epoch_id="test-api",
                epoch_token="test-only",
                test_mode=True,
            ),
            engine=engine,
        )
    )
    client = app.test_client()
    first = client.get(result["page"]["thumbnailSourceUrl"])
    assert first.status_code == 200
    assert first.content_type == "image/webp"
    assert "immutable" in first.headers["Cache-Control"]
    assert first.headers["ETag"]
    conditional = client.get(
        result["page"]["thumbnailSourceUrl"],
        headers={"If-None-Match": first.headers["ETag"]},
    )
    assert conditional.status_code == 304
    download = client.get(
        f'{result["page"]["thumbnailSourceUrl"]}?download=1'
        "&filename=original_media.png",
    )
    assert download.status_code == 200
    assert "original_media.png" in download.headers["Content-Disposition"]
    assert "original_media.png.webp" not in download.headers["Content-Disposition"]
    extensionless_download = client.get(
        f'{result["page"]["thumbnailSourceUrl"]}?download=1'
        "&filename=task-artifact",
    )
    assert extensionless_download.status_code == 200
    assert (
        "task-artifact.webp"
        in extensionless_download.headers["Content-Disposition"]
    )


def test_media_api_marks_missing_object_as_failed_integrity(
    content_platform,
) -> None:
    data_root, engine, repository, storage, importer, _book, chapter = content_platform
    result, _ = _import(
        repository,
        importer,
        chapter_id=str(chapter["id"]),
        payload=_image_bytes((20, 20)),
        logical_path="missing.png",
        key="missing-media",
    )
    asset_id = result["page"]["thumbnailSourceUrl"].rsplit("/", 1)[-1]
    record = storage.get_record(asset_id)
    assert record is not None
    storage.resolve_relative_path(record.relative_path).unlink()

    app = create_api_app(
        ApiSettings(
            data_root=data_root,
            identity=RuntimeIdentity(
                epoch_id="test-api",
                epoch_token="test-only",
                test_mode=True,
            ),
            engine=engine,
        )
    )
    response = app.test_client().get(result["page"]["thumbnailSourceUrl"])

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "asset_missing"
    with engine.connect() as connection:
        status = connection.execute(
            select(assets.c.integrity_status).where(assets.c.id == asset_id)
        ).scalar_one()
    assert status == "missing"


def test_content_routes_reject_scalar_coercion_and_missing_required_fields(
    content_platform,
) -> None:
    data_root, engine, repository, _storage, importer, book, chapter = (
        content_platform
    )
    imported, _ = _import(
        repository,
        importer,
        chapter_id=str(chapter["id"]),
        payload=_image_bytes((40, 60)),
        logical_path="strict-content-routes.png",
        key="strict-content-routes",
    )
    page_id = str(imported["page"]["id"])
    app = create_api_app(
        ApiSettings(
            data_root=data_root,
            identity=RuntimeIdentity(
                epoch_id="strict-content-routes-api",
                epoch_token="test-only",
                test_mode=True,
            ),
            engine=engine,
        )
    )
    client = app.test_client()
    calls = [
        lambda: client.put(
            f"/api/v2/books/{book['id']}",
            headers={"Idempotency-Key": "strict-book-boolean"},
            json={"clearCover": "yes"},
        ),
        lambda: client.post(
            "/api/v2/books/batch-tags",
            headers={"Idempotency-Key": "strict-batch-action"},
            json={"bookIds": [book["id"]], "tagIds": [], "action": {}},
        ),
        lambda: client.put(
            f"/api/v2/books/{book['id']}/chapters/order",
            headers={"Idempotency-Key": "strict-chapter-revision"},
            json={"orderedIds": [chapter["id"]], "baseRevision": True},
        ),
        lambda: client.get(
            f"/api/v2/chapters/{chapter['id']}/pages?all=perhaps"
        ),
        lambda: client.put(
            f"/api/v2/pages/{page_id}/source",
            headers={"Idempotency-Key": "strict-source-revision"},
            data={
                "file": (BytesIO(_image_bytes((40, 60))), "strict.png"),
            },
            content_type="multipart/form-data",
        ),
    ]

    try:
        for call in calls:
            response = call()
            assert response.status_code == 422, response.get_data(as_text=True)
            assert response.get_json()["error"]["code"] == "validation_error"
    finally:
        app.extensions["saber_v2_runtime"].close()


def test_content_tag_filters_and_batch_updates_reject_duplicate_ids(
    content_platform,
) -> None:
    _root, _engine, repository, _storage, _importer, book, _chapter = (
        content_platform
    )
    tag = repository.create_tag(name="Strict", color="#112233")

    with pytest.raises(ValueError, match="tagIds must contain unique IDs"):
        repository.list_books(tag_ids=(str(tag["id"]), str(tag["id"])))
    with pytest.raises(ValueError, match="tagIds must contain unique IDs"):
        repository.batch_update_tags(
            book_ids=[str(book["id"])],
            tag_ids=[str(tag["id"]), str(tag["id"])],
            action="add",
        )


