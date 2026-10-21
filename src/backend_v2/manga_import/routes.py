import os
import json
import uuid
import sqlite3
import shutil
import mimetypes
from pathlib import Path
from typing import Any
from flask import Blueprint, jsonify, request, Response, send_file
from sqlalchemy import select, insert, update

from src.backend_v2.storage.schema import books, chapters, pages, page_assets
from src.backend_v2.content.repository import ContentRepository
from src.backend_v2.settings.resolver import SettingsResolver
from src.backend_v2.storage.assets import AssetStorageService
from src.backend_v2.content.image_import import ImageImportService
from src.backend_v2.content.page_style import resolve_new_page_style
from src.backend_v2.timestamps import utcnow
from src.backend_v2.auth.authorization import effective_owner_id
from src.backend_v2.auth.constants import LOCAL_USER_ID
from src.backend_v2.manga_import.local_preview import preview_local_paths
from src.backend_v2.manga_import.local_confirm import import_single_local_book

def default_venera_base_dir() -> Path:
    """Venera 的默认数据目录（安装后即存在；用户可在导入界面覆盖）。"""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise FileNotFoundError("APPDATA environment variable not found")
    return Path(appdata) / "com.github.wgh136" / "venera"


def _expand_user_path(raw: str) -> Path:
    text = os.path.expandvars(os.path.expanduser(raw.strip().strip(chr(34))))
    return Path(text)


def resolve_venera_base_dir(engine: object) -> tuple[Path, str]:
    """返回 (Venera 数据目录, 来源)；来源为 ``setting`` 或 ``default``。

    用户在设置里可能填 Venera 根目录（含 ``local.db``），也可能填它的 ``local``
    子目录；两种都接受，避免因为多一层/少一层目录就报「未检测到」。
    """
    configured = ""
    try:
        configured = str(SettingsResolver(engine).resolve_manga_import().get("venera_base_dir") or "")
    except Exception:
        configured = ""
    if configured:
        candidate = _expand_user_path(configured)
        if (candidate / "local.db").is_file():
            return candidate, "setting"
        if candidate.name.lower() == "local" and (candidate.parent / "local.db").is_file():
            return candidate.parent, "setting"
        return candidate, "setting"
    return default_venera_base_dir(), "default"


def venera_local_db_path(base_dir: Path) -> Path:
    return base_dir / "local.db"


def venera_comic_dir(base_dir: Path, directory: object) -> Path | None:
    name = str(directory or "").strip()
    if not name:
        return None
    candidate = base_dir / "local" / name
    try:
        candidate.resolve().relative_to((base_dir / "local").resolve())
    except (ValueError, OSError):
        return None
    return candidate


def venera_cover_path(base_dir: Path, comic: dict[str, Any]) -> Path | None:
    comic_dir = venera_comic_dir(base_dir, comic.get("directory"))
    cover = str(comic.get("cover") or "").strip()
    if comic_dir is None or not cover:
        return None
    path = comic_dir / cover
    try:
        path.resolve().relative_to((base_dir / "local").resolve())
    except (ValueError, OSError):
        return None
    return path if path.is_file() else None


def list_venera_comics(base_dir: Path) -> list[dict[str, object]]:
    db_path = venera_local_db_path(base_dir)
    if not db_path.exists():
        return []
    
    abs_posix = db_path.resolve().as_posix()
    uri = f"file:///{abs_posix}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, title, subtitle, tags, directory, chapters, cover, comic_type, downloadedChapters, created_at FROM comics")
        rows = cur.fetchall()
        result = []
        for r in rows:
            cid, title, subtitle, tags_json, directory, chapters_json, cover, comic_type, downloaded_json, created_at = r
            try:
                downloaded_list = json.loads(downloaded_json) if downloaded_json else []
            except Exception:
                downloaded_list = []
            
            try:
                tags = json.loads(tags_json) if tags_json else []
            except Exception:
                tags = []

            cover_rel = f"local/{directory}/{cover}" if directory and cover else ""
            comic_dir = venera_comic_dir(base_dir, directory)
            has_cover = bool(comic_dir and cover and (comic_dir / cover).is_file())

            result.append({
                "id": cid,
                "title": title,
                "subtitle": subtitle,
                "tags": tags,
                "directory": directory,
                "cover": cover,
                "coverPath": cover_rel,
                "hasCover": has_cover,
                "comicType": comic_type,
                "chapterCount": len(downloaded_list),
                "downloadedChapters": downloaded_list,
                "createdAt": created_at,
            })
        return result
    finally:
        conn.close()

