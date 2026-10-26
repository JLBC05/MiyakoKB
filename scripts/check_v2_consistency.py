"""Report v2 database/file/vector consistency as JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.backend_v2.paths import resolve_data_root
from src.backend_v2.storage.consistency import (
    DEFAULT_CONTENT_SAMPLE,
    ConsistencyChecker,
)
from src.backend_v2.storage.database import (
    create_sqlite_engine,
    database_path_for,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check Saber Translator v2 storage consistency",
    )
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument(
        "--skip-vectors",
        action="store_true",
        help="skip Chroma collection inspection",
    )
    parser.add_argument(
        "--content-sample",
        type=int,
        default=DEFAULT_CONTENT_SAMPLE,
        metavar="N",
        help=(
            "hash at most N assets to verify their bytes "
            f"(default {DEFAULT_CONTENT_SAMPLE}; 0 disables, -1 verifies all)"
        ),
    )
    args = parser.parse_args()
    content_sample: int | None = args.content_sample
    if content_sample is not None and content_sample < 0:
        content_sample = None
    data_root = resolve_data_root(args.data_dir)
    database_path = database_path_for(data_root)
    if not database_path.is_file():
        parser.error(f"v2 database does not exist: {database_path}")
    engine = create_sqlite_engine(database_path)
    try:
        report = ConsistencyChecker(
            data_root=data_root,
            engine=engine,
        ).check(
            include_vectors=not args.skip_vectors,
            verify_content=content_sample != 0,
            content_sample=content_sample,
        )
    finally:
        engine.dispose()
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
