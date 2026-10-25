"""本地漫画导入：可续传、可部分成功、失败逐条上报。

旧的 ``local_confirm.py`` 用 ``except Exception: pass`` 吞掉封面失败，且每次确认都
新建 book_id；这里复用与 Venera 导入相同的骨架：

- 书标题带 ``local:<指纹>`` 溯源标记（指纹 = 标题 + 各章 (序号, 类型, 路径)），
  同一批书重试命中已有书并整章跳过已完成章节；
- 章节级失败（路径消失、压缩包损坏、章序号冲突）与页面级失败分别记 issue，
  但仍继续处理其余章节；
- 封面失败记 ``cover`` 级 issue，不再静默。
"""

from __future__ import annotations

import hashlib
import os
import zipfile
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
from src.backend_v2.manga_import.local_preview import (
    is_valid_image_filename,
    natural_sort_key,
)
from src.backend_v2.manga_import.source_tag import build_title


def book_signature(*, title: str, chapters: Iterable[dict[str, Any]]) -> str:
    """由标题与章节列表派生稳定指纹，用于「同一本书」的识别。"""
    parts = [title.strip()]
    for item in chapters:
        ordinal = item.get("ordinal")
        source_type = item.get("source_type") or "folder"
        path = Path(str(item.get("source_path") or "")).resolve()
        parts.append(f"{ordinal}|{source_type}|{path}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:24]


class _LocalImporter(_ImporterBase):
    def run(self, book_data: dict[str, Any]) -> dict[str, Any]:
        title = (book_data.get("title") or "").strip()
        if not title:
            return self._book_fault(BOOK_INVALID, "漫画标题不能为空")

        chapters_input = book_data.get("chapters") or []
        if not isinstance(chapters_input, list) or not chapters_input:
            return self._book_fault(BOOK_INVALID, f"漫画 '{title}' 缺少有效的章节列表")

        issues: list[ImportIssue] = []
        validated: list[dict[str, Any]] = []
        ordinals_seen: set[int] = set()
        for idx, raw in enumerate(chapters_input, start=1):
            ch_title = (raw.get("title") or "").strip() or f"第{idx}话"
            ordinal = raw.get("ordinal")
            if ordinal is None:
                issues.append(
                    ImportIssue(
                        scope="chapter",
                        code=SOURCE_INVALID,
                        reason="缺少章节序号（ordinal），请在前端手动指定顺序",
                        chapter=_chapter_ref(idx, ch_title),
                    )
                )
                continue
            try:
                ordinal_value = int(ordinal)
            except (TypeError, ValueError):
                issues.append(
                    ImportIssue(
                        scope="chapter",
                        code=SOURCE_INVALID,
                        reason=f"章节序号无效：{ordinal}",
                        chapter=_chapter_ref(idx, ch_title),
                    )
                )
                continue
            if ordinal_value < 1:
                issues.append(
                    ImportIssue(
                        scope="chapter",
                        code=SOURCE_INVALID,
                        reason=f"章节序号必须大于等于 1：{ordinal_value}",
                        chapter=_chapter_ref(ordinal_value, ch_title),
                    )
                )
                continue
            if ordinal_value in ordinals_seen:
                issues.append(
                    ImportIssue(
                        scope="chapter",
                        code=SOURCE_INVALID,
                        reason=f"章节序号重复（ordinal={ordinal_value}）",
                        chapter=_chapter_ref(ordinal_value, ch_title),
                    )
                )
                continue
            ordinals_seen.add(ordinal_value)

            source_type = raw.get("source_type") or "folder"
            source_path = Path(str(raw.get("source_path") or "")).resolve()
            if not source_path.exists():
                issues.append(
                    ImportIssue(
                        scope="chapter",
                        code=SOURCE_MISSING,
                        reason=f"章节文件路径不存在：{source_path}",
                        chapter=_chapter_ref(ordinal_value, ch_title),
                    )
                )
                continue
            validated.append(
                {
                    "title": ch_title,
                    "ordinal": ordinal_value,
                    "source_type": source_type,
                    "path": source_path,
                }
            )

        validated.sort(key=lambda item: int(item["ordinal"]))
        if not validated:
            result = self._book_fault(BOOK_INVALID, f"漫画 '{title}' 没有可导入的章节")
            result["issues"] = [dict(issue) for issue in issues]
            result["summary"] = _summary(issues)
            return result

        signature = book_signature(title=title, chapters=validated)
        wire_title = build_title(title, kind="local", identifier=signature)
        existing = self._find_existing_book(kind="local", identifier=signature, digest=_digest(signature))
        if existing is None:
            # 兼容 v0.1.0 及更早导入的书（标题无标记）：命中就补标记，避免重试
            # 造成重复书。
            from src.backend_v2.manga_import.source_tag import adopt_legacy_book

            existing = adopt_legacy_book(
                self.engine,
                owner_id=self.owner_id,
                kind="local",
                identifier=signature,
                legacy_title=title,
            )
        resumed = existing is not None

        if existing is not None:
            book_id = existing
        else:
            cover_asset_id, cover_issue = self._publish_cover(book_data, validated)
            if cover_issue is not None:
                issues.append(cover_issue)
            book_id = self._create_book(title=wire_title, cover_asset_id=cover_asset_id)

        default_font_id, style_defaults = self._resolve_page_style()
        done_chapters = self._existing_chapters(book_id) if resumed else {}

        chapters_written = 0
        pages_written = 0
        skipped: list[dict[str, object]] = []

        for info in validated:
            ordinal = int(info["ordinal"])
            ch_title = str(info["title"])
            try:
                entries = _entries_for(info)
                if not entries:
                    issues.append(
                        ImportIssue(
                            scope="chapter",
                            code=SOURCE_INVALID,
                            reason="章节里没有可用的图片文件",
                            chapter=_chapter_ref(ordinal, ch_title),
                        )
                    )
                    continue
                logical_paths = {name for _, name, _ in entries}
                done = done_chapters.get(ordinal)
                if done is not None and _chapter_complete(done, logical_paths):
                    skipped.append(_chapter_ref(ordinal, ch_title))
                    continue
                try:
                    chapter_id = self._create_chapter(
                        book_id=book_id, ordinal=ordinal, title=ch_title
                    )
                except IntegrityError:
                    # 续传时同 ordinal 的章记录已存在（上次写到一半崩了）：
                    # 复用旧章补缺页，而不是把整本判失败。
                    reused = self._existing_chapters(book_id).get(ordinal)
                    if reused is None:
                        raise
                    chapter_id = str(reused["id"])
            except (OSError, zipfile.BadZipFile) as error:
                issues.append(
                    ImportIssue(
                        scope="chapter",
                        code=SOURCE_READ_FAILED,
                        reason=f"读取章节失败：{error}",
                        chapter=_chapter_ref(ordinal, ch_title),
                    )
                )
                continue
            except Exception as error:  # noqa: BLE001 - 单章失败不打断整本书
                issues.append(
                    ImportIssue(
                        scope="chapter",
                        code=STORAGE_CHAPTER_FAILED,
                        reason=f"创建章节记录失败：{error}",
                        chapter=_chapter_ref(ordinal, ch_title),
                    )
                )
                continue

            written = self._write_pages(
                chapter_id=chapter_id,
                ordinal=ordinal,
                title=ch_title,
                entries=lambda info=info: _entries_for(info),
                default_font_id=default_font_id,
                style_defaults=style_defaults,
                issues=issues,
                existing_paths=(done.get("paths") if done is not None else None),
            )
            chapters_written += 1
            pages_written += written

        return self._result(
            book_id=book_id,
            title=title,
            chapters_written=chapters_written,
            pages_written=pages_written,
            issues=issues,
            resumed=resumed,
            skippedChapters=skipped,
        )

    def _book_fault(self, code: str, reason: str) -> dict[str, Any]:
        return self._result(
            book_id=None,
            title="",
            chapters_written=0,
            pages_written=0,
            issues=[ImportIssue(scope="book", code=code, reason=reason)],
            resumed=False,
        )

    def _publish_cover(
        self, book_data: dict[str, Any], validated: list[dict[str, Any]]
    ) -> tuple[Optional[str], Optional[ImportIssue]]:
        cover_source = book_data.get("cover_source_path")
        if cover_source and Path(cover_source).exists():
            try:
                with open(cover_source, "rb") as handle:
                    asset = self.importer.publish_cover(handle)
                return asset.id, None
            except Exception as error:  # noqa: BLE001 - 回退到首图封面
                first_error = ImportIssue(
                    scope="cover",
                    code=STORAGE_COVER_FAILED,
                    reason=f"指定封面发布失败：{error}",
                )
            else:  # pragma: no cover - 上面的 except 已覆盖
                first_error = None
        else:
            first_error = None

        # 回退：第一章的第一张图
        first = validated[0]
        try:
            entries = _entries_for(first)
            if entries:
                _, _, opener = entries[0]
                upload = opener()
                try:
                    asset = self.importer.publish_cover(upload)
                finally:
                    upload.close()
                return asset.id, first_error
        except Exception as error:  # noqa: BLE001 - 封面缺失不阻断导入
            return None, first_error or ImportIssue(
                scope="cover",
                code=STORAGE_COVER_FAILED,
                reason=f"首图封面发布失败：{error}",
            )
        return None, first_error


def _entries_for(info: dict[str, Any]) -> list[tuple[int, str, Callable[[], BinaryIO]]]:
    """把一章展开成 ``[(页序号, 文件名, 打开器)]``。"""
    path = Path(info["path"])
    result: list[tuple[int, str, Callable[[], BinaryIO]]] = []
    if info["source_type"] == "folder":
        files = [f for f in path.iterdir() if f.is_file() and is_valid_image_filename(f.name)]
        files.sort(key=lambda f: natural_sort_key(f.name))
        for offset, item in enumerate(files, start=1):
            result.append((offset, item.name, lambda p=item: p.open("rb")))
        return result

    if info["source_type"] == "archive":
        archive = zipfile.ZipFile(path, "r")
        names = [name for name in archive.namelist() if is_valid_image_filename(name)]
        names.sort(key=natural_sort_key)
        for offset, name in enumerate(names, start=1):
            result.append(
                (offset, os.path.basename(name), lambda n=name, z=archive: z.open(n))
            )
        return result

    raise ValueError(f"不支持的章节来源类型：{info['source_type']}")


def _chapter_complete(done: dict[str, object], expected_paths: set[str]) -> bool:
    paths = set(done.get("paths") or ())
    if not paths:
        return False
    # 本地章节的页名是文件名，库里存的是 ``<chapter_id>/<文件名>``；按文件名比对。
    stored_names = {str(p).rsplit("/", 1)[-1] for p in paths}
    return stored_names == expected_paths and int(done.get("pages") or 0) == len(expected_paths)


def _digest(signature: str) -> str:
    from src.backend_v2.manga_import.source_tag import digest_for

    return digest_for("local", signature)


def _summary(issues: list[ImportIssue]) -> dict[str, object]:
    from src.backend_v2.manga_import.importer_base import _summarize_issues

    return _summarize_issues(issues)


def import_single_local_book(
    book_data: dict[str, Any],
    *,
    data_root: Path,
    engine: object,
    importer: ImageImportService,
) -> dict[str, Any]:
    """API 入口：导入一本本地漫画（保留旧签名）。"""
    return _LocalImporter(engine=engine, importer=importer).run(book_data)


__all__ = ["book_signature", "import_single_local_book"]