def _get_venera_comic_detail(base_dir: Path, comic_id: str) -> dict[str, Any] | None:
    db_path = venera_local_db_path(base_dir)
    if not db_path.exists():
        return None
    abs_posix = db_path.resolve().as_posix()
    uri = f"file:///{abs_posix}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, title, directory, cover, chapters, downloadedChapters FROM comics WHERE id = ?", (comic_id,))
        row = cur.fetchone()
        if not row:
            return None
        cid, title, directory, cover, chapters_json, downloaded_json = row
        chapters_data = json.loads(chapters_json) if chapters_json else {}
        downloaded = json.loads(downloaded_json) if downloaded_json else []
        
        chapter_title_map: dict[str, str] = {}
        if isinstance(chapters_data, dict):
            for group_name, group_map in chapters_data.items():
                if isinstance(group_map, dict):
                    for cuuid, ctitle in group_map.items():
                        chapter_title_map[cuuid] = str(ctitle)
                elif isinstance(group_map, str):
                    chapter_title_map[group_name] = str(group_map)

        return {
            "id": cid,
            "title": title,
            "directory": directory,
            "cover": cover,
            "chapter_title_map": chapter_title_map,
            "downloaded_chapters": downloaded,
        }
    finally:
        conn.close()

def import_single_venera_comic(
    *,
    base_dir: Path,
    comic_id: str,
    data_root: Path,
    engine: object,
    importer: ImageImportService,
) -> dict[str, Any]:
    comic = _get_venera_comic_detail(base_dir, comic_id)
    if not comic:
        return {"comic_id": comic_id, "status": "failed", "error": f"Venera comic '{comic_id}' not found in local.db"}

    comic_dir = venera_comic_dir(base_dir, comic["directory"])
    if comic_dir is None:
        return {"comic_id": comic_id, "status": "failed", "error": f"Comic directory for '{comic_id}' is invalid"}
    if not comic_dir.exists():
        return {"comic_id": comic_id, "status": "failed", "error": f"Comic directory '{comic_dir}' does not exist"}

    now = utcnow()
    book_id = str(uuid.uuid4())
    owner_id = effective_owner_id()

    # 1. Publish cover asset (outside transaction)
    cover_asset_id = None
    cover_path = comic_dir / comic["cover"] if comic.get("cover") else None
    if cover_path and cover_path.exists():
        with cover_path.open("rb") as f:
            cover_asset = importer.publish_cover(f)
            cover_asset_id = cover_asset.id

    # 2. Insert book record
    with engine.begin() as conn:
        conn.execute(
            insert(books).values(
                id=book_id,
                owner_user_id=owner_id,
                kind="library",
                title=comic["title"],
                chapter_order_revision=1,
                cover_asset_id=cover_asset_id,
                created_at=now,
                updated_at=now,
            )
        )

    # Resolve default page style
    with engine.connect() as conn:
        default_font_id, style_defaults = resolve_new_page_style(conn)

    total_chapters = 0
    total_pages = 0

    downloaded = comic["downloaded_chapters"]
    title_map = comic["chapter_title_map"]

    for c_idx, ch_uuid in enumerate(downloaded, start=1):
        ch_title = title_map.get(ch_uuid, f"第{c_idx}话")
        chapter_dir = comic_dir / ch_uuid
        if not chapter_dir.exists():
            continue

        chapter_id = str(uuid.uuid4())
        ch_now = utcnow()
        with engine.begin() as conn:
            conn.execute(
                insert(chapters).values(
                    id=chapter_id,
                    book_id=book_id,
                    ordinal=c_idx,
                    title=ch_title,
                    page_order_revision=1,
                    settings_memory_json="{}",
                    settings_memory_revision=1,
                    created_at=ch_now,
                    updated_at=ch_now,
                )
            )

        page_files = [f for f in chapter_dir.iterdir() if f.is_file() and f.suffix.lower() in (".webp", ".jpg", ".jpeg", ".png")]
        page_files.sort(key=lambda f: int(f.stem) if f.stem.isdigit() else 999999)

        batch_size = 50
        for b_start in range(0, len(page_files), batch_size):
            chunk = page_files[b_start : b_start + batch_size]
            
            # Step A: Publish image assets OUTSIDE open database transaction
            published_chunk: list[tuple[int, str, Any, Any]] = []
            for p_offset, p_file in enumerate(chunk):
                p_ordinal = b_start + p_offset + 1
                with p_file.open("rb") as pf:
                    source_asset, thumb_asset = importer.publish_standalone_image(pf)
                published_chunk.append((p_ordinal, p_file.name, source_asset, thumb_asset))

            # Step B: Insert page records in a dedicated transaction
            with engine.begin() as conn:
                for p_ordinal, fname, source_asset, thumb_asset in published_chunk:
                    page_id = str(uuid.uuid4())
                    logical_path = f"{ch_uuid}/{fname}"
                    p_now = utcnow()

                    conn.execute(
                        insert(pages).values(
                            id=page_id,
                            chapter_id=chapter_id,
                            ordinal=p_ordinal,
                            logical_source_path=logical_path,
                            source_revision=1,
                            document_revision=1,
                            render_status="not_rendered",
                            detection_state="unprocessed",
                            default_font_id=default_font_id,
                            page_style_defaults_json=json.dumps(style_defaults),
                            warnings_json="[]",
                            created_at=p_now,
                            updated_at=p_now,
                        )
                    )

                    conn.execute(
                        insert(page_assets),
                        [
                            {
                                "page_id": page_id,
                                "role": "source",
                                "asset_id": source_asset.id,
                                "input_source_revision": 1,
                                "parent_asset_id": None,
                            },
                            {
                                "page_id": page_id,
                                "role": "thumbnail_source",
                                "asset_id": thumb_asset.id,
                                "input_source_revision": 1,
                                "parent_asset_id": source_asset.id,
                            },
                        ],
                    )
                    total_pages += 1

        total_chapters += 1

    return {
        "comic_id": comic_id,
        "book_id": book_id,
        "status": "success",
        "chapters": total_chapters,
        "pages": total_pages,
    }

