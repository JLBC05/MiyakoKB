"""题库结构守卫：确保 tests_backend/v2/eval/qa_cases.json 可被 scripts/qa_eval.py 直接使用。"""

from __future__ import annotations

import json
from pathlib import Path

CASES = Path(__file__).resolve().parent / "eval" / "qa_cases.json"


def _load() -> dict:
    return json.loads(CASES.read_text(encoding="utf-8"))


def test_case_bank_has_books_and_questions() -> None:
    data = _load()
    assert set(data) == {"books", "questions"}
    books = data["books"]
    assert isinstance(books, dict) and books
    for name, book_id in books.items():
        assert isinstance(name, str) and name
        assert isinstance(book_id, str) and len(book_id) == 36
    questions = data["questions"]
    assert isinstance(questions, list) and len(questions) >= 40


def test_every_question_is_well_formed() -> None:
    data = _load()
    books = data["books"]
    seen: set[int] = set()
    for item in data["questions"]:
        assert set(item) >= {"id", "book", "question", "expected"}, item
        identifier = item["id"]
        assert isinstance(identifier, int) and identifier > 0
        assert identifier not in seen, f"题号重复: {identifier}"
        seen.add(identifier)
        assert item["book"] in books, f"未登记的书籍键: {item['book']}"
        assert isinstance(item["question"], str) and item["question"].strip()
        expected = item["expected"]
        assert isinstance(expected, dict) and expected, item
        # 判分器认识的键（见 scripts/qa_eval.py 的 grade()）。
        allowed = {"chapter", "page", "keyword", "entities", "any_of", "forbid"}
        assert set(expected) <= allowed, item
        for key in ("chapter", "page"):
            # None 表示该维度不做判定（例如「第几话」明确但页码资料不足）。
            if key in expected and expected[key] is not None:
                assert isinstance(expected[key], int), item
        for key in ("entities", "any_of", "forbid"):
            if key in expected:
                values = expected[key]
                assert isinstance(values, list) and values, item
                assert all(isinstance(value, str) and value for value in values), item
        if "keyword" in expected:
            assert isinstance(expected["keyword"], str) and expected["keyword"], item


def test_question_ids_are_contiguous_from_one() -> None:
    data = _load()
    ids = sorted(item["id"] for item in data["questions"])
    assert ids == list(range(1, len(ids) + 1))
