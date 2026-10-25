"""Venera 单本导入：可续传、可部分成功、失败逐条上报。

相对于旧实现（每次 new uuid4 book_id、整章异常直接放弃、封面失败静默吞），
这里的行为约定是：

1. **幂等**：书标题带 ``venera:<comic_id>`` 溯源标记；重试同一本漫画会命中已有
   书（``resumed=True``），已完成章节（同 ordinal、同页数、同逻辑页路径集合）
   整章跳过，因此不会产生重复书，也不会重复搬页。
2. **部分成功可见**：账号目录缺失 → 一条 ``chapter`` 级 issue 并继续；单张图片
   发布失败 → 一条 ``page`` 级 issue 并继续。返回值里 ``status`` 取
   ``success`` / ``partial`` / ``failed``，并带 ``issues`` 明细。
3. **封面失败不再静默**：封面发布异常记 ``cover`` 级 issue，书仍然创建。
"""

from __future__ import annotations

import sqlite3
import json
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Optional

from sqlalchemy.exc import IntegrityError

from src.backend_v2.content.image_import import ImageImportService
from src.backend_v2.manga_import.importer_base import (
    BOOK_INVALID,
    ImportIssue,
    SOURCE_INVALID,
    SOURCE_MISSING,
    SOURCE_READ_FAILED,
    STORAGE_CHAPTER_FAILED,
    STORAGE_COVER_FAILED,
    _ImporterBase,
    _chapter_ref,
)
from src.backend_v2.manga_import.source_tag import build_title


class _LedgerUnavailableRename(RuntimeError):
    """库上已有旧版（无标记）同源书、但重命名撞上唯一约束时使用。"""

_IMAGE_SUFFIXES = (".webp", ".jpg", ".jpeg", ".png")


def _page_sort_key(path: Path) -> int:
    return int(path.stem) if path.stem.isdigit() else 999999


class _VeneraImporter(_ImporterBase):
    """把一本 Venera 漫画搬进知识库。"""

    def run(self, *, base_dir: Path, comic_id: str) -> dict[str, Any]:
        comic = _get_venera_comic_detail(base_dir, comic_id)
        if comic is None:
            return self._result(
                book_id=None,
                title="",
                chapters_written=0,
                pages_written=0,
                issues=[
                    ImportIssue(
                        scope="book",
                        code=SOURCE_MISSING,
                        reason=f"Venera 库中找不到漫画 '{comic_id}'",
                    )
                ],
                resumed=False,
                comic_id=comic_id,
            )

        directory = str(comic.get("directory") or "")
        comic_dir = _venera_comic_dir(base_dir, directory)
        if comic_dir is None:
            return self._result(
                book_id=None,
                title=comic["title"],
                chapters_written=0,
                pages_written=0,
                issues=[
                    ImportIssue(
                        scope="book",
                        code=SOURCE_INVALID,
                        reason=f"漫画目录非法：{directory!r}",
                    )
                ],
                resumed=False,
                comic_id=comic_id,
            )
        if not comic_dir.exists():
            return self._result(
                book_id=None,
                title=comic["title"],
                chapters_written=0,
                pages_written=0,
                issues=[
                    ImportIssue(
                        scope="book",
                        code=SOURCE_MISSING,
                        reason=f"漫画目录不存在：{comic_dir}",
                    )
                ],
                resumed=False,
                comic_id=comic_id,
            )

        issues: list[ImportIssue] = []
        wire_title = build_title(comic["title"], kind="venera", identifier=comic_id)

        existing = self._find_existing_book(kind="venera", identifier=comic_id, digest=_digest(comic_id))
        if existing is None:
            # 兼容 v0.1.0 及更早导入的书：标题是裸书名、没有溯源标记。命中就补上
            # 标记，让后续重试走续传而不是再造一本重复书。
            existing = self._adopt_legacy_book(comic["title"], kind="venera", identifier=comic_id)
        resumed = existing is not None
        if existing is not None:
            book_id = existing
        else:
            cover_asset_id, cover_issue = self._publish_cover(comic_dir, comic, comic_id=comic_id)
            if cover_issue is not None:
                issues.append(cover_issue)
            book_id = self._create_book(title=wire_title, cover_asset_id=cover_asset_id)

        default_font_id, style_defaults = self._resolve_page_style()
        done_chapters = self._existing_chapters(book_id) if resumed else {}

        downloaded = list(comic.get("downloaded_chapters") or [])
        title_map: dict[str, str] = comic.get("chapter_title_map") or {}

        chapters_written = 0
        pages_written = 0
        skipped_chapters: list[dict[str, object]] = []

        for c_idx, ch_uuid in enumerate(downloaded, start=1):
            ch_title = title_map.get(ch_uuid, f"第{c_idx}话")
            chapter_dir = comic_dir / ch_uuid
            if not chapter_dir.exists():
                issues.append(
                    ImportIssue(
                        scope="chapter",
                        code=SOURCE_MISSING,
                        reason="源目录缺失（Venera 里已下载但磁盘上没有这个章节目录）",
                        chapter=_chapter_ref(c_idx, ch_title),
                    )
                )
                continue

            try:
                page_files = [
                    item
                    for item in chapter_dir.iterdir()
                    if item.is_file() and item.suffix.lower() in _IMAGE_SUFFIXES
                ]
            except OSError as error:
                issues.append(
                    ImportIssue(
                        scope="chapter",
                        code=SOURCE_READ_FAILED,
                        reason=f"读取章节目录失败：{error}",
                        chapter=_chapter_ref(c_idx, ch_title),
                    )
                )
                continue

            page_files.sort(key=_page_sort_key)
            logical_paths = {f"{ch_uuid}/{item.name}" for item in page_files}
            done = done_chapters.get(c_idx)
            if done is not None and _chapter_complete(done, logical_paths, len(page_files)):
                skipped_chapters.append(_chapter_ref(c_idx, ch_title))
                continue
            if done is not None:
                page_files = [
                    item for item in page_files if f"{ch_uuid}/{item.name}" not in done["paths"]
                ]

            try:
                chapter_id = self._create_chapter(book_id=book_id, ordinal=c_idx, title=ch_title)
            except IntegrityError:
                # 续传时同 ordinal 的章记录已存在（上次写到一半崩了）：复用旧章
                # 补缺页，而不是把整本判失败。
                reused = self._existing_chapters(book_id).get(c_idx)
                if reused is None:
                    issues.append(
                        ImportIssue(
                            scope="chapter",
                            code=STORAGE_CHAPTER_FAILED,
                            reason="章节序号已存在但无法复用",
                            chapter=_chapter_ref(c_idx, ch_title),
                        )
                    )
                    continue
                chapter_id = str(reused["id"])

            written = self._write_pages(
                chapter_id=chapter_id,
                ordinal=c_idx,
                title=ch_title,
                entries=lambda files=page_files, ch=ch_uuid: _open_entries(files, ch),
                default_font_id=default_font_id,
                style_defaults=style_defaults,
                issues=issues,
                existing_paths=(done.get("paths") if done is not None else None),
            )
            chapters_written += 1
            pages_written += written

        return self._result(
            book_id=book_id,
            title=comic["title"],
            chapters_written=chapters_written,
            pages_written=pages_written,
            issues=issues,
            resumed=resumed,
            comic_id=comic_id,
            skippedChapters=skipped_chapters,
        )

    def _adopt_legacy_book(self, title: str, *, kind: str, identifier: str) -> Optional[str]:
        from src.backend_v2.manga_import.source_tag import adopt_legacy_book

        return adopt_legacy_book(
            self.engine,
            owner_id=self.owner_id,
            kind=kind,
            identifier=identifier,
            legacy_title=title,
        )

    def _publish_cover(
        self,
        comic_dir: Path,
        comic: dict[str, Any],
        *,
        comic_id: str,
    ) -> tuple[Optional[str], Optional[ImportIssue]]:
        cover_name = str(comic.get("cover") or "").strip()
        if not cover_name:
            return None, None
        cover_path = comic_dir / cover_name
        if not cover_path.is_file():
            return None, ImportIssue(
                scope="cover",
                code=SOURCE_MISSING,
                reason=f"封面文件不存在：{cover_name}",
            )
        try:
            with cover_path.open("rb") as handle:
                cover_asset = self.importer.publish_cover(handle)
        except Exception as error:  # noqa: BLE001 - 封面失败不阻断导入
            return None, ImportIssue(
                scope="cover",
                code=STORAGE_COVER_FAILED,
                reason=str(error) or error.__class__.__name__,
            )
        return cover_asset.id, None