def create_manga_import_blueprint(*, data_root: Path, engine: object) -> Blueprint:
    blueprint = Blueprint("manga_import_v2", __name__, url_prefix="/api/v2/import")

    repository = ContentRepository(engine)
    storage = AssetStorageService(data_root, engine)
    importer = ImageImportService(
        data_root=data_root,
        repository=repository,
        storage=storage,
    )

    @blueprint.get("/venera/library")
    def get_venera_library() -> Response:
        try:
            base_dir, source = resolve_venera_base_dir(engine)
            comics = list_venera_comics(base_dir)
            return jsonify({
                "comics": comics,
                "total": len(comics),
                "veneraFound": True,
                "baseDir": str(base_dir),
                "baseDirSource": source,
                "dbPath": str(venera_local_db_path(base_dir)),
            })
        except Exception as e:
            return jsonify({
                "comics": [],
                "total": 0,
                "veneraFound": False,
                "error": str(e),
            }), 200

    @blueprint.get("/venera/cover")
    def get_venera_cover() -> Response:
        comic_id = str(request.args.get("comicId") or "").strip()
        if not comic_id:
            return jsonify({"error": "comicId is required"}), 400
        try:
            base_dir, _ = resolve_venera_base_dir(engine)
        except Exception as e:
            return jsonify({"error": str(e)}), 404
        comic = _get_venera_comic_detail(base_dir, comic_id)
        if comic is None:
            return jsonify({"error": "comic not found"}), 404
        path = venera_cover_path(base_dir, comic)
        if path is None:
            return jsonify({"error": "cover not found"}), 404
        mimetype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        response = send_file(path, mimetype=mimetype, conditional=True)
        response.headers["Cache-Control"] = "private, max-age=3600"
        return response

    @blueprint.post("/venera")
    def import_venera() -> Response:
        payload = request.get_json(silent=True) or {}
        comic_ids = payload.get("comic_ids") or []
        if not isinstance(comic_ids, list) or not comic_ids:
            return jsonify({"error": "comic_ids must be a non-empty array"}), 400

        details = []
        imported_count = 0
        failed_count = 0

        try:
            base_dir, _ = resolve_venera_base_dir(engine)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

        for cid in comic_ids:
            try:
                res = import_single_venera_comic(
                    base_dir=base_dir,
                    comic_id=str(cid),
                    data_root=data_root,
                    engine=engine,
                    importer=importer,
                )
                details.append(res)
                if res.get("status") == "success":
                    imported_count += 1
                else:
                    failed_count += 1
            except Exception as e:
                details.append({
                    "comic_id": str(cid),
                    "status": "failed",
                    "error": str(e),
                })
                failed_count += 1

        return jsonify({
            "imported": imported_count,
            "failed": failed_count,
            "details": details,
        })

    @blueprint.post("/local/preview")
    def preview_local() -> Response:
        payload = request.get_json(silent=True) or {}
        paths = payload.get("paths") or []
        enable_ai_vision = bool(payload.get("enable_ai_vision", False))
        if not isinstance(paths, list) or not paths:
            return jsonify({"error": "paths 必须是非空数组"}), 400

        try:
            result = preview_local_paths(paths=paths, enable_ai_vision=enable_ai_vision)
            return jsonify(result), 200
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"扫描失败: {str(e)}"}), 500

    @blueprint.post("/local/confirm")
    def confirm_local() -> Response:
        payload = request.get_json(silent=True) or {}
        books_payload = payload.get("books") or []
        if not isinstance(books_payload, list) or not books_payload:
            return jsonify({"error": "books 必须是非空数组"}), 400

        imported_books = []
        for b_data in books_payload:
            try:
                res = import_single_local_book(
                    b_data,
                    data_root=data_root,
                    engine=engine,
                    importer=importer,
                )
                imported_books.append(res)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            except Exception as e:
                return jsonify({"error": f"导入失败: {str(e)}"}), 500

        return jsonify({
            "status": "ok",
            "imported_books": imported_books,
        }), 200

    return blueprint
