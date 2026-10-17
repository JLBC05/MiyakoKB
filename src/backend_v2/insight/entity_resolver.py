"""Entity resolver for Manga Insight QA.

Identifies canonical entities and aliases in user questions with position-aware,
longest-match-first interval consumption.
"""

from __future__ import annotations

import json
from typing import Any
import unicodedata

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.backend_v2.storage.schema import analysis_artifacts

STRUCTURED_QUERY_KEYWORDS = [
    "第几话",
    "第几页",
    "首次",
    "几次",
    "最多",
    "最早",
    "最晚",
    "第一次",
    "出现过",
    "出现次数",
    "频次",
    "什么时候",
    "哪一页",
    "哪一话",
    "几回",
]


def _is_empty_or_pure_punctuation(text: str) -> bool:
    """Return True if text is empty or consists purely of whitespace and punctuation."""
    if not text or not text.strip():
        return True
    return all(unicodedata.category(c).startswith("P") or c.isspace() for c in text)


def resolve_entities_in_question(
    book_id: str,
    question: str,
    engine: Engine,
) -> dict[str, Any]:
    """Resolve entities mentioned in user question against entity_alignment artifact.

    Args:
        book_id: Target book identifier.
        question: User query string.
        engine: SQLAlchemy engine connection.

    Returns:
        Dict with 'matched_entities' and 'is_structured_query'.
    """
    is_structured_query = any(kw in question for kw in STRUCTURED_QUERY_KEYWORDS) if question else False

    with engine.connect() as conn:
        stmt = (
            select(analysis_artifacts)
            .where(
                analysis_artifacts.c.book_id == book_id,
                analysis_artifacts.c.kind == "entity_alignment",
                analysis_artifacts.c.is_active == True,
            )
            .order_by(analysis_artifacts.c.created_at.desc())
        )
        row = conn.execute(stmt).mappings().first()

        if not row:
            stmt_fallback = (
                select(analysis_artifacts)
                .where(
                    analysis_artifacts.c.book_id == book_id,
                    analysis_artifacts.c.kind == "entity_alignment",
                )
                .order_by(analysis_artifacts.c.created_at.desc())
            )
            row = conn.execute(stmt_fallback).mappings().first()

    if not row:
        return {
            "matched_entities": [],
            "is_structured_query": is_structured_query,
        }

    payload_raw = row[analysis_artifacts.c.payload_json]
    if isinstance(payload_raw, str):
        artifact = json.loads(payload_raw)
    elif isinstance(payload_raw, dict):
        artifact = payload_raw
    else:
        artifact = {}

    canonical_entities_raw = artifact.get("canonical_entities", [])
    alias_map_raw = artifact.get("alias_map", {})

    entities_by_id: dict[str, dict[str, Any]] = {}
    entities_by_name: dict[str, dict[str, Any]] = {}

    if isinstance(canonical_entities_raw, list):
        for ent in canonical_entities_raw:
            if isinstance(ent, dict):
                eid = ent.get("entity_id")
                if eid:
                    entities_by_id[eid] = ent
                    cname = ent.get("canonical_name")
                    if cname:
                        entities_by_name[cname] = ent
    elif isinstance(canonical_entities_raw, dict):
        for k, v in canonical_entities_raw.items():
            if isinstance(v, dict):
                ent = dict(v)
                if "entity_id" not in ent:
                    ent["entity_id"] = k
                eid = ent["entity_id"]
                entities_by_id[eid] = ent
                cname = ent.get("canonical_name")
                if cname:
                    entities_by_name[cname] = ent

    # Build candidate terms mapping: name -> entity_id
    cand_to_entity: dict[str, str] = {}

    # 1. Canonical names
    for eid, ent in entities_by_id.items():
        cname = ent.get("canonical_name")
        if cname:
            cand_to_entity[cname] = eid

    # 2. Aliases from entity info
    for eid, ent in entities_by_id.items():
        aliases = ent.get("aliases")
        if isinstance(aliases, list):
            for alias in aliases:
                if alias and alias not in cand_to_entity:
                    cand_to_entity[alias] = eid

    # 3. Supplementary mapping from alias_map: alias_name -> canonical_name -> entity_id or alias_name -> entity_id
    if isinstance(alias_map_raw, dict):
        for alias_name, target in alias_map_raw.items():
            if not alias_name or alias_name in cand_to_entity:
                continue
            if target in entities_by_id:
                cand_to_entity[alias_name] = target
            elif target in entities_by_name:
                cand_to_entity[alias_name] = entities_by_name[target]["entity_id"]

    # 4. Dot-separated components for names with middle dot '·' (e.g. "斯科特·朝圣者" -> "斯科特")
    dot_parts_map: dict[str, set[str]] = {}
    for eid, ent in entities_by_id.items():
        names_to_split: list[str] = []
        cname = ent.get("canonical_name")
        if isinstance(cname, str) and cname:
            names_to_split.append(cname)
        aliases = ent.get("aliases")
        if isinstance(aliases, list):
            names_to_split.extend(a for a in aliases if isinstance(a, str) and a)
        for name in names_to_split:
            if "·" in name:
                parts = [p.strip() for p in name.split("·") if len(p.strip()) >= 2]
                for p in parts:
                    dot_parts_map.setdefault(p, set()).add(eid)

    for part, eids in dot_parts_map.items():
        if part not in cand_to_entity:
            if len(eids) == 1:
                cand_to_entity[part] = next(iter(eids))
            else:
                char_eids = [eid for eid in eids if entities_by_id[eid].get("category") == "characters"]
                if len(char_eids) == 1:
                    cand_to_entity[part] = char_eids[0]
                elif char_eids:
                    sorted_char = sorted(
                        char_eids,
                        key=lambda eid: entities_by_id[eid].get("frequency") or len(entities_by_id[eid].get("pages", [])),
                        reverse=True,
                    )
                    cand_to_entity[part] = sorted_char[0]

    # Sort candidate names by descending string length (longest first)
    sorted_candidates = sorted(cand_to_entity.keys(), key=lambda s: (-len(s), s))

    # Position mask matching
    consumed = [False] * len(question)
    first_seen_pos: dict[str, int] = {}

    for cand in sorted_candidates:
        if _is_empty_or_pure_punctuation(cand):
            continue
        cand_len = len(cand)
        start = 0
        while True:
            idx = question.find(cand, start)
            if idx == -1:
                break
            if not any(consumed[idx : idx + cand_len]):
                for i in range(idx, idx + cand_len):
                    consumed[i] = True
                eid = cand_to_entity[cand]
                if eid not in first_seen_pos:
                    first_seen_pos[eid] = idx
                else:
                    first_seen_pos[eid] = min(first_seen_pos[eid], idx)
            start = idx + 1

    # Deduplicate and sort hit entities by first appearance index
    matched_entity_ids = sorted(first_seen_pos.keys(), key=lambda eid: first_seen_pos[eid])
    matched_entities: list[dict[str, Any]] = []

    for eid in matched_entity_ids:
        ent = entities_by_id[eid]
        freq = ent.get("frequency")
        if freq is None:
            freq = len(ent.get("pages", []))
        matched_entities.append({
            "entity_id": ent["entity_id"],
            "canonical_name": ent.get("canonical_name", ""),
            "category": ent.get("category", "characters") or "characters",
            "aliases": list(ent.get("aliases", [])),
            "pages": list(ent.get("pages", [])),
            "frequency": freq,
        })

    return {
        "matched_entities": matched_entities,
        "is_structured_query": is_structured_query,
    }
