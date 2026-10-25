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

from src.backend_v2.api.request_helpers import error_response as _error
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
from src.backend_v2.manga_import.local_importer import import_single_local_book
from src.backend_v2.manga_import.source_tag import display_title
from src.backend_v2.manga_import.venera_importer import import_single_venera_comic

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

def import_single_venera_comic_v2(
    *,
    base_dir: Path,
    comic_id: str,
    data_root: Path,
    engine: object,
    importer: ImageImportService,
) -> dict[str, Any]:
    """兼容包装：保留模块级旧入口名，实现已迁到 ``venera_importer``。"""
    return import_single_venera_comic(
        base_dir=base_dir,
        comic_id=comic_id,
        data_root=data_root,
        engine=engine,
        importer=importer,
    )

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
        except FileNotFoundError as error:
            return _error(
                "venera_data_dir_unavailable",
                f"未找到 Venera 数据目录，请在设置里指定：{error}",
                503,
            )
        except Exception as error:  # noqa: BLE001 - 库读取失败要显式上报
            return _error(
                "venera_library_unreadable",
                f"读取 Venera 书库失败：{error}",
                500,
            )
        return jsonify({
            "comics": comics,
            "total": len(comics),
            "veneraFound": True,
            "baseDir": str(base_dir),
            "baseDirSource": source,
            "dbPath": str(venera_local_db_path(base_dir)),
        })

    @blueprint.get("/venera/cover")
    def get_venera_cover() -> Response:
        comic_id = str(request.args.get("comicId") or "").strip()
        if not comic_id:
            return _error("validation_error", "comicId 不能为空", 400)
        try:
            base_dir, _ = resolve_venera_base_dir(engine)
        except Exception as error:  # noqa: BLE001
            return _error(
                "venera_data_dir_unavailable",
                f"未找到 Venera 数据目录，请在设置里指定：{error}",
                503,
            )
        comic = _get_venera_comic_detail(base_dir, comic_id)
        if comic is None:
            return _error("not_found", f"Venera 书库里没有漫画 '{comic_id}'", 404)
        path = venera_cover_path(base_dir, comic)
        if path is None:
            return _error("not_found", f"漫画 '{comic_id}' 的封面文件不存在", 404)
        mimetype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        response = send_file(path, mimetype=mimetype, conditional=True)
        response.headers["Cache-Control"] = "private, max-age=3600"
        return response

    @blueprint.post("/venera")
    def import_venera() -> Response:
        payload = request.get_json(silent=True) or {}
        comic_ids = payload.get("comic_ids") or []
        if not isinstance(comic_ids, list) or not comic_ids:
            return _error("validation_error", "comic_ids 必须是非空数组", 400)

        try:
            base_dir, _ = resolve_venera_base_dir(engine)
        except Exception as error:  # noqa: BLE001
            return _error(
                "venera_data_dir_unavailable",
                f"未找到 Venera 数据目录，请在设置里指定：{error}",
                503,
            )

        imported_count = 0
        partial_count = 0
        failed_count = 0
        details: list[dict[str, Any]] = []

        for raw_id in comic_ids:
            comic_id = str(raw_id)
            try:
                result = import_single_venera_comic(
                    base_dir=base_dir,
                    comic_id=comic_id,
                    data_root=data_root,
                    engine=engine,
                    importer=importer,
                )
            except Exception as error:  # noqa: BLE001 - 单本失败不打断整批
                result = _crash_result(comic_id, error)
            status = str(result.get("status") or "failed")
            if status == "success":
                imported_count += 1
            elif status == "partial":
                partial_count += 1
            else:
                failed_count += 1
            details.append(result)

        return jsonify({
            "imported": imported_count,
            "partial": partial_count,
            "failed": failed_count,
            "details": details,
        })

    @blueprint.post("/local/preview")
    def preview_local() -> Response:
        payload = request.get_json(silent=True) or {}
        paths = payload.get("paths") or []
        enable_ai_vision = bool(payload.get("enable_ai_vision", False))
        if not isinstance(paths, list) or not paths:
            return _error("validation_error", "paths 必须是非空数组", 400)

        try:
            result = preview_local_paths(paths=paths, enable_ai_vision=enable_ai_vision)
        except ValueError as error:
            return _error("validation_error", str(error), 400)
        except Exception as error:  # noqa: BLE001
            return _error("scan_failed", f"扫描失败：{error}", 500)
        return jsonify(result)

    @blueprint.post("/local/confirm")
    def confirm_local() -> Response:
        payload = request.get_json(silent=True) or {}
        books_payload = payload.get("books") or []
        if not isinstance(books_payload, list) or not books_payload:
            return _error("validation_error", "books 必须是非空数组", 400)

        imported_books: list[dict[str, Any]] = []
        for index, book_data in enumerate(books_payload, start=1):
            if not isinstance(book_data, dict):
                imported_books.append(
                    _crash_result(
                        f"book#{index}",
                        ValueError("books 数组的每一项都必须是对象"),
                    )
                )
                continue
            try:
                imported_books.append(
                    import_single_local_book(
                        book_data,
                        data_root=data_root,
                        engine=engine,
                        importer=importer,
                    )
                )
            except Exception as error:  # noqa: BLE001 - 单本失败不打断整批
                imported_books.append(_crash_result(f"book#{index}", error))

        statuses = [str(item.get("status") or "failed") for item in imported_books]
        if all(value == "failed" for value in statuses):
            overall = "failed"
        elif any(value != "success" for value in statuses):
            overall = "partial"
        else:
            overall = "success"

        return jsonify({
            "status": overall,
            "imported_books": imported_books,
            "imported": statuses.count("success"),
            "partial": statuses.count("partial"),
            "failed": statuses.count("failed"),
        }), 200

    return blueprint


def _crash_result(identifier: str, error: Exception) -> dict[str, Any]:
    """把路由层捕获的异常也表达成与导入器一致的形状。"""
    return {
        "comic_id": identifier,
        "book_id": None,
        "status": "failed",
        "chapters": 0,
        "chaptersWritten": 0,
        "pages": 0,
        "pagesWritten": 0,
        "resumed": False,
        "skippedChapters": [],
        "issues": [
            {
                "scope": "book",
                "code": "import_crashed",
                "reason": f"{type(error).__name__}: {error}",
            }
        ],
        "summary": {"pagesFailed": 0, "chaptersFailed": 1, "bookFaults": 1, "coverFailed": 0},
        "title": "",
    }


def _display_titles(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for item in items:
        if isinstance(item, dict) and item.get("title"):
            item["title"] = display_title(str(item["title"]))
    return items


__all__ = [
    "create_manga_import_blueprint",
    "default_venera_base_dir",
    "import_single_venera_comic_v2",
    "list_venera_comics",
    "resolve_venera_base_dir",
    "venera_comic_dir",
    "venera_cover_path",
    "venera_local_db_path",
]
