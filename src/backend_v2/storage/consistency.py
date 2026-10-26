"""Read-only consistency report for v2 structured and file storage."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import Engine, select

from src.backend_v2.insight.derived import InsightVectorStore
from src.backend_v2.storage.schema import assets, object_commit_journal

# Content verification reads every sampled file in full, so the default sample is
# deliberately small: a full sweep over a large library is I/O bound and is only
# appropriate as an explicit maintenance action.
DEFAULT_CONTENT_SAMPLE = 32
_CONTENT_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ConsistencyReport:
    foreign_key_violations: tuple[tuple[object, ...], ...]
    invalid_asset_paths: tuple[str, ...]
    missing_asset_files: tuple[str, ...]
    integrity_status_mismatches: tuple[str, ...]
    # 内容类缺陷单独分组：存在性检查早已覆盖「文件在不在」，而字节被截断、
    # 被替换或磁盘损坏只有读到内容才能发现。
    content_checksum_mismatches: tuple[str, ...]
    content_size_mismatches: tuple[str, ...]
    orphan_object_files: tuple[str, ...]
    missing_vector_collections: tuple[str, ...]
    orphan_vector_collections: tuple[str, ...]
    vector_check_error: str | None
    content_checked: int = 0
    content_sample: int | None = None

    @property
    def ok(self) -> bool:
        return not any(
            (
                self.foreign_key_violations,
                self.invalid_asset_paths,
                self.missing_asset_files,
                self.integrity_status_mismatches,
                self.content_checksum_mismatches,
                self.content_size_mismatches,
                self.orphan_object_files,
                self.missing_vector_collections,
                self.orphan_vector_collections,
                self.vector_check_error,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, **asdict(self)}


class ConsistencyChecker:
    def __init__(
        self,
        *,
        data_root: Path,
        engine: Engine,
    ) -> None:
        self.data_root = data_root.resolve()
        self.objects_root = self.data_root / "objects"
        self.engine = engine

    def check(
        self,
        *,
        include_vectors: bool = True,
        verify_content: bool = True,
        content_sample: int | None = DEFAULT_CONTENT_SAMPLE,
    ) -> ConsistencyReport:
        """Build the report.

        ``content_sample`` limits how many assets get their bytes hashed: ``None``
        verifies every asset (slow on large libraries), ``0`` skips content
        verification entirely while still reporting ``byte_size`` drift.
        """
        if content_sample is not None and content_sample < 0:
            raise ValueError("content_sample must be >= 0 or None")
        invalid_paths: list[str] = []
        missing_files: list[str] = []
        status_mismatches: list[str] = []
        size_mismatches: list[str] = []
        checksum_mismatches: list[str] = []
        present_assets: list[tuple[str, Path, int, str]] = []
        with self.engine.connect() as connection:
            foreign_key_violations = tuple(
                tuple(row)
                for row in connection.exec_driver_sql(
                    "PRAGMA foreign_key_check"
                ).all()
            )
            asset_rows = list(
                connection.execute(
                    select(
                        assets.c.id,
                        assets.c.relative_path,
                        assets.c.integrity_status,
                        assets.c.checksum,
                        assets.c.byte_size,
                    )
                ).mappings()
            )
            journal_paths = {
                str(value)
                for value in connection.execute(
                    select(object_commit_journal.c.final_relative_path)
                ).scalars()
            }

        recorded_paths: set[str] = set()
        for row in asset_rows:
            asset_id = str(row["id"])
            relative_path = str(row["relative_path"])
            recorded_paths.add(relative_path)
            resolved = self._resolve(relative_path)
            if resolved is None:
                invalid_paths.append(asset_id)
                continue
            exists = resolved.is_file()
            expected_status = "ok" if exists else "missing"
            if not exists:
                missing_files.append(asset_id)
            elif str(row["integrity_status"]) == "ok":
                # 大小对不上说明文件被截断或替换过；这一步只是 stat，成本可忽略，
                # 因此总是执行，不受抽样限制。
                actual_size = resolved.stat().st_size
                if actual_size != int(row["byte_size"]):
                    size_mismatches.append(asset_id)
                    continue
                present_assets.append(
                    (
                        asset_id,
                        resolved,
                        int(row["byte_size"]),
                        str(row["checksum"]),
                    )
                )
            if str(row["integrity_status"]) != expected_status:
                status_mismatches.append(asset_id)

        content_checked = 0
        if verify_content and present_assets:
            matched = present_assets
            if content_sample is not None:
                matched = present_assets[:content_sample]
            for asset_id, resolved, byte_size, checksum in matched:
                content_checked += 1
                if self._file_matches(resolved, byte_size, checksum):
                    continue
                checksum_mismatches.append(asset_id)

        orphan_files: list[str] = []
        if self.objects_root.is_dir():
            for path in self.objects_root.rglob("*"):
                if not path.is_file():
                    continue
                relative_path = path.relative_to(self.data_root).as_posix()
                if (
                    relative_path not in recorded_paths
                    and relative_path not in journal_paths
                ):
                    orphan_files.append(relative_path)

        missing_vectors: tuple[str, ...] = ()
        orphan_vectors: tuple[str, ...] = ()
        vector_error: str | None = None
        if include_vectors:
            try:
                inspection = InsightVectorStore(
                    self.data_root
                ).inspect_collections(self.engine)
            except Exception as exc:
                vector_error = f"{type(exc).__name__}: {exc}"
            else:
                missing_vectors = inspection.missing
                orphan_vectors = inspection.orphaned

        return ConsistencyReport(
            foreign_key_violations=foreign_key_violations,
            invalid_asset_paths=tuple(sorted(invalid_paths)),
            missing_asset_files=tuple(sorted(missing_files)),
            integrity_status_mismatches=tuple(sorted(status_mismatches)),
            content_checksum_mismatches=tuple(sorted(checksum_mismatches)),
            content_size_mismatches=tuple(sorted(size_mismatches)),
            orphan_object_files=tuple(sorted(orphan_files)),
            missing_vector_collections=missing_vectors,
            orphan_vector_collections=orphan_vectors,
            vector_check_error=vector_error,
            content_checked=content_checked,
            content_sample=content_sample,
        )

    def _file_matches(self, path: Path, byte_size: int, checksum: str) -> bool:
        digest = hashlib.sha256()
        total = 0
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(_CONTENT_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    digest.update(chunk)
        except OSError:
            # 读不出来就是内容不可用，与缺失等价。
            return False
        return total == byte_size and digest.hexdigest() == checksum

    def _resolve(self, relative_path: str) -> Path | None:
        pure = PurePosixPath(relative_path)
        if pure.is_absolute() or not pure.parts or ".." in pure.parts:
            return None
        resolved = (self.data_root / Path(*pure.parts)).resolve()
        try:
            resolved.relative_to(self.data_root)
        except ValueError:
            return None
        return resolved
