"""漫画导入的幂等续传与部分失败上报。

背景（审计 P1-1）：Venera / 本地导入按「书 → 章 → 每 50 页」各自独立事务提交，
单章或单批次异常只写进 ``details.failed``，接口照样返回成功；前端因此只看到
「导入成功」，残缺的书与重试产生的重复书并存。

这里把导入拆成两层：

- ``_ImporterBase``：负责「找到/创建目标书」与「逐章搬页」，把每个失败点收敛成
  ``{"scope", "chapter", "page", "code", "reason"}`` 结构的 ``ImportIssue``，并在
  可重试的位置（封面、单张页面、单章）继续往下走而不是整体放弃；
- 溯源标签：书的标题里带一段隐藏标记（``⟦miyako:<kind>:<id>:<digest>⟧``，见
  ``src.backend_v2.manga_import.source_tag``）。同一本书重试时按标记命中已有书，
  已完成章节按「章序号 + 页数 + 逻辑页路径集合」比对后整章跳过，不再制造重复书。

所有 DB 写入仍然走真实 schema 表对象，不手写字段名。
"""

from __future__ import annotations

import json
import uuid
import zipfile
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Optional

from sqlalchemy import insert, select, update

from src.backend_v2.auth.authorization import effective_owner_id
from src.backend_v2.content.image_import import ImageImportService
from src.backend_v2.content.page_style import resolve_new_page_style
from src.backend_v2.manga_import.source_tag import find_imported_book
from src.backend_v2.storage.schema import books, chapters, page_assets, pages
from src.backend_v2.timestamps import utcnow

# P1-1 的问题分类（导入可部分成功）：
#   source_*    源数据本身有问题（目录不存在、文件名不合规、压缩包损坏）
#   storage_*   落盘/发布资产失败（单页可跳过，章级视情况继续）
#   book_*      整本书级别的前置问题（标题为空、章节序号冲突）
SOURCE_READ_FAILED = "source_read_failed"
SOURCE_INVALID = "source_invalid"
SOURCE_MISSING = "source_missing"
STORAGE_PAGE_FAILED = "storage_page_failed"
STORAGE_CHAPTER_FAILED = "storage_chapter_failed"
STORAGE_COVER_FAILED = "storage_cover_failed"
BOOK_INVALID = "book_invalid"
BOOK_NOT_FOUND = "book_not_found"

PAGE_BATCH_SIZE = 50


class ImportIssue(dict):
    """一条导入问题：既可直接 JSON 序列化，也能当作 dict 读取。"""

    def __init__(
        self,
        *,
        scope: str,
        code: str,
        reason: str,
        chapter: Optional[dict[str, object]] = None,
        page: Optional[int] = None,
    ) -> None:
        super().__init__(
            scope=scope,
            code=code,
            reason=reason,
            chapter=chapter,
            page=page,
        )


class MissingImages(Exception):
    """某一章在源里一张可用图片都没有。"""


def _chapter_ref(ordinal: int, title: str) -> dict[str, object]:
    return {"ordinal": ordinal, "title": title}


def _summarize_issues(issues: Iterable[ImportIssue]) -> dict[str, object]:
    counts = {
        "pagesFailed": 0,
        "chaptersFailed": 0,
        "bookFaults": 0,
        "coverFailed": 0,
    }
    for issue in issues:
        scope = issue["scope"]
        if scope == "page":
            counts["pagesFailed"] += 1
        elif scope == "chapter":
            counts["chaptersFailed"] += 1
        elif scope == "cover":
            counts["coverFailed"] += 1
        else:
            counts["bookFaults"] += 1
    return counts


def render_issues(issues: Iterable[dict[str, Any]]) -> list[str]:
    """把结构化 issue 渲染成给人看的整句，供日志与前端「可展开详情」共用。

    例：``第2话（ordinal=2）：章节文件路径不存在：D:\\manga\\ch2``
    """
    lines: list[str] = []
    for issue in issues:
        scope = str(issue.get("scope") or "book")
        chapter = issue.get("chapter") or {}
        page = issue.get("page")
        location = ""
        if scope == "page" and chapter:
            location = f"第{chapter.get('ordinal')}话 第{page}页（{chapter.get('title')}）："
        elif chapter:
            location = f"第{chapter.get('ordinal')}话（{chapter.get('title')}）："
        elif scope == "book":
            location = "整本书："
        elif scope == "cover":
            location = "封面："
        lines.append(f"{location}{issue.get('reason')}［{issue.get('code')}］")
    return lines


