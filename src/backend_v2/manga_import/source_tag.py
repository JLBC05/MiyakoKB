"""导入溯源标签：把「这本书来自哪里」编进书标题，重试时据此续传。

为什么编码在标题里而不是新增列：``storage_migrator`` 是打签发的存储契约，
加列意味着新的 storageVersion 与一套迁移脚本；而导入幂等只是一次止血，不值得
为它引入迁移风险。标题本身就是自由文本（``VARCHAR(500)``，无格式校验），
把标记放在末尾既不影响展示（前端负责剥掉），又能随 ``books.title`` 一起持久化。

标记格式（``<kind>`` 取 ``venera`` / ``local``）::

    <书名>\\ue000⟦miyako:venera:zhoushuhuizhan:3f2a1c⟧\\ue001

``digest`` 是标识的短哈希，用来吸收超长标题被截断的情况：只要前缀还在、
digest 还对得上，就认为是同一本书。
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional

from sqlalchemy import select, update

from src.backend_v2.storage.schema import books

_MARK_OPEN = "\ue000⟦miyako:"
_MARK_CLOSE = "⟧\ue001"
_MARK_PATTERN = re.compile(r"\ue000⟦miyako:(?P<kind>[a-z_]+):(?P<identifier>.*?):(?P<digest>[0-9a-f]{8})⟧\ue001")
_DIGEST_LENGTH = 8
# books.title 是 VARCHAR(500)；给标记与分隔符留 120 字符余量。
_TITLE_LIMIT = 500 - 120


class ImportSourceTag:
    """一条可往返的溯源标记。"""

    __slots__ = ("kind", "identifier", "digest", "raw")

    def __init__(self, *, kind: str, identifier: str, digest: str, raw: str) -> None:
        self.kind = kind
        self.identifier = identifier
        self.digest = digest
        self.raw = raw

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"ImportSourceTag(kind={self.kind!r}, identifier={self.identifier!r})"


def digest_for(kind: str, identifier: str) -> str:
    return hashlib.sha256(f"{kind}\x00{identifier}".encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]


def build_title(title: str, *, kind: str, identifier: str) -> str:
    """生成带溯源标记的库内标题。"""
    clean = (title or "").strip() or "未命名漫画"
    if len(clean) > _TITLE_LIMIT:
        clean = clean[:_TITLE_LIMIT].rstrip()
    return f"{clean}{_MARK_OPEN}{kind}:{identifier}:{digest_for(kind, identifier)}{_MARK_CLOSE}"


def parse_tag(title: str) -> Optional[ImportSourceTag]:
    """从标题里取出标记；没有标记返回 ``None``。"""
    match = _MARK_PATTERN.search(title or "")
    if match is None:
        return None
    return ImportSourceTag(
        kind=match.group("kind"),
        identifier=match.group("identifier"),
        digest=match.group("digest"),
        raw=match.group(0),
    )


def display_title(title: str) -> str:
    """给用户看的标题：去掉标记。"""
    return _MARK_PATTERN.sub("", title or "").strip()


def find_imported_book(
    engine: object,
    *,
    owner_id: str,
    kind: str,
    identifier: str,
    digest: str,
) -> Optional[str]:
    """按溯源标记查已有书 id；命中返回 book_id。"""
    if not identifier:
        return None
    prefix = f"{_MARK_OPEN}{kind}:{identifier}:"
    with engine.connect() as conn:
        rows = conn.execute(
            select(books.c.id, books.c.title).where(
                books.c.owner_user_id == owner_id,
                books.c.title.like(f"%{prefix}%"),
            )
        ).all()
    for row in rows:
        tag = parse_tag(row.title or "")
        if tag is None:
            continue
        if tag.kind != kind or tag.identifier != identifier:
            continue
        # digest 校验吸收「标题被截断但仍残留前缀」的极端情况。
        if tag.digest == digest and digest_for(kind, identifier) == digest:
            return row.id
    return None


def adopt_legacy_book(
    engine: object,
    *,
    owner_id: str,
    kind: str,
    identifier: str,
    legacy_title: str,
) -> Optional[str]:
    """认领 v0.1.0 及更早导入的「无标记同源书」。

    这些书的标题就是裸书名，重试时按标记找不到，旧实现会再造一本重复书。这里
    按「同一 owner + 标题等于裸书名 + 还没有标记」命中最多一本，原库位改写成带
    标记的标题，之后的重试就能走续传。

    ``books`` 上有 ``uq_books_owner_title``，若已有另一本同名书占位就放弃认领
    （返回 ``None``，调用方按新书处理），绝不因为抢名字而让导入失败。
    """
    clean = (legacy_title or "").strip()
    if not clean or not identifier:
        return None
    with engine.connect() as conn:
        rows = conn.execute(
            select(books.c.id, books.c.title).where(
                books.c.owner_user_id == owner_id,
                books.c.title == clean,
            )
        ).all()
    if len(rows) != 1:
        return None
    candidate = rows[0]
    if parse_tag(candidate.title or "") is not None:
        return None
    try:
        with engine.begin() as conn:
            conn.execute(
                update(books)
                .where(books.c.id == candidate.id)
                .values(title=build_title(clean, kind=kind, identifier=identifier))
            )
    except Exception:  # noqa: BLE001 - 唯一约束冲突时放弃认领
        return None
    return candidate.id


__all__ = [
    "ImportSourceTag",
    "adopt_legacy_book",
    "build_title",
    "digest_for",
    "display_title",
    "find_imported_book",
    "parse_tag",
]