def _open_entries(
    files: Iterable[Path], chapter_uuid: str
) -> Iterable[tuple[int, str, Callable[[], BinaryIO]]]:
    for offset, item in enumerate(files, start=1):
        name = item.name
        yield offset, name, (lambda path=item: path.open("rb"))


def _chapter_complete(
    done: dict[str, object], logical_paths: set[str], expected: int
) -> bool:
    # logical_paths 用 ``<章节 uuid>/<文件名>``，库里存的也是这个形状；
    # 但本地导入写的是 ``<chapter_id>/<文件名>``，所以两边都按文件名集合比对。
    stored = {str(item).rsplit("/", 1)[-1] for item in (done.get("paths") or ())}
    return (
        int(done.get("pages") or 0) == expected
        and stored == {path.rsplit("/", 1)[-1] for path in logical_paths}
    )


def _digest(comic_id: str) -> str:
    from src.backend_v2.manga_import.source_tag import digest_for

    return digest_for("venera", comic_id)


def _venera_comic_dir(base_dir: Path, directory: object) -> Optional[Path]:
    name = str(directory or "").strip()
    if not name:
        return None
    candidate = base_dir / "local" / name
    try:
        candidate.resolve().relative_to((base_dir / "local").resolve())
    except (ValueError, OSError):
        return None
    return candidate


def _get_venera_comic_detail(base_dir: Path, comic_id: str) -> Optional[dict[str, Any]]:
    db_path = base_dir / "local.db"
    if not db_path.exists():
        return None
    uri = f"file:///{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT id, title, directory, cover, chapters, downloadedChapters FROM comics WHERE id = ?",
            (comic_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cid, title, directory, cover, chapters_json, downloaded_json = row
        chapters_data = json.loads(chapters_json) if chapters_json else {}
        downloaded = list(json.loads(downloaded_json) if downloaded_json else [])

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
    """API 入口：导入一本 Venera 漫画（保留旧签名，返回值向后兼容）。"""
    result = _VeneraImporter(engine=engine, importer=importer).run(
        base_dir=base_dir, comic_id=comic_id
    )
    result.setdefault("comic_id", comic_id)
    return result


__all__ = ["import_single_venera_comic"]