class _ImporterBase:
    """Venera / 本地两个导入器共用的「找书 + 搬章」骨架。"""

    def __init__(
        self,
        *,
        engine: object,
        importer: ImageImportService,
        owner_id: Optional[str] = None,
    ) -> None:
        self.engine = engine
        self.importer = importer
        self.owner_id = owner_id or effective_owner_id()

    # ---------------- 书 ----------------

    def _find_existing_book(self, *, kind: str, identifier: str, digest: str) -> Optional[str]:
        return find_imported_book(
            self.engine, owner_id=self.owner_id, kind=kind, identifier=identifier, digest=digest
        )

    def _create_book(self, *, title: str, cover_asset_id: Optional[str]) -> str:
        book_id = str(uuid.uuid4())
        now = utcnow()
        with self.engine.begin() as conn:
            conn.execute(
                insert(books).values(
                    id=book_id,
                    owner_user_id=self.owner_id,
                    kind="library",
                    title=title,
                    chapter_order_revision=1,
                    cover_asset_id=cover_asset_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        return book_id

    def _touch_cover(self, book_id: str, cover_asset_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(books)
                .where(books.c.id == book_id)
                .values(cover_asset_id=cover_asset_id, updated_at=utcnow())
            )

    # ---------------- 章 ----------------

    def _existing_chapters(self, book_id: str) -> dict[int, dict[str, object]]:
        """返回 ``{ordinal: {"id", "pages", "paths"}}``，供续传时判断章节是否已完整。"""
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(chapters.c.id, chapters.c.ordinal).where(chapters.c.book_id == book_id)
            ).all()
            if not rows:
                return {}
            by_id: dict[str, dict[str, object]] = {}
            result: dict[int, dict[str, object]] = {}
            for row in rows:
                entry: dict[str, object] = {"id": row.id, "pages": 0, "paths": set()}
                by_id[row.id] = entry
                result[int(row.ordinal)] = entry
            page_rows = conn.execute(
                select(pages.c.chapter_id, pages.c.logical_source_path).where(
                    pages.c.chapter_id.in_(tuple(by_id))
                )
            ).all()
        for row in page_rows:
            entry = by_id.get(row.chapter_id)
            if entry is None:
                continue
            entry["pages"] = int(entry["pages"]) + 1
            paths = entry["paths"]
            assert isinstance(paths, set)
            paths.add(row.logical_source_path)
        return result

    def _create_chapter(self, *, book_id: str, ordinal: int, title: str) -> str:
        chapter_id = str(uuid.uuid4())
        now = utcnow()
        with self.engine.begin() as conn:
            conn.execute(
                insert(chapters).values(
                    id=chapter_id,
                    book_id=book_id,
                    ordinal=ordinal,
                    title=title,
                    page_order_revision=1,
                    settings_memory_json="{}",
                    settings_memory_revision=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        return chapter_id

    def _resolve_page_style(self) -> tuple[Optional[str], dict[str, Any]]:
        with self.engine.connect() as conn:
            return resolve_new_page_style(conn)

    def _write_pages(
        self,
        *,
        chapter_id: str,
        ordinal: int,
        title: str,
        entries: Callable[[], Iterable[tuple[int, str, Callable[[], BinaryIO]]]],
        default_font_id: Optional[str],
        style_defaults: dict[str, Any],
        issues: list[ImportIssue],
        existing_paths: Optional[Iterable[str]] = None,
    ) -> int:
        """逐批发布资产并写页；单张发布失败只记一条 issue，不打断整章。

        ``existing_paths`` 用于续传：已在库里的逻辑页路径直接跳过，避免页序号
        唯一约束冲突，也避免同一个文件被重复发布成两份资产。
        """
        total = 0
        written = 0
        skip = set(existing_paths or ())
        batch: list[tuple[int, str, BinaryIO]] = []

        def flush_page(p_ordinal: int, fname: str, upload: BinaryIO) -> int:
            try:
                source_asset, thumb_asset = self.importer.publish_standalone_image(upload)
            except Exception as error:  # noqa: BLE001 - 单页失败必须可跳过
                issues.append(
                    ImportIssue(
                        scope="page",
                        code=STORAGE_PAGE_FAILED,
                        reason=str(error) or error.__class__.__name__,
                        chapter=_chapter_ref(ordinal, title),
                        page=p_ordinal,
                    )
                )
                return 0
            finally:
                try:
                    upload.close()
                except Exception:  # noqa: BLE001 - 关闭失败不影响导入
                    pass
            page_id = str(uuid.uuid4())
            now = utcnow()
            with self.engine.begin() as conn:
                conn.execute(
                    insert(pages).values(
                        id=page_id,
                        chapter_id=chapter_id,
                        ordinal=p_ordinal,
                        logical_source_path=f"{chapter_id}/{fname}",
                        source_revision=1,
                        document_revision=1,
                        render_status="not_rendered",
                        detection_state="unprocessed",
                        default_font_id=default_font_id,
                        page_style_defaults_json=json.dumps(style_defaults),
                        warnings_json="[]",
                        created_at=now,
                        updated_at=now,
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
            return 1

        try:
            for p_ordinal, fname, opener in entries():
                if skip and f"{chapter_id}/{fname}" in skip:
                    continue
                batch.append((p_ordinal, fname, opener()))
                if len(batch) >= PAGE_BATCH_SIZE:
                    for item in batch:
                        written += flush_page(*item)
                    total += 1
                    batch = []
            for item in batch:
                written += flush_page(*item)
        finally:
            for _, _, upload in batch:
                try:
                    upload.close()
                except Exception:  # noqa: BLE001 - 关闭失败不影响导入
                    pass
        return written

    # ---------------- 结果 ----------------

    @staticmethod
    def _result(
        *,
        book_id: Optional[str],
        title: str,
        chapters_written: int,
        pages_written: int,
        issues: list[ImportIssue],
        resumed: bool,
        **extra: object,
    ) -> dict[str, Any]:
        counts = _summarize_issues(issues)
        if counts["bookFaults"]:
            status = "failed"
        elif issues:
            status = "partial"
        else:
            status = "success"
        payload: dict[str, Any] = {
            "status": status,
            "book_id": book_id,
            "title": title,
            "chapters": chapters_written,
            "chaptersWritten": chapters_written,
            "pages": pages_written,
            "pagesWritten": pages_written,
            "resumed": resumed,
            "issues": [dict(issue) for issue in issues],
            "summary": counts,
        }
        payload.update(extra)
        return payload


__all__ = [
    "BOOK_INVALID",
    "BOOK_NOT_FOUND",
    "ImportIssue",
    "MissingImages",
    "PAGE_BATCH_SIZE",
    "SOURCE_INVALID",
    "SOURCE_MISSING",
    "SOURCE_READ_FAILED",
    "STORAGE_CHAPTER_FAILED",
    "STORAGE_COVER_FAILED",
    "STORAGE_PAGE_FAILED",
    "_ImporterBase",
    "_chapter_ref",
    "_summarize_issues",
    "render_issues",
]
