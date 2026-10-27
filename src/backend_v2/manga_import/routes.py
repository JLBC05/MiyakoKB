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
from src.backend_v2.manga_import.integrity import (
    check_import_integrity,
    probe_local_paths,
    probe_venera_comic,
)
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

def _match_venera_directory(paths: list[Path]) -> tuple[Path, str] | None:
    """若路径恰好是 Venera 某本漫画的目录，返回 ``(base_dir, comic_id)``。

    Venera 的章节目录名是随机 UUID，按名字排序得到的序位与 ``downloaded_chapters``
    里的真实顺序无关，直接当本地目录扫描会把「序位错配」误报成「页数不足」。
    """

    if not paths or not all(item.is_dir() for item in paths):
        return None
    # Venera 是 Windows 桌面客户端，只有 Windows 才有 APPDATA；在没有它的机器上
    # 「这个目录是不是 Venera 的」无从判断，安静退回本地口径即可，不能把导入本身搞失败。
    try:
        base_dir = default_venera_base_dir()
    except OSError:
        return None
    if not base_dir.is_dir():
        return None
    local_root = base_dir / "local"
    wanted = {item.resolve() for item in paths}
    try:
        comics = list_venera_comics(base_dir)
    except Exception:  # noqa: BLE001 - 读不到 Venera 就安静退回本地口径
        return None
    for comic in comics:
        comic_dir = venera_comic_dir(base_dir, comic.get("directory"))
        if comic_dir is None:
            continue
        try:
            resolved = comic_dir.resolve()
        except OSError:
            resolved = comic_dir
        if resolved in wanted:
            return base_dir, str(comic.get("id") or "")
    _ = local_root  # 保留给排查用：目录形状是 base/local/<directory>
    return None


def _integrity_for_venera(
    *,
    engine: object,
    base_dir: Path,
    comic_id: str,
    book_id: str | None,
) -> dict[str, Any] | None:
    """导入后立刻核对一本书，把结果挂进导入响应。

    口径与 ``POST /import/integrity-check`` 完全一致（同一套 ``integrity``
    函数），区别只在**不抛出**：核对本身失败不能把已经成功的导入变成失败，
    所以这里把异常收成 ``{"ok": None, "error": "..."}``。

    ``book_id`` 为空（导入彻底失败）时直接返回 ``None`` —— 没有书就没有可
    对照的东西。
    """

    if not book_id:
        return None
    try:
        probes, notes = probe_venera_comic(base_dir, comic_id)
        detail = _get_venera_comic_detail(base_dir, comic_id) or {}
        downloaded = list(detail.get("downloaded_chapters") or [])
        on_disk = {item.ordinal for item in probes}
        unmapped = [
            index for index in range(1, len(downloaded) + 1) if index not in on_disk
        ]
        report = check_import_integrity(
            engine=engine,
            book_id=book_id,
            probes=probes,
            source="venera",
            target=str(detail.get("title") or comic_id),
            notes=notes,
            unmapped_ordinals=unmapped,
        )
        return report.to_payload()
    except Exception as error:  # noqa: BLE001 - 核对失败不影响导入结果
        return {"ok": None, "error": f"{type(error).__name__}: {error}"}


