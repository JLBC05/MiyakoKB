from __future__ import annotations

import io
import json
from pathlib import Path
import zipfile

from src.backend_v2.insight.character_skill import (
    CHARACTER_CARD_KIND,
    CHARACTER_SKILL_KIND,
    LOREBOOK_KIND,
    PageEvidence,
    _Facts,
    _archive_filename,
    _build_card_payload,
    _build_lorebook_payload,
    _lorebook_entries,
    build_skill_archive,
)


def _page(number: int, *, chapter: str = "第1話", ordinal: int = 1, summary: str = "") -> PageEvidence:
    return PageEvidence(
        global_page=number,
        chapter_title=chapter,
        chapter_ordinal=1,
        page_ordinal=ordinal,
        page_id=f"page-{number}",
        page_summary=summary or f"第 {number} 页的梗概",
        key_events=[{"summary": f"第 {number} 页发生了事件", "importance": "high"}],
        characters=["久留美"],
        other_entities={"concepts": ["FX"]},
        asset_id=f"asset-{number}",
        asset_path=Path("objects") / f"asset-{number}",
    )


def _facts() -> _Facts:
    return _Facts(
        chapter_distribution={"第1話": 2},
        co_occurrence=[("康子", 2)],
        concepts=[("FX", 3)],
        events=[{"summary": "久留美加仓", "importance": "high", "citation": "《第1話》第1页（全书第1页）"}],
        solo_pages=[1],
        importance={"high": 1},
    )


def _character() -> dict[str, object]:
    return {
        "entity_id": "character_jiu_liu_mei",
        "canonical_name": "久留美",
        "aliases": ["くるみ"],
        "category": "characters",
        "pages": [1, 2],
        "total_frequency": 2,
    }


def test_card_payload_renders_v3_and_v2_sillytavern_files() -> None:
    card = {
        "name": "久留美",
        "description": "一名做 FX 交易的大学生。",
        "personality": "乐观、冲动",
        "scenario": "外汇账户浮亏中",
        "first_mes": "追加 long!!",
        "mes_example": "<START>\n{{user}}: 还好吗\n{{char}}: 没事",
        "creator_notes": "依据知识库生成",
        "system_prompt": "扮演久留美",
        "post_history_instructions": "不要编造剧情",
        "alternate_greetings": ["再来一次"],
        "tags": ["FX", "大学生"],
        "evidence": [{"claim": "她是大学生", "citation": "《第1話》第1页（全书第1页）"}],
    }
    payload = _build_card_payload(
        book_id="book-1",
        book_title="FX战士久留美",
        character=_character(),
        run_id="run-1",
        facts=_facts(),
        evidence=[_page(1)],
        observations=[],
        card=card,
        dropped=0,
        vlm_model="gemini-3.8-flash-high",
        chat_model="gemini-3.8-flash-high",
    )

    assert payload["kind"] == CHARACTER_CARD_KIND
    assert payload["stats"]["pages"] == 2
    assert payload["card"]["spec"] == "chara_card_v3"
    assert payload["card"]["data"]["name"] == "久留美"
    assert "《第1話》第1页（全书第1页）" in payload["card"]["data"]["description"]
    assert payload["card"]["data"]["extensions"]["miyako"]["entityId"] == "character_jiu_liu_mei"

    files = payload["files"]
    assert set(files) == {"character-card-v3.json", "character-card-v2.json", "character-card.md"}
    v2 = json.loads(files["character-card-v2.json"])
    assert v2["spec"] == "chara_card_v2"
    assert "group_only_greetings" not in v2["data"]
    assert v2["data"]["mes_example"].startswith("<START>")
    assert "久留美" in files["character-card.md"]


def test_lorebook_entries_quote_page_evidence_and_keep_aliases_as_triggers() -> None:
    entities = [
        {
            "entity_id": "character_jiu_liu_mei",
            "canonical_name": "久留美",
            "category": "characters",
            "aliases": ["くるみ", "福賀久留美"],
            "pages": [1, 2, 3],
        },
        {
            "entity_id": "location_xue_xiao",
            "canonical_name": "学校",
            "category": "locations",
            "aliases": [],
            "pages": [4],
        },
    ]
    preview = {"character_jiu_liu_mei": [1, 3], "location_xue_xiao": [4]}
    by_page = {number: _page(number) for number in (1, 3, 4)}

    entries = _lorebook_entries(
        book_title="FX战士久留美",
        entities=entities,
        preview=preview,
        by_page=by_page,
    )

    assert [entry["uid"] for entry in entries] == [0, 1]
    assert entries[0]["key"] == ["久留美", "くるみ", "福賀久留美"]
    assert entries[0]["comment"] == "角色：久留美"
    assert "出场 3 页" in entries[0]["content"]
    assert "《第1話》第1页（全书第1页）" in entries[0]["content"]

    payload = _build_lorebook_payload(
        book_id="book-1",
        book_title="FX战士久留美",
        run_id="run-1",
        entries=entries,
        entity_total=2,
        page_total=3,
    )
    assert payload["kind"] == LOREBOOK_KIND
    assert payload["stats"]["entries"] == 2
    assert payload["stats"]["byCategory"] == {"characters": 1, "locations": 1}
    world = json.loads(payload["files"]["lorebook.json"])
    assert set(world["entries"]) == {"0", "1"}
    assert world["entries"]["0"]["content"].startswith("《FX战士久留美》角色")
    assert "世界书" in payload["files"]["lorebook.md"]


def test_archive_filenames_differ_per_kind_and_strip_unsafe_characters() -> None:
    document = {
        "bookTitle": "FX/战士久留美",
        "characterName": "久留美",
        "files": {"a.txt": "x"},
    }
    assert _archive_filename(document, CHARACTER_SKILL_KIND) == "FX_战士久留美-久留美-skill.zip"
    assert _archive_filename(document, CHARACTER_CARD_KIND) == "FX_战士久留美-久留美-card.zip"
    assert _archive_filename(document, LOREBOOK_KIND) == "FX_战士久留美-worldbook.zip"

    filename, payload = build_skill_archive(document, CHARACTER_CARD_KIND)
    assert filename.endswith("-card.zip")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.namelist() == ["a.txt"]
        assert archive.read("a.txt") == b"x"
