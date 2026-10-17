from __future__ import annotations

import json
from pathlib import Path
import sys
import threading

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import create_engine, select
from src.backend_v2.storage.schema import chapters, pages, analysis_page_results
from src.backend_v2.insight.qa import (
    DefaultQAApiAlgorithms,
    InsightQACommandService,
)
from src.backend_v2.insight.entity_resolver import resolve_entities_in_question

PROJECT_ROOT = Path(__file__).resolve().parent
db_path = PROJECT_ROOT / "data-v2" / "saber.sqlite3"
engine = create_engine(f"sqlite:///{db_path}")

book_id = "92daa1f4-ce28-4cfe-945c-1deb95cf5939"
cmd_service = InsightQACommandService(engine)
config = cmd_service.settings.resolve_insight(book_id=book_id, scope="qa")
materialized_config = cmd_service.materialize_api_config(config, include_reranker=False)
qa_api = DefaultQAApiAlgorithms()


def test_question_1():
    print("=" * 60)
    print("问题 1：“斯科特在第几页出现最多？”")
    print("=" * 60)
    q1 = "斯科特在第几页出现最多？"
    res1 = resolve_entities_in_question(book_id=book_id, question=q1, engine=engine)
    print("【实体解析结果】:")
    print(json.dumps(res1, ensure_ascii=False, indent=2))

    cancelled = threading.Event()
    answer1_chunks = []
    for chunk in qa_api.stream_answer(
        question=q1,
        candidates=[],
        config=materialized_config,
        cancelled=cancelled,
        resolved_entities=res1,
    ):
        answer1_chunks.append(chunk)

    full_answer_1 = "".join(answer1_chunks)
    print("\n【最终问答回答】:")
    print(full_answer_1)
    return res1, full_answer_1


def test_question_2():
    print("\n" + "=" * 60)
    print("问题 2：“第 3 话发生了什么？”")
    print("=" * 60)
    q2 = "第 3 话发生了什么？"
    res2 = resolve_entities_in_question(book_id=book_id, question=q2, engine=engine)
    print("【实体解析结果】:")
    print(json.dumps(res2, ensure_ascii=False, indent=2))

    # Retrieve Chapter 3 pages from DB
    with engine.connect() as conn:
        stmt = (
            select(
                pages.c.id,
                pages.c.ordinal,
                analysis_page_results.c.page_number_snapshot,
                analysis_page_results.c.payload_json,
            )
            .join(chapters, chapters.c.id == pages.c.chapter_id)
            .join(analysis_page_results, analysis_page_results.c.page_id == pages.c.id)
            .where(
                chapters.c.book_id == book_id,
                chapters.c.ordinal == 3,
            )
            .order_by(pages.c.ordinal)
        )
        ch3_pages = conn.execute(stmt).fetchall()

    candidates2 = []
    for p in ch3_pages:
        payload = json.loads(p[3])
        page_summary = payload.get("page_summary", "")
        key_events = payload.get("key_events", [])
        event_texts = [e.get("summary", "") for e in key_events if isinstance(e, dict) and e.get("summary")]
        doc = f"第 {p[2]} 页剧情：{page_summary}"
        if event_texts:
            doc += f" (关键事件: {'；'.join(event_texts)})"
        candidates2.append({
            "pageId": p[0],
            "pageNumber": p[2],
            "document": doc,
            "score": 1.0,
        })

    print(f"\n【漫画剧情检索结果】: 检索到第 3 话共 {len(candidates2)} 页剧情资料 (页码 {candidates2[0]['pageNumber']} ~ {candidates2[-1]['pageNumber']})")

    cancelled = threading.Event()
    answer2_chunks = []
    for chunk in qa_api.stream_answer(
        question=q2,
        candidates=candidates2,
        config=materialized_config,
        cancelled=cancelled,
        resolved_entities=res2,
    ):
        answer2_chunks.append(chunk)

    full_answer_2 = "".join(answer2_chunks)
    print("\n【最终问答回答】:")
    print(full_answer_2)
    return res2, full_answer_2


if __name__ == "__main__":
    test_question_1()
    test_question_2()
