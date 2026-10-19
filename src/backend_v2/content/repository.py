"""Relational content repository with revision CAS and atomic ordinals."""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any
import uuid

from sqlalchemy import Engine, and_, delete, exists, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from src.backend_v2.auth.ownership import effective_owner_id
from src.backend_v2.serialization import canonical_json as _json
from src.backend_v2.timestamps import iso_utc, utcnow as _utcnow
# translation_constraints imports removed
from src.backend_v2.content.page_style import (
    validate_text_style_defaults,
)
from src.backend_v2.insight.repository import (
    mark_book_insight_derived_stale,
)
from src.backend_v2.storage.assets import AssetRecord, AssetStorageService
from src.backend_v2.storage.database import immediate_transaction
from src.backend_v2.storage.schema import (
    ACTIVE_OPERATION_STATUSES,
    NONTERMINAL_JOB_STATUSES,
    assets,
    book_tags,
    books,
    chapter_write_locks,
    chapters,
    idempotency_records,
    jobs,
    operations,
    page_assets,
    pages,
    studio_chat_sessions,
    studio_documents,
    tags,
)


class ContentNotFound(LookupError):
    pass


class ContentConflict(RuntimeError):
    pass


class ContentLocked(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.details = details


class IdempotencyConflict(ContentConflict):
    pass


def normalize_logical_path(raw_path: str) -> str:
    normalized = raw_path.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or any(part in ("", ".") for part in path.parts)
    ):
        raise ValueError("logical source path must be a normalized relative path")
    return path.as_posix()


def deduplicate_logical_path(requested: str, existing: set[str]) -> str:
    if requested not in existing:
        return requested
    path = PurePosixPath(requested)
    suffix = path.suffix
    stem = path.name[: -len(suffix)] if suffix else path.name
    parent = "" if str(path.parent) == "." else f"{path.parent.as_posix()}/"
    counter = 2
    while True:
        candidate = f"{parent}{stem} ({counter}){suffix}"
        if candidate not in existing:
            return candidate
        counter += 1


def natural_sort_key(value: object) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", str(value))
    )


class ContentRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        database_path = engine.url.database
        self.storage = (
            AssetStorageService(Path(database_path).resolve().parent, engine)
            if database_path and database_path != ":memory:"
            else None
        )

    def list_books(
        self,
        *,
        search: str = "",
        tag_ids: tuple[str, ...] = (),
        sort_by: str = "updated_at",
        sort_order: str = "desc",
    ) -> list[dict[str, object]]:
        if len(tag_ids) != len(set(tag_ids)):
            raise ValueError("tagIds must contain unique IDs")
        if sort_by not in {"title", "created_at", "updated_at"}:
            raise ValueError("sort_by must be title, created_at, or updated_at")
        if sort_order not in {"asc", "desc"}:
            raise ValueError("sort_order must be asc or desc")
        chapter_counts = (
            select(
                chapters.c.book_id,
                func.count(chapters.c.id).label("chapter_count"),
            )
            .group_by(chapters.c.book_id)
            .subquery()
        )
        page_counts = (
            select(
                chapters.c.book_id,
                func.count(pages.c.id).label("page_count"),
            )
            .select_from(chapters.join(pages, pages.c.chapter_id == chapters.c.id))
            .group_by(chapters.c.book_id)
            .subquery()
        )
        statement = (
            select(
                books,
                func.coalesce(chapter_counts.c.chapter_count, 0).label("chapter_count"),
                func.coalesce(page_counts.c.page_count, 0).label("page_count"),
            )
            .outerjoin(chapter_counts, chapter_counts.c.book_id == books.c.id)
            .outerjoin(page_counts, page_counts.c.book_id == books.c.id)
            .where(
                books.c.kind == "library",
                books.c.owner_user_id == effective_owner_id(),
            )
        )
        if sort_by != "title":
            sort_column = (
                books.c.created_at
                if sort_by == "created_at"
                else books.c.updated_at
            )
            statement = statement.order_by(
                sort_column.asc() if sort_order == "asc" else sort_column.desc(),
                books.c.id,
            )
        normalized_search = search.strip()
        if normalized_search:
            pattern = f"%{normalized_search}%"
            statement = statement.where(
                or_(
                    books.c.title.ilike(pattern),
                    books.c.id.in_(
                        select(book_tags.c.book_id)
                        .join(tags, tags.c.id == book_tags.c.tag_id)
                        .where(tags.c.name.ilike(pattern))
                    ),
                )
            )
        if tag_ids:
            statement = statement.where(
                books.c.id.in_(
                    select(book_tags.c.book_id)
                    .where(book_tags.c.tag_id.in_(tag_ids))
                    .group_by(book_tags.c.book_id)
                    .having(func.count(func.distinct(book_tags.c.tag_id)) == len(tag_ids))
                )
            )
        with self.engine.connect() as connection:
            rows = list(connection.execute(statement).mappings())
            tag_rows = list(
                connection.execute(
                    select(book_tags.c.book_id, tags.c.id, tags.c.name, tags.c.color)
                    .join(tags, tags.c.id == book_tags.c.tag_id)
                    .where(book_tags.c.book_id.in_([row["id"] for row in rows]))
                    .order_by(book_tags.c.book_id, tags.c.name, tags.c.id)
                ).mappings()
            ) if rows else []
            job_rows = list(
                connection.execute(
                    select(
                        jobs.c.book_id,
                        jobs.c.status,
                        func.count(jobs.c.id).label("job_count"),
                    )
                    .where(
                        jobs.c.book_id.in_([row["id"] for row in rows]),
                        jobs.c.status.in_(
                            (
                                "queued",
                                "running",
                                "paused",
                                "interrupted",
                                "failed",
                            )
                        ),
                    )
                    .group_by(jobs.c.book_id, jobs.c.status)
                ).mappings()
            ) if rows else []
        if sort_by == "title":
            rows.sort(
                key=lambda row: natural_sort_key(row["title"]),
                reverse=sort_order == "desc",
            )
        tags_by_book: dict[str, list[dict[str, str]]] = {}
        for row in tag_rows:
            tags_by_book.setdefault(str(row["book_id"]), []).append(
                {"id": str(row["id"]), "name": str(row["name"]), "color": str(row["color"])}
            )
        jobs_by_book: dict[str, dict[str, int]] = {}
        for row in job_rows:
            jobs_by_book.setdefault(str(row["book_id"]), {})[
                str(row["status"])
            ] = int(row["job_count"])
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "coverAssetUrl": (
                    f"/api/v2/assets/{row['cover_asset_id']}"
                    if row["cover_asset_id"]
                    else None
                ),
                "chapterCount": row["chapter_count"],
                "pageCount": row["page_count"],
                "tags": tags_by_book.get(str(row["id"]), []),
                "jobStatusSummary": jobs_by_book.get(str(row["id"]), {}),
                "chapterOrderRevision": row["chapter_order_revision"],
                "createdAt": iso_utc(row["created_at"]),
                "updatedAt": iso_utc(row["updated_at"]),
            }
            for row in rows
        ]

    def create_book(
        self,
        *,
        title: str,
        tag_ids: list[str] | None = None,
        cover_asset_id: str | None = None,
    ) -> dict[str, object]:
        normalized = title.strip()
        if not normalized or len(normalized) > 500:
            raise ValueError("book title must contain 1-500 characters")
        book_id = str(uuid.uuid4())
        with self.engine.begin() as connection:
            connection.execute(
                insert(books).values(
                    id=book_id,
                    owner_user_id=effective_owner_id(),
                    kind="library",
                    title=normalized,
                    cover_asset_id=cover_asset_id,
                )
            )
            if tag_ids is not None:
                self._replace_book_tags(connection, book_id, tag_ids)
        return {
            "id": book_id,
            "title": normalized,
            "chapterOrderRevision": 1,
            "coverAssetUrl": (
                f"/api/v2/assets/{cover_asset_id}"
                if cover_asset_id
                else None
            ),
        }

    def get_book(self, book_id: str) -> dict[str, object]:
        with self.engine.connect() as connection:
            book = connection.execute(
                select(books).where(
                    books.c.id == book_id,
                    books.c.kind == "library",
                    books.c.owner_user_id == effective_owner_id(),
                )
            ).mappings().one_or_none()
            if book is None:
                raise ContentNotFound("book not found")
            chapter_rows = list(
                connection.execute(
                    select(
                        chapters.c.id,
                        chapters.c.ordinal,
                        chapters.c.title,
                        chapters.c.page_order_revision,
                        func.count(pages.c.id).label("page_count"),
                    )
                    .outerjoin(pages, pages.c.chapter_id == chapters.c.id)
                    .where(chapters.c.book_id == book_id)
                    .group_by(chapters.c.id)
                    .order_by(chapters.c.ordinal)
                ).mappings()
            )
            tag_rows = list(
                connection.execute(
                    select(tags.c.id, tags.c.name, tags.c.color)
                    .join(book_tags, book_tags.c.tag_id == tags.c.id)
                    .where(book_tags.c.book_id == book_id)
                    .order_by(tags.c.name)
                ).mappings()
            )
            chapter_job_rows = list(
                connection.execute(
                    select(
                        jobs.c.chapter_id,
                        jobs.c.status,
                        func.count(jobs.c.id).label("job_count"),
                    )
                    .where(
                        jobs.c.chapter_id.in_(
                            [row["id"] for row in chapter_rows]
                        ),
                        jobs.c.kind == "translation",
                        jobs.c.status.in_(
                            (
                                "queued",
                                "running",
                                "paused",
                                "interrupted",
                                "failed",
                            )
                        ),
                    )
                    .group_by(jobs.c.chapter_id, jobs.c.status)
                ).mappings()
            ) if chapter_rows else []
        chapter_jobs: dict[str, dict[str, int]] = {}
        for row in chapter_job_rows:
            chapter_jobs.setdefault(str(row["chapter_id"]), {})[
                str(row["status"])
            ] = int(row["job_count"])
        return {
            "id": book["id"],
            "title": book["title"],
            "coverAssetUrl": (
                f"/api/v2/assets/{book['cover_asset_id']}"
                if book["cover_asset_id"]
                else None
            ),
            "chapterOrderRevision": book["chapter_order_revision"],
            "createdAt": iso_utc(book["created_at"]),
            "updatedAt": iso_utc(book["updated_at"]),
            "tags": [dict(row) for row in tag_rows],
            "chapters": [
                {
                    "id": row["id"],
                    "ordinal": row["ordinal"],
                    "title": row["title"],
                    "pageCount": row["page_count"],
                    "pageOrderRevision": row["page_order_revision"],
                    "jobStatusSummary": chapter_jobs.get(str(row["id"]), {}),
                }
                for row in chapter_rows
            ],
        }

    def update_book(
        self,
        *,
        book_id: str,
        title: str | None = None,
        tag_ids: list[str] | None = None,
        cover_asset_id: str | None = None,
        replace_cover: bool = False,
    ) -> dict[str, object]:
        normalized_title: str | None = None
        if title is not None:
            normalized_title = title.strip()
            if not normalized_title or len(normalized_title) > 500:
                raise ValueError("book title must contain 1-500 characters")
        with immediate_transaction(self.engine) as connection:
            values: dict[str, object] = {"updated_at": _utcnow()}
            if normalized_title is not None:
                values["title"] = normalized_title
            if replace_cover:
                values["cover_asset_id"] = cover_asset_id
            changed = connection.execute(
                update(books)
                .where(
                    books.c.id == book_id,
                    books.c.kind == "library",
                    books.c.owner_user_id == effective_owner_id(),
                )
                .values(**values)
            )
            if changed.rowcount != 1:
                raise ContentNotFound("book not found")
            if tag_ids is not None:
                self._replace_book_tags(connection, book_id, tag_ids)
        return self.get_book(book_id)

    def delete_book(self, book_id: str) -> None:
        with immediate_transaction(self.engine) as connection:
            kind = connection.execute(
                select(books.c.kind).where(
                    books.c.id == book_id,
                    books.c.owner_user_id == effective_owner_id(),
                )
            ).scalar_one_or_none()
            if kind != "library":
                raise ContentNotFound("book not found")
            chapter_ids = [
                str(value)
                for value in connection.execute(
                    select(chapters.c.id).where(chapters.c.book_id == book_id)
                ).scalars()
            ]
            self._assert_targets_idle(connection, book_id, chapter_ids)
            connection.execute(
                delete(books).where(
                    books.c.id == book_id,
                    books.c.owner_user_id == effective_owner_id(),
                )
            )

    def batch_delete_books(
        self,
        book_ids: list[str],
    ) -> dict[str, list[dict[str, str]] | list[str]]:
        if not book_ids or len(book_ids) != len(set(book_ids)):
            raise ValueError("bookIds must contain unique IDs")
        deleted: list[str] = []
        rejected: list[dict[str, str]] = []
        for book_id in book_ids:
            try:
                self.delete_book(book_id)
                deleted.append(book_id)
            except (ContentLocked, ContentNotFound) as exc:
                rejected.append(
                    {
                        "bookId": book_id,
                        "reason": (
                            "locked"
                            if isinstance(exc, ContentLocked)
                            else "not_found"
                        ),
                        "message": str(exc),
                    }
                )
        return {"deleted": deleted, "rejected": rejected}

    def update_chapter(self, *, chapter_id: str, title: str) -> dict[str, object]:
        normalized = title.strip()
        if not normalized or len(normalized) > 500:
            raise ValueError("chapter title must contain 1-500 characters")
        with immediate_transaction(self.engine) as connection:
            row = connection.execute(
                select(chapters.c.book_id)
                .join(books, books.c.id == chapters.c.book_id)
                .where(
                    chapters.c.id == chapter_id,
                    books.c.owner_user_id == effective_owner_id(),
                )
            ).mappings().one_or_none()
            if row is None:
                raise ContentNotFound("chapter not found")
            self._assert_targets_idle(
                connection,
                str(row["book_id"]),
                [chapter_id],
            )
            connection.execute(
                update(chapters)
                .where(chapters.c.id == chapter_id)
                .values(title=normalized, updated_at=_utcnow())
            )
        return {"id": chapter_id, "title": normalized}

    def delete_chapter(self, chapter_id: str) -> None:
        with immediate_transaction(self.engine) as connection:
            row = connection.execute(
                select(
                    chapters.c.book_id,
                    chapters.c.ordinal,
                    books.c.kind,
                )
                .join(books, books.c.id == chapters.c.book_id)
                .where(
                    chapters.c.id == chapter_id,
                    books.c.owner_user_id == effective_owner_id(),
                )
            ).mappings().one_or_none()
            if row is None or row["kind"] != "library":
                raise ContentNotFound("chapter not found")
            book_id = str(row["book_id"])
            self._assert_targets_idle(connection, book_id, [chapter_id])
            if connection.execute(
                select(exists().where(pages.c.chapter_id == chapter_id))
            ).scalar_one():
                mark_book_insight_derived_stale(
                    connection,
                    book_id=book_id,
                    now=_utcnow(),
                )
            connection.execute(delete(chapters).where(chapters.c.id == chapter_id))
            following_ids = connection.execute(
                select(chapters.c.id)
                .where(
                    chapters.c.book_id == book_id,
                    chapters.c.ordinal > row["ordinal"],
                )
                .order_by(chapters.c.ordinal)
            ).scalars().all()
            for following_id in following_ids:
                connection.execute(
                    update(chapters)
                    .where(chapters.c.id == following_id)
                    .values(ordinal=chapters.c.ordinal - 1)
                )
            connection.execute(
                update(books)
                .where(books.c.id == book_id)
                .values(
                    chapter_order_revision=books.c.chapter_order_revision + 1,
                    updated_at=_utcnow(),
                )
            )

    def list_tags(self) -> list[dict[str, object]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    tags,
                    func.count(book_tags.c.book_id).label("book_count"),
                )
                .outerjoin(book_tags, book_tags.c.tag_id == tags.c.id)
                .where(tags.c.owner_user_id == effective_owner_id())
                .group_by(tags.c.id)
                .order_by(tags.c.name)
            ).mappings()
            return [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "color": row["color"],
                    "bookCount": row["book_count"],
                }
                for row in rows
            ]

    def create_tag(self, *, name: str, color: str) -> dict[str, object]:
        normalized_name = name.strip()
        normalized_color = self._normalize_color(color)
        if not normalized_name or len(normalized_name) > 200:
            raise ValueError("tag name must contain 1-200 characters")
        tag_id = str(uuid.uuid4())
        try:
            with immediate_transaction(self.engine) as connection:
                connection.execute(
                    insert(tags).values(
                        id=tag_id,
                        owner_user_id=effective_owner_id(),
                        name=normalized_name,
                        color=normalized_color,
                    )
                )
        except IntegrityError as exc:
            raise ContentConflict("tag name already exists") from exc
        return {
            "id": tag_id,
            "name": normalized_name,
            "color": normalized_color,
            "bookCount": 0,
        }

    def update_tag(
        self,
        *,
        tag_id: str,
        name: str,
        color: str,
    ) -> dict[str, object]:
        normalized_name = name.strip()
        normalized_color = self._normalize_color(color)
        if not normalized_name or len(normalized_name) > 200:
            raise ValueError("tag name must contain 1-200 characters")
        try:
            with immediate_transaction(self.engine) as connection:
                changed = connection.execute(
                    update(tags)
                    .where(
                        tags.c.id == tag_id,
                        tags.c.owner_user_id == effective_owner_id(),
                    )
                    .values(
                        name=normalized_name,
                        color=normalized_color,
                        updated_at=_utcnow(),
                    )
                )
                if changed.rowcount != 1:
                    raise ContentNotFound("tag not found")
        except IntegrityError as exc:
            raise ContentConflict("tag name already exists") from exc
        return {
            "id": tag_id,
            "name": normalized_name,
            "color": normalized_color,
        }

    def delete_tag(self, tag_id: str) -> None:
        with immediate_transaction(self.engine) as connection:
            removed = connection.execute(
                delete(tags).where(
                    tags.c.id == tag_id,
                    tags.c.owner_user_id == effective_owner_id(),
                )
            )
            if removed.rowcount != 1:
                raise ContentNotFound("tag not found")

    def batch_update_tags(
        self,
        *,
        book_ids: list[str],
        tag_ids: list[str],
        action: str,
    ) -> None:
        if action not in {"add", "remove"}:
            raise ValueError("tag action must be add or remove")
        if not book_ids or len(book_ids) != len(set(book_ids)):
            raise ValueError("bookIds must contain unique IDs")
        if len(tag_ids) != len(set(tag_ids)):
            raise ValueError("tagIds must contain unique IDs")
        with immediate_transaction(self.engine) as connection:
            existing_books = set(
                connection.execute(
                    select(books.c.id).where(
                        books.c.id.in_(book_ids),
                        books.c.kind == "library",
                        books.c.owner_user_id == effective_owner_id(),
                    )
                ).scalars()
            )
            existing_tags = set(
                connection.execute(
                    select(tags.c.id).where(
                        tags.c.id.in_(tag_ids),
                        tags.c.owner_user_id == effective_owner_id(),
                    )
                ).scalars()
            )
            if existing_books != set(book_ids) or existing_tags != set(tag_ids):
                raise ContentNotFound("book or tag not found")
            if action == "add" and tag_ids:
                connection.execute(
                    insert(book_tags).prefix_with("OR IGNORE"),
                    [
                        {"book_id": book_id, "tag_id": tag_id}
                        for book_id in book_ids
                        for tag_id in tag_ids
                    ],
                )
            elif tag_ids:
                connection.execute(
                    delete(book_tags).where(
                        book_tags.c.book_id.in_(book_ids),
                        book_tags.c.tag_id.in_(tag_ids),
                    )
                )

    def create_chapter(self, *, book_id: str, title: str) -> dict[str, object]:
        normalized = title.strip()
        if not normalized or len(normalized) > 500:
            raise ValueError("chapter title must contain 1-500 characters")
        chapter_id = str(uuid.uuid4())
        with immediate_transaction(self.engine) as connection:
            book_revision = connection.execute(
                select(books.c.chapter_order_revision).where(
                    books.c.id == book_id,
                    books.c.kind.in_(("library", "quick_workspace")),
                    books.c.owner_user_id == effective_owner_id(),
                )
            ).scalar_one_or_none()
            if book_revision is None:
                raise ContentNotFound("book not found")
            ordinal = (
                connection.execute(
                    select(func.coalesce(func.max(chapters.c.ordinal), 0)).where(
                        chapters.c.book_id == book_id
                    )
                ).scalar_one()
                + 1
            )
            connection.execute(
                insert(chapters).values(
                    id=chapter_id,
                    book_id=book_id,
                    ordinal=ordinal,
                    title=normalized,
                )
            )
            connection.execute(
                update(books)
                .where(
                    books.c.id == book_id,
                    books.c.chapter_order_revision == book_revision,
                )
                .values(
                    chapter_order_revision=book_revision + 1,
                    updated_at=_utcnow(),
                )
            )
        return {
            "id": chapter_id,
            "bookId": book_id,
            "ordinal": ordinal,
            "title": normalized,
            "pageOrderRevision": 1,
        }

    def list_chapters(self, book_id: str) -> dict[str, object]:
        with self.engine.connect() as connection:
            book = connection.execute(
                select(
                    books.c.id,
                    books.c.title,
                    books.c.kind,
                    books.c.chapter_order_revision,
                ).where(
                    books.c.id == book_id,
                    books.c.owner_user_id == effective_owner_id(),
                )
            ).mappings().one_or_none()
            if book is None:
                raise ContentNotFound("book not found")
            rows = list(
                connection.execute(
                    select(
                        chapters.c.id,
                        chapters.c.ordinal,
                        chapters.c.title,
                        chapters.c.page_order_revision,
                        func.count(pages.c.id).label("page_count"),
                    )
                    .outerjoin(pages, pages.c.chapter_id == chapters.c.id)
                    .where(chapters.c.book_id == book_id)
                    .group_by(chapters.c.id)
                    .order_by(chapters.c.ordinal)
                ).mappings()
            )
        return {
            "book": dict(book),
            "chapters": [
                {
                    "id": row["id"],
                    "ordinal": row["ordinal"],
                    "title": row["title"],
                    "pageCount": row["page_count"],
                    "pageOrderRevision": row["page_order_revision"],
                }
                for row in rows
            ],
        }

    def reorder_chapters(
        self,
        *,
        book_id: str,
        ordered_ids: list[str],
        base_revision: int,
    ) -> int:
        with immediate_transaction(self.engine) as connection:
            revision = connection.execute(
                select(books.c.chapter_order_revision).where(
                    books.c.id == book_id,
                    books.c.owner_user_id == effective_owner_id(),
                )
            ).scalar_one_or_none()
            if revision is None:
                raise ContentNotFound("book not found")
            if revision != base_revision:
                raise ContentConflict("chapter order revision changed")
            existing = list(
                connection.execute(
                    select(chapters.c.id)
                    .where(chapters.c.book_id == book_id)
                    .order_by(chapters.c.ordinal)
                ).scalars()
            )
            if len(ordered_ids) != len(set(ordered_ids)) or set(existing) != set(ordered_ids):
                raise ValueError("ordered chapter ids must be an exact permutation")
            offset = len(existing) * 2 + 1
            connection.execute(
                update(chapters)
                .where(chapters.c.book_id == book_id)
                .values(ordinal=chapters.c.ordinal + offset)
            )
            for ordinal, chapter_id in enumerate(ordered_ids, start=1):
                connection.execute(
                    update(chapters)
                    .where(chapters.c.id == chapter_id, chapters.c.book_id == book_id)
                    .values(ordinal=ordinal)
                )
            connection.execute(
                update(books)
                .where(
                    books.c.id == book_id,
                    books.c.chapter_order_revision == base_revision,
                )
                .values(
                    chapter_order_revision=base_revision + 1,
                    updated_at=_utcnow(),
                )
            )
            mark_book_insight_derived_stale(
                connection,
                book_id=book_id,
                now=_utcnow(),
            )
        return base_revision + 1

    def reorder_pages(
        self,
        *,
        chapter_id: str,
        ordered_ids: list[str],
        base_revision: int,
    ) -> int:
        with immediate_transaction(self.engine) as connection:
            chapter = connection.execute(
                select(
                    chapters.c.book_id,
                    chapters.c.page_order_revision,
                ).where(chapters.c.id == chapter_id)
            ).mappings().one_or_none()
            if chapter is None:
                raise ContentNotFound("chapter not found")
            if chapter["page_order_revision"] != base_revision:
                raise ContentConflict("page order revision changed")
            self._assert_chapter_writable(connection, chapter_id)
            existing = list(
                connection.execute(
                    select(pages.c.id)
                    .where(pages.c.chapter_id == chapter_id)
                    .order_by(pages.c.ordinal)
                ).scalars()
            )
            if len(ordered_ids) != len(set(ordered_ids)) or set(existing) != set(
                ordered_ids
            ):
                raise ValueError("ordered page ids must be an exact permutation")
            offset = len(existing) * 2 + 1
            connection.execute(
                update(pages)
                .where(pages.c.chapter_id == chapter_id)
                .values(ordinal=pages.c.ordinal + offset)
            )
            for ordinal, page_id in enumerate(ordered_ids, start=1):
                connection.execute(
                    update(pages)
                    .where(
                        pages.c.id == page_id,
                        pages.c.chapter_id == chapter_id,
                    )
                    .values(ordinal=ordinal)
                )
            connection.execute(
                update(chapters)
                .where(
                    chapters.c.id == chapter_id,
                    chapters.c.page_order_revision == base_revision,
                )
                .values(
                    page_order_revision=base_revision + 1,
                    updated_at=_utcnow(),
                )
            )
            mark_book_insight_derived_stale(
                connection,
                book_id=str(chapter["book_id"]),
                now=_utcnow(),
            )
        return base_revision + 1

    def delete_page(self, page_id: str) -> None:
        with immediate_transaction(self.engine) as connection:
            page = connection.execute(
                select(
                    pages.c.chapter_id,
                    pages.c.ordinal,
                    chapters.c.book_id,
                )
                .join(chapters, chapters.c.id == pages.c.chapter_id)
                .where(pages.c.id == page_id)
            ).mappings().one_or_none()
            if page is None:
                raise ContentNotFound("page not found")
            chapter_id = str(page["chapter_id"])
            self._assert_targets_idle(
                connection,
                str(page["book_id"]),
                [chapter_id],
            )
            mark_book_insight_derived_stale(
                connection,
                book_id=str(page["book_id"]),
                now=_utcnow(),
            )
            connection.execute(delete(pages).where(pages.c.id == page_id))
            following_ids = connection.execute(
                select(pages.c.id)
                .where(
                    pages.c.chapter_id == chapter_id,
                    pages.c.ordinal > page["ordinal"],
                )
                .order_by(pages.c.ordinal)
            ).scalars().all()
            for following_id in following_ids:
                connection.execute(
                    update(pages)
                    .where(pages.c.id == following_id)
                    .values(ordinal=pages.c.ordinal - 1)
                )
            connection.execute(
                update(chapters)
                .where(chapters.c.id == chapter_id)
                .values(
                    page_order_revision=chapters.c.page_order_revision + 1,
                    updated_at=_utcnow(),
                )
            )

    def clear_chapter_pages(self, chapter_id: str) -> int:
        with immediate_transaction(self.engine) as connection:
            chapter = connection.execute(
                select(chapters.c.book_id).where(chapters.c.id == chapter_id)
            ).mappings().one_or_none()
            if chapter is None:
                raise ContentNotFound("chapter not found")
            self._assert_targets_idle(
                connection,
                str(chapter["book_id"]),
                [chapter_id],
            )
            has_pages = connection.execute(
                select(exists().where(pages.c.chapter_id == chapter_id))
            ).scalar_one()
            if has_pages:
                mark_book_insight_derived_stale(
                    connection,
                    book_id=str(chapter["book_id"]),
                    now=_utcnow(),
                )
            removed = connection.execute(
                delete(pages).where(pages.c.chapter_id == chapter_id)
            )
            deleted_count = int(removed.rowcount or 0)
            if deleted_count:
                connection.execute(
                    update(chapters)
                    .where(chapters.c.id == chapter_id)
                    .values(
                        page_order_revision=chapters.c.page_order_revision + 1,
                        updated_at=_utcnow(),
                    )
                )
            return deleted_count

    def replace_page_source(
        self,
        *,
        page_id: str,
        base_source_revision: int,
        source: AssetRecord,
        thumbnail: AssetRecord,
        idempotency_scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[dict[str, object], bool]:
        now = _utcnow()
        with immediate_transaction(self.engine) as connection:
            replay = connection.execute(
                select(
                    idempotency_records.c.request_hash,
                    idempotency_records.c.response_json,
                    idempotency_records.c.expires_at,
                ).where(
                    idempotency_records.c.scope == idempotency_scope,
                    idempotency_records.c.key == idempotency_key,
                    idempotency_records.c.owner_user_id == effective_owner_id(),
                )
            ).mappings().one_or_none()
            if replay is not None:
                if replay["expires_at"] > now:
                    if replay["request_hash"] != request_hash:
                        raise IdempotencyConflict(
                            "Idempotency-Key was reused for different content"
                        )
                    return json.loads(replay["response_json"]), True
                connection.execute(
                    delete(idempotency_records).where(
                        idempotency_records.c.scope == idempotency_scope,
                        idempotency_records.c.key == idempotency_key,
                        idempotency_records.c.owner_user_id == effective_owner_id(),
                    )
                )
            page = connection.execute(
                select(
                    pages.c.chapter_id,
                    pages.c.source_revision,
                    pages.c.document_revision,
                    chapters.c.book_id,
                )
                .join(chapters, chapters.c.id == pages.c.chapter_id)
                .where(pages.c.id == page_id)
            ).mappings().one_or_none()
            if page is None:
                raise ContentNotFound("page not found")
            if page["source_revision"] != base_source_revision:
                raise ContentConflict("page source revision changed")
            self._assert_chapter_writable(
                connection,
                str(page["chapter_id"]),
            )
            if connection.execute(
                select(operations.c.id).where(
                    operations.c.page_id == page_id,
                    operations.c.status.in_(ACTIVE_OPERATION_STATUSES),
                )
            ).scalar_one_or_none() is not None:
                raise ContentConflict("page has an active operation")
            new_source_revision = base_source_revision + 1
            new_document_revision = int(page["document_revision"]) + 1
            connection.execute(
                delete(page_assets).where(
                    page_assets.c.page_id == page_id,
                    page_assets.c.role.not_in(("source", "thumbnail_source")),
                )
            )
            for role, record, parent in (
                ("source", source, None),
                ("thumbnail_source", thumbnail, source.id),
            ):
                connection.execute(
                    update(page_assets)
                    .where(
                        page_assets.c.page_id == page_id,
                        page_assets.c.role == role,
                    )
                    .values(
                        asset_id=record.id,
                        input_source_revision=new_source_revision,
                        input_document_revision=None,
                        parent_asset_id=parent,
                        producer_job_step_id=None,
                        producer_operation_id=None,
                        producer_render_request_id=None,
                    )
                )
            connection.execute(
                update(pages)
                .where(
                    pages.c.id == page_id,
                    pages.c.source_revision == base_source_revision,
                )
                .values(
                    source_revision=new_source_revision,
                    document_revision=new_document_revision,
                    rendered_revision=None,
                    render_status="not_rendered",
                    detection_state="unprocessed",
                    updated_at=_utcnow(),
                )
            )
            mark_book_insight_derived_stale(
                connection,
                book_id=str(page["book_id"]),
                now=now,
            )
            result = {
                "pageId": page_id,
                "sourceRevision": new_source_revision,
                "documentRevision": new_document_revision,
                "sourceUrl": f"/api/v2/assets/{source.id}",
                "thumbnailSourceUrl": f"/api/v2/assets/{thumbnail.id}",
            }
            connection.execute(
                insert(idempotency_records).values(
                    owner_user_id=effective_owner_id(),
                    scope=idempotency_scope,
                    key=idempotency_key,
                    request_hash=request_hash,
                    http_status=200,
                    response_json=_json(result),
                    resource_type="page",
                    resource_id=page_id,
                    expires_at=now.replace(microsecond=0) + timedelta(days=7),
                )
            )
        return result, False

    def append_page(
        self,
        *,
        chapter_id: str,
        requested_logical_path: str,
        text_style: dict[str, object],
        source: AssetRecord,
        thumbnail: AssetRecord,
        idempotency_scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[dict[str, object], bool]:
        logical_path = normalize_logical_path(requested_logical_path)
        now = _utcnow()
        with immediate_transaction(self.engine) as connection:
            replay = connection.execute(
                select(
                    idempotency_records.c.request_hash,
                    idempotency_records.c.http_status,
                    idempotency_records.c.response_json,
                    idempotency_records.c.expires_at,
                ).where(
                    idempotency_records.c.scope == idempotency_scope,
                    idempotency_records.c.key == idempotency_key,
                    idempotency_records.c.owner_user_id == effective_owner_id(),
                )
            ).mappings().one_or_none()
            if replay is not None:
                if replay["expires_at"] > now:
                    if replay["request_hash"] != request_hash:
                        raise IdempotencyConflict(
                            "Idempotency-Key was reused for different content"
                        )
                    return json.loads(replay["response_json"]), True
                connection.execute(
                    delete(idempotency_records).where(
                        idempotency_records.c.scope == idempotency_scope,
                        idempotency_records.c.key == idempotency_key,
                        idempotency_records.c.owner_user_id == effective_owner_id(),
                    )
                )

            chapter = connection.execute(
                select(
                    chapters.c.book_id,
                    chapters.c.page_order_revision,
                ).where(chapters.c.id == chapter_id)
            ).mappings().one_or_none()
            if chapter is None:
                raise ContentNotFound("chapter not found")
            chapter_revision = chapter["page_order_revision"]
            self._assert_chapter_writable(connection, chapter_id)
            existing_paths = set(
                connection.execute(
                    select(pages.c.logical_source_path).where(
                        pages.c.chapter_id == chapter_id
                    )
                ).scalars()
            )
            final_logical_path = deduplicate_logical_path(logical_path, existing_paths)
            ordinal = (
                connection.execute(
                    select(func.coalesce(func.max(pages.c.ordinal), 0)).where(
                        pages.c.chapter_id == chapter_id
                    )
                ).scalar_one()
                + 1
            )
            page_id = str(uuid.uuid4())
            default_font_id, style_defaults = validate_text_style_defaults(
                connection,
                text_style,
            )
            connection.execute(
                insert(pages).values(
                    id=page_id,
                    chapter_id=chapter_id,
                    ordinal=ordinal,
                    logical_source_path=final_logical_path,
                    default_font_id=default_font_id,
                    page_style_defaults_json=_json(style_defaults),
                )
            )
            connection.execute(
                insert(page_assets),
                [
                    {
                        "page_id": page_id,
                        "role": "source",
                        "asset_id": source.id,
                        "input_source_revision": 1,
                        "parent_asset_id": None,
                    },
                    {
                        "page_id": page_id,
                        "role": "thumbnail_source",
                        "asset_id": thumbnail.id,
                        "input_source_revision": 1,
                        "parent_asset_id": source.id,
                    },
                ],
            )
            connection.execute(
                update(chapters)
                .where(
                    chapters.c.id == chapter_id,
                    chapters.c.page_order_revision == chapter_revision,
                )
                .values(
                    page_order_revision=chapter_revision + 1,
                    updated_at=now,
                )
            )
            mark_book_insight_derived_stale(
                connection,
                book_id=str(chapter["book_id"]),
                now=now,
            )
            response = {
                "page": {
                    "id": page_id,
                    "chapterId": chapter_id,
                    "ordinal": ordinal,
                    "logicalSourcePath": final_logical_path,
                    "sourceRevision": 1,
                    "documentRevision": 1,
                    "width": source.width,
                    "height": source.height,
                    "sourceUrl": f"/api/v2/assets/{source.id}",
                    "thumbnailSourceUrl": f"/api/v2/assets/{thumbnail.id}",
                    "translatedUrl": None,
                },
                "pageOrderRevision": chapter_revision + 1,
            }
            connection.execute(
                insert(idempotency_records).values(
                    owner_user_id=effective_owner_id(),
                    scope=idempotency_scope,
                    key=idempotency_key,
                    request_hash=request_hash,
                    http_status=201,
                    response_json=_json(response),
                    resource_type="page",
                    resource_id=page_id,
                    expires_at=now.replace(microsecond=0) + timedelta(days=7),
                )
            )
        return response, False

    def replay_idempotency(
        self,
        *,
        scope: str,
        key: str,
        request_hash: str,
    ) -> dict[str, object] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    idempotency_records.c.request_hash,
                    idempotency_records.c.response_json,
                ).where(
                    idempotency_records.c.scope == scope,
                    idempotency_records.c.key == key,
                    idempotency_records.c.owner_user_id == effective_owner_id(),
                    idempotency_records.c.expires_at > _utcnow(),
                )
            ).mappings().one_or_none()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyConflict("Idempotency-Key was reused for different content")
        return json.loads(row["response_json"])

    @staticmethod
    def _assert_chapter_writable(connection: object, chapter_id: str) -> None:
        lock = connection.execute(  # type: ignore[attr-defined]
            select(chapter_write_locks.c.chapter_id).where(
                chapter_write_locks.c.chapter_id == chapter_id
            )
        ).scalar_one_or_none()
        if lock is not None:
            raise ContentLocked("chapter is reserved by backend work")

    def list_pages(
        self,
        *,
        chapter_id: str,
        after_ordinal: int = 0,
        limit: int = 50,
        all_pages: bool = False,
    ) -> dict[str, object]:
        if limit < 1 or limit > 200:
            raise ValueError("page limit must be between 1 and 200")
        statement = self._page_summary_statement().where(
            pages.c.chapter_id == chapter_id,
            pages.c.ordinal > after_ordinal,
        )
        statement = statement.order_by(pages.c.ordinal)
        if not all_pages:
            statement = statement.limit(limit + 1)
        with self.engine.connect() as connection:
            chapter_revision = connection.execute(
                select(chapters.c.page_order_revision).where(
                    chapters.c.id == chapter_id
                )
            ).scalar_one_or_none()
            if chapter_revision is None:
                raise ContentNotFound("chapter not found")
            rows = list(connection.execute(statement).mappings())
        has_more = not all_pages and len(rows) > limit
        visible = rows[:limit] if has_more else rows
        return {
            "items": [self._page_summary(row) for row in visible],
            "nextCursor": visible[-1]["ordinal"] if has_more and visible else None,
            "pageOrderRevision": chapter_revision,
        }

    def get_page_summary(self, page_id: str) -> dict[str, object]:
        with self.engine.connect() as connection:
            row = connection.execute(
                self._page_summary_statement().where(pages.c.id == page_id)
            ).mappings().one_or_none()
        if row is None:
            raise ContentNotFound("page not found")
        return self._page_summary(row)

    @staticmethod
    def _page_summary_statement():
        asset_aliases = {
            role: page_assets.alias(name=f"pa_{role}")
            for role in (
                "source",
                "thumbnail_source",
                "clean",
                "translated",
            )
        }
        statement = select(
            pages,
            *[
                alias.c.asset_id.label(f"{role}_asset_id")
                for role, alias in asset_aliases.items()
            ],
            assets.c.width.label("source_width"),
            assets.c.height.label("source_height"),
        )
        for role, alias in asset_aliases.items():
            statement = statement.outerjoin(
                alias,
                and_(alias.c.page_id == pages.c.id, alias.c.role == role),
            )
        return statement.outerjoin(
            assets,
            assets.c.id == asset_aliases["source"].c.asset_id,
        )

    @staticmethod
    def _page_summary(row: dict[str, Any]) -> dict[str, object]:
        def url(role: str) -> str | None:
            asset_id = row.get(f"{role}_asset_id")
            return f"/api/v2/assets/{asset_id}" if asset_id else None

        return {
            "id": row["id"],
            "chapterId": row["chapter_id"],
            "ordinal": row["ordinal"],
            "logicalSourcePath": row["logical_source_path"],
            "width": row.get("source_width"),
            "height": row.get("source_height"),
            "sourceRevision": row["source_revision"],
            "documentRevision": row["document_revision"],
            "renderedRevision": row["rendered_revision"],
            "renderStatus": row["render_status"],
            "detectionState": row["detection_state"],
            "sourceUrl": url("source"),
            "thumbnailSourceUrl": url("thumbnail_source"),
            "cleanUrl": url("clean"),
            "translatedUrl": url("translated"),
        }

    @staticmethod
    def _assert_targets_idle(
        connection: object,
        book_id: str,
        chapter_ids: list[str],
    ) -> None:
        session_document = studio_documents.alias(
            "content_guard_session_document"
        )
        active_jobs = list(
            connection.execute(  # type: ignore[attr-defined]
                select(jobs.c.id, jobs.c.status).where(
                    jobs.c.status.in_(NONTERMINAL_JOB_STATUSES),
                    or_(
                        jobs.c.book_id == book_id,
                        jobs.c.chapter_id.in_(chapter_ids) if chapter_ids else False,
                    ),
                )
            ).mappings()
        )
        active_operation_ids = [
            str(value)
            for value in connection.execute(  # type: ignore[attr-defined]
                select(operations.c.id)
                .join(pages, pages.c.id == operations.c.page_id, isouter=True)
                .join(
                    studio_documents,
                    studio_documents.c.id == operations.c.studio_document_id,
                    isouter=True,
                )
                .join(
                    studio_chat_sessions,
                    studio_chat_sessions.c.id == operations.c.studio_session_id,
                    isouter=True,
                )
                .join(
                    session_document,
                    session_document.c.id == studio_chat_sessions.c.document_id,
                    isouter=True,
                )
                .where(
                    operations.c.status.in_(ACTIVE_OPERATION_STATUSES),
                    or_(
                        (
                            pages.c.chapter_id.in_(chapter_ids)
                            if chapter_ids
                            else False
                        ),
                        studio_documents.c.book_id == book_id,
                        session_document.c.book_id == book_id,
                    ),
                )
            ).scalars()
        ]
        if active_jobs or active_operation_ids:
            raise ContentLocked(
                "content is still referenced by active work",
                details={
                    "jobs": [
                        {
                            "jobId": str(row["id"]),
                            "status": str(row["status"]),
                        }
                        for row in active_jobs
                    ],
                    "operationIds": active_operation_ids,
                },
            )

    @staticmethod
    def _replace_book_tags(
        connection: object,
        book_id: str,
        tag_ids: list[str],
    ) -> None:
        if len(tag_ids) != len(set(tag_ids)):
            raise ValueError("tagIds must contain unique IDs")
        if tag_ids:
            found = set(
                connection.execute(  # type: ignore[attr-defined]
                    select(tags.c.id).where(
                        tags.c.id.in_(tag_ids),
                        tags.c.owner_user_id == effective_owner_id(),
                    )
                ).scalars()
            )
            if found != set(tag_ids):
                raise ContentNotFound("tag not found")
        connection.execute(  # type: ignore[attr-defined]
            delete(book_tags).where(book_tags.c.book_id == book_id)
        )
        if tag_ids:
            connection.execute(  # type: ignore[attr-defined]
                insert(book_tags),
                [
                    {"book_id": book_id, "tag_id": tag_id}
                    for tag_id in tag_ids
                ],
            )

    @staticmethod
    def _normalize_color(value: str) -> str:
        normalized = value.strip()
        if (
            len(normalized) != 7
            or not normalized.startswith("#")
            or any(character not in "0123456789abcdefABCDEF" for character in normalized[1:])
        ):
            raise ValueError("tag color must be a six-digit hexadecimal color")
        return normalized.lower()