def _integrity_for_local(
    *,
    engine: object,
    paths: list[str],
    book_id: str | None,
) -> dict[str, Any] | None:
    """本地导入后的核对；路径指向 Venera 漫画目录时自动改走 Venera 口径。"""

    if not book_id:
        return None
    try:
        resolved = [_expand_user_path(str(item)) for item in paths]
        venera_hit = _match_venera_directory(resolved)
        if venera_hit is not None:
            base_dir, comic_id = venera_hit
            return _integrity_for_venera(
                engine=engine,
                base_dir=base_dir,
                comic_id=comic_id,
                book_id=book_id,
            )
        probes = probe_local_paths(resolved)
        report = check_import_integrity(
            engine=engine,
            book_id=book_id,
            probes=probes,
            source="local",
            target=", ".join(str(item) for item in resolved),
        )
        return report.to_payload()
    except Exception as error:  # noqa: BLE001 - 核对失败不影响导入结果
        return {"ok": None, "error": f"{type(error).__name__}: {error}"}


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
            # 导入完立刻核对：把「磁盘有、库里没有」的章节在响应里直接透出来，
            # 用户不必再手动点一次核对。核对失败只降级，不影响导入状态。
            result["integrity"] = _integrity_for_venera(
                engine=engine,
                base_dir=base_dir,
                comic_id=comic_id,
                book_id=result.get("book_id"),
            )
            details.append(result)

        return jsonify({
            "imported": imported_count,
            "partial": partial_count,
            "failed": failed_count,
            "details": details,
        })

    @blueprint.post("/integrity-check")
    def check_import_integrity_route() -> Response:
        """核对「磁盘上实际存在的章节是否都进了库」。

        口径见 ``integrity`` 模块文档：以文件系统为准，Venera 登记但磁盘没有的
        章节报「未下载」而不是失败。纯只读，不写库、不联网。
        """

        payload = request.get_json(silent=True) or {}
        source = str(payload.get("source") or "").strip()
        if source not in {"venera", "local"}:
            return _error(
                "validation_error", "source 必须是 venera 或 local", 400
            )
        book_id = str(payload.get("bookId") or "").strip()
        if not book_id:
            return _error("validation_error", "bookId 不能为空", 400)

        notes: list[str] = []
        unmapped: list[int] = []
        try:
            if source == "venera":
                comic_id = str(payload.get("comicId") or "").strip()
                if not comic_id:
                    return _error("validation_error", "comicId 不能为空", 400)
                base_dir, _ = resolve_venera_base_dir(engine)
                detail = _get_venera_comic_detail(base_dir, comic_id)
                probes, notes = probe_venera_comic(base_dir, comic_id)
                target = str((detail or {}).get("title") or comic_id)
                downloaded = list((detail or {}).get("downloaded_chapters") or [])
                on_disk = {item.ordinal for item in probes}
                unmapped = [
                    index
                    for index in range(1, len(downloaded) + 1)
                    if index not in on_disk
                ]
            else:
                raw_paths = payload.get("paths") or []
                if not isinstance(raw_paths, list) or not raw_paths:
                    return _error("validation_error", "paths 必须是非空数组", 400)
                paths = [_expand_user_path(str(item)) for item in raw_paths]
                # 指向 Venera 漫画根目录时，只有 ``downloaded_chapters`` 的顺序
                # 是权威（目录名是随机 UUID，按名字排序必然错位）。这里自动
                # 识别并改走 Venera 口径，避免把序位错配报成「页数不足」。
                venera_hit = _match_venera_directory(paths)
                if venera_hit is not None:
                    base_dir, comic_id = venera_hit
                    probes, notes = probe_venera_comic(base_dir, comic_id)
                    source = "venera"
                    target = comic_id
                    detail = _get_venera_comic_detail(base_dir, comic_id)
                    downloaded = list((detail or {}).get("downloaded_chapters") or [])
                    on_disk = {item.ordinal for item in probes}
                    unmapped = [
                        index
                        for index in range(1, len(downloaded) + 1)
                        if index not in on_disk
                    ]
                    notes.append(
                        "该路径是 Venera 漫画目录，已按 Venera 的已下载顺序核对"
                        "（目录名是随机 UUID，按名字排序会与库里的序位错配）"
                    )
                else:
                    probes = probe_local_paths(paths)
                    target = ", ".join(str(item) for item in paths)
        except FileNotFoundError as error:
            return _error("import_source_unavailable", str(error), 503)
        except Exception as error:  # noqa: BLE001 - 核对失败要显式上报
            return _error("integrity_check_failed", f"核对失败：{error}", 500)

        report = check_import_integrity(
            engine=engine,
            book_id=book_id,
            probes=probes,
            source=source,
            target=target,
            notes=notes,
            unmapped_ordinals=unmapped,
        )
        return jsonify(report.to_payload())

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
        # 每本书自己的源路径，用于导入后核对（books[i] 的 paths 与 imported_books[i] 一一对应）。
        source_paths: list[list[str]] = []
        for index, book_data in enumerate(books_payload, start=1):
            if not isinstance(book_data, dict):
                imported_books.append(
                    _crash_result(
                        f"book#{index}",
                        ValueError("books 数组的每一项都必须是对象"),
                    )
                )
                source_paths.append([])
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

            raw_paths = book_data.get("paths") if isinstance(book_data, dict) else None
            if not isinstance(raw_paths, list) or not raw_paths:
                raw_paths = [
                    str(item.get("source_path"))
                    for item in (book_data.get("chapters") or [])
                    if isinstance(item, dict) and item.get("source_path")
                ]
            source_paths.append([str(item) for item in raw_paths])

        # 本地导入同样在响应里带上核对结果；路径缺失时核对照常跳过。
        for entry, paths in zip(imported_books, source_paths):
            if not isinstance(entry, dict) or not paths:
                continue
            entry["integrity"] = _integrity_for_local(
                engine=engine,
                paths=paths,
                book_id=entry.get("book_id"),
            )

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
