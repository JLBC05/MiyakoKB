"""Test script for entity resolver with Manga Insight artifacts.

Conforms to AGENTS.md database test script rules:
- Imports actual Table objects from src.backend_v2.storage.schema.
- Never handwrites table or column name strings in SQL expressions.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

# Ensure backend package can be imported
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
WORKSPACE_ROOT = PROJECT_ROOT.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from sqlalchemy import create_engine, select
from src.backend_v2.insight.entity_resolver import resolve_entities_in_question
from src.backend_v2.storage.schema import analysis_artifacts, books


def find_kurumi_database() -> tuple[str, str]:
    """Find database and book_id containing entity alignment for FX战士久留美."""
    candidate_db_paths = [
        PROJECT_ROOT / "test_kurumi_ch1_to_ch5.db",
        WORKSPACE_ROOT / "test_kurumi_ch1_to_ch5.db",
        PROJECT_ROOT / "data-v2" / "saber.sqlite3",
        WORKSPACE_ROOT / "data-v2" / "saber.sqlite3",
        PROJECT_ROOT / "test_chapters_1_to_5_automerge.db",
        WORKSPACE_ROOT / "test_chapters_1_to_5_automerge.db",
    ]

    for db_path in candidate_db_paths:
        if not db_path.exists():
            continue
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            # Safely query via real schema objects without hardcoded table/column strings
            stmt = (
                select(books.c.id, books.c.title)
                .join(analysis_artifacts, analysis_artifacts.c.book_id == books.c.id)
                .where(analysis_artifacts.c.kind == "entity_alignment")
            )
            rows = conn.execute(stmt).fetchall()
            for row in rows:
                book_id = row[0]
                book_title = row[1] or ""
                if "久留美" in book_title or "FX" in book_title:
                    return str(db_path), str(book_id)

    # Fallback to any book with entity_alignment artifact
    for db_path in candidate_db_paths:
        if not db_path.exists():
            continue
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            stmt = (
                select(analysis_artifacts.c.book_id)
                .where(analysis_artifacts.c.kind == "entity_alignment")
            )
            row = conn.execute(stmt).first()
            if row:
                return str(db_path), str(row[0])

    raise RuntimeError("No database with entity_alignment artifact found in candidate paths.")


def run_tests() -> None:
    # Ensure stdout handles unicode
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    db_path, book_id = find_kurumi_database()
    print(f"Using database: {db_path}")
    print(f"Target book_id: {book_id}")
    print("=" * 60)

    engine = create_engine(f"sqlite:///{db_path}")

    # Question 1
    q1 = "福贺久留美在第几页出现最多？"
    res1 = resolve_entities_in_question(book_id=book_id, question=q1, engine=engine)
    print(f"问题 1: \"{q1}\"")
    print(json.dumps(res1, indent=2, ensure_ascii=False))
    print("-" * 60)

    # Question 2
    q2 = "福贺久留美和久留美母亲是什么关系？"
    res2 = resolve_entities_in_question(book_id=book_id, question=q2, engine=engine)
    print(f"问题 2: \"{q2}\"")
    print(json.dumps(res2, indent=2, ensure_ascii=False))
    print("-" * 60)

    # Question 3
    q3 = "第 2 话发生了什么？"
    res3 = resolve_entities_in_question(book_id=book_id, question=q3, engine=engine)
    print(f"问题 3: \"{q3}\"")
    print(json.dumps(res3, indent=2, ensure_ascii=False))
    print("=" * 60)

    # Core Assertions
    print("Verifying core assertions...")

    # Assertion 1: 问题 1 命中 福贺久留美，is_structured_query: True
    matched_names_1 = [e["canonical_name"] for e in res1["matched_entities"]]
    assert "福贺久留美" in matched_names_1, f"Assertion failed: Q1 should match 福贺久留美, got {matched_names_1}"
    assert res1["is_structured_query"] is True, f"Assertion failed: Q1 is_structured_query should be True, got {res1['is_structured_query']}"
    print("[PASS] 问题 1: 命中 福贺久留美，is_structured_query: True")

    # Assertion 2: 问题 2 命中 福贺久留美 与 久留美母亲 两个独立实体，没有把“久留美母亲”误分解或吞并成“久留美”，is_structured_query: False
    matched_names_2 = [e["canonical_name"] for e in res2["matched_entities"]]
    assert matched_names_2 == ["福贺久留美", "久留美母亲"], (
        f"Assertion failed: Q2 matched entities should be ['福贺久留美', '久留美母亲'], got {matched_names_2}"
    )
    assert res2["is_structured_query"] is False, f"Assertion failed: Q2 is_structured_query should be False, got {res2['is_structured_query']}"
    print("[PASS] 问题 2: 命中 福贺久留美 与 久留美母亲 两个独立实体，未发生误分解或吞并，is_structured_query: False")

    # Assertion 3: 问题 3 matched_entities 为空，is_structured_query: False
    assert len(res3["matched_entities"]) == 0, f"Assertion failed: Q3 matched_entities should be empty, got {res3['matched_entities']}"
    assert res3["is_structured_query"] is False, f"Assertion failed: Q3 is_structured_query should be False, got {res3['is_structured_query']}"
    print("[PASS] 问题 3: matched_entities 为空，is_structured_query: False")

    print("\nAll assertions passed successfully!")


if __name__ == "__main__":
    run_tests()
