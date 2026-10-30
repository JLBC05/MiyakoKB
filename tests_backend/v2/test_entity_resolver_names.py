"""实体解析：同名跨类目时不能被冷门实体抢占（离线、不联网）。"""

from __future__ import annotations

import json

from sqlalchemy import create_engine, insert

from src.backend_v2.insight.entity_resolver import resolve_entities_in_question
from src.backend_v2.storage.schema import analysis_artifacts


BOOK_ID = "book-same-name"


def _engine_with(entities: list[dict], alias_map: dict[str, str] | None = None):
    engine = create_engine("sqlite://")
    analysis_artifacts.create(engine)
    payload = {"canonical_entities": entities, "alias_map": alias_map or {}}
    with engine.begin() as conn:
        conn.execute(
            insert(analysis_artifacts).values(
                id="artifact-1",
                book_id=BOOK_ID,
                kind="entity_alignment",
                template="default",
                status="ready",
                revision=1,
                is_active=True,
                dependency_fingerprint="0" * 64,
                payload_json=json.dumps(payload, ensure_ascii=False),
            )
        )
    return engine


def _resolved_ids(engine, question: str) -> list[str]:
    result = resolve_entities_in_question(BOOK_ID, question, engine)
    return [item["entity_id"] for item in result["matched_entities"]]


def test_same_name_prefers_the_character_over_a_stray_concept():
    # 概念排在后面：旧实现会被后写的 concept_jiu_liu_mei 覆盖。
    engine = _engine_with(
        [
            {
                "entity_id": "character_jiu_liu_mei",
                "canonical_name": "久留美",
                "category": "characters",
                "frequency": 1021,
                "pages": list(range(1, 1022)),
                "aliases": ["福贺久留美"],
            },
            {
                "entity_id": "concept_jiu_liu_mei",
                "canonical_name": "久留美",
                "category": "concepts",
                "frequency": 1,
                "pages": [1117],
                "aliases": [],
            },
        ]
    )
    assert _resolved_ids(engine, "久留美第一次出场是在第几话？") == ["character_jiu_liu_mei"]


def test_shared_alias_goes_to_the_more_frequent_entity():
    engine = _engine_with(
        [
            {
                "entity_id": "concept_ya_chui",
                "canonical_name": "芽吹",
                "category": "concepts",
                "frequency": 3,
                "pages": [3, 5, 7],
                "aliases": ["山师"],
            },
            {
                "entity_id": "character_ya_chui",
                "canonical_name": "芽吹",
                "category": "characters",
                "frequency": 492,
                "pages": list(range(1, 493)),
                "aliases": ["山师"],
            },
        ]
    )
    assert _resolved_ids(engine, "山师芽吹做了什么？") == ["character_ya_chui"]


def test_same_frequency_prefers_the_concrete_category():
    engine = _engine_with(
        [
            {
                "entity_id": "concept_token",
                "canonical_name": "影",
                "category": "concepts",
                "frequency": 5,
                "pages": [1, 2, 3, 4, 5],
                "aliases": [],
            },
            {
                "entity_id": "character_token",
                "canonical_name": "影",
                "category": "characters",
                "frequency": 5,
                "pages": [1, 2, 3, 4, 5],
                "aliases": [],
            },
        ]
    )
    assert _resolved_ids(engine, "影是谁？") == ["character_token"]
