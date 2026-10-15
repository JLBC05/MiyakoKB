"""Strict boundary normalization for persisted Insight page analysis."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine
from src.backend_v2.storage.schema import analysis_runs


ALLOWED_IMPORTANCE = frozenset({"high", "medium", "normal"})
CORE_PAGE_FIELDS = frozenset(
    {
        "page_number",
        "page_summary",
        "key_events",
        "continuity_notes",
        "warnings",
    }
)
PAGE_FIELDS = CORE_PAGE_FIELDS
EVENT_FIELDS = frozenset({"summary", "importance", "event_type"})
WARNING_FIELDS = frozenset({"code", "message"})
CORE_PERSISTED_FIELDS = frozenset(
    {
        "page_id",
        "source_asset_id",
        "source_checksum",
        "page_number_snapshot",
        "page_summary",
        "key_events",
        "continuity_notes",
        "warnings",
    }
)
PERSISTED_FIELDS = CORE_PERSISTED_FIELDS


class InvalidPageAnalysis(ValueError):
    pass


def _validate_entity_list(items: object, field_name: str) -> list[dict[str, Any]]:
    """Validate that extra entity field is a list[dict] where each dict contains a non-empty name."""
    if not isinstance(items, list):
        raise InvalidPageAnalysis(f"{field_name} must be an array")
    normalized_list: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise InvalidPageAnalysis(f"{field_name}[{index}] must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise InvalidPageAnalysis(
                f"{field_name}[{index}].name must be a non-empty string"
            )
        normalized_item: dict[str, Any] = {}
        for k, v in item.items():
            if not isinstance(k, str) or not k:
                raise InvalidPageAnalysis(f"{field_name}[{index}] key must be a non-empty string")
            normalized_item[k] = v
        normalized_item["name"] = name.strip()
        normalized_list.append(normalized_item)
    return normalized_list


def normalize_page_analysis(
    raw: Mapping[str, Any],
    *,
    page_id: str,
    source_asset_id: str,
    source_checksum: str,
    page_number: int,
    extra_entities: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Return only the canonical persisted page-analysis fields."""

    _require_identity(
        page_id=page_id,
        source_asset_id=source_asset_id,
        source_checksum=source_checksum,
        page_number=page_number,
    )
    extra_keys = tuple(extra_entities or ())
    page = normalize_model_page(
        _extract_page(raw),
        page_number=page_number,
        extra_entities=extra_keys,
    )
    result = {
        "page_id": page_id,
        "source_asset_id": source_asset_id,
        "source_checksum": source_checksum,
        "page_number_snapshot": page_number,
        **{key: page[key] for key in CORE_PAGE_FIELDS - {"page_number"}},
    }
    for key in extra_keys:
        if key in page:
            result[key] = page[key]
    return result


def normalize_model_page(
    page: Mapping[str, Any],
    *,
    page_number: int,
    extra_entities: Sequence[str] | None = None,
) -> dict[str, Any]:
    """The same model fields are checked during generation and publication."""
    extra_keys = set(extra_entities or ())
    allowed_fields = CORE_PAGE_FIELDS | extra_keys
    if not CORE_PAGE_FIELDS.issubset(set(page)):
        missing = sorted(CORE_PAGE_FIELDS - set(page))
        raise InvalidPageAnalysis(
            f"page analysis missing required core fields: {', '.join(missing)}"
        )
    if not set(page).issubset(allowed_fields):
        unknown = sorted(set(page) - allowed_fields)
        raise InvalidPageAnalysis(
            f"page analysis contains unknown fields: {', '.join(unknown)}"
        )
    if (
        isinstance(page["page_number"], bool)
        or not isinstance(page["page_number"], int)
        or page["page_number"] != page_number
    ):
        raise InvalidPageAnalysis(f"page_number must be {page_number}")
    summary = _required_text(page["page_summary"], "page_summary")
    continuity = _text(page["continuity_notes"], "continuity_notes")
    raw_events = page["key_events"]
    if not isinstance(raw_events, list):
        raise InvalidPageAnalysis("key_events must be an array")
    events: list[dict[str, str]] = []
    for index, event in enumerate(raw_events):
        if not isinstance(event, Mapping):
            raise InvalidPageAnalysis(f"key_events[{index}] must be an object")
        if set(event) not in (
            {"summary", "importance"},
            EVENT_FIELDS,
        ):
            raise InvalidPageAnalysis(
                f"key_events[{index}] fields are invalid"
            )
        event_summary = _required_text(
            event["summary"],
            f"key_events[{index}].summary",
        )
        importance = event["importance"]
        if not isinstance(importance, str) or importance not in ALLOWED_IMPORTANCE:
            raise InvalidPageAnalysis(
                f"key_events[{index}].importance is invalid"
            )
        normalized = {
            "summary": event_summary,
            "importance": importance,
        }
        if "event_type" in event:
            normalized["event_type"] = _required_text(
                event["event_type"],
                f"key_events[{index}].event_type",
            )
        events.append(normalized)

    raw_warnings = page["warnings"]
    if not isinstance(raw_warnings, list):
        raise InvalidPageAnalysis("warnings must be an array")
    warnings: list[dict[str, str]] = []
    for index, warning in enumerate(raw_warnings):
        if not isinstance(warning, Mapping):
            raise InvalidPageAnalysis(f"warnings[{index}] must be an object")
        if set(warning) != WARNING_FIELDS:
            raise InvalidPageAnalysis(f"warnings[{index}] fields are invalid")
        code = _required_text(
            warning["code"],
            f"warnings[{index}].code",
        )
        message = _required_text(
            warning["message"],
            f"warnings[{index}].message",
        )
        warnings.append({"code": code, "message": message})

    result: dict[str, Any] = {
        "page_number": page_number,
        "page_summary": summary,
        "key_events": events,
        "continuity_notes": continuity,
        "warnings": warnings,
    }
    for key in sorted(extra_keys):
        raw_val = page.get(key, [])
        result[key] = _validate_entity_list(raw_val, key)

    return result


def validate_persisted_page_analysis(
    raw: object,
    *,
    extra_entities: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Validate persisted page-analysis shape supporting configured extra entities."""
    extra_keys = set(extra_entities or ())
    expected_fields = CORE_PERSISTED_FIELDS | extra_keys
    if not isinstance(raw, Mapping):
        raise InvalidPageAnalysis("persisted page analysis must be an object")
    if not CORE_PERSISTED_FIELDS.issubset(set(raw)):
        missing = sorted(CORE_PERSISTED_FIELDS - set(raw))
        raise InvalidPageAnalysis(
            f"persisted page analysis missing core fields: {', '.join(missing)}"
        )
    if not set(raw).issubset(expected_fields):
        unknown = sorted(set(raw) - expected_fields)
        raise InvalidPageAnalysis(
            f"persisted page analysis fields do not match schema: {', '.join(unknown)}"
        )
    model_page_input: dict[str, Any] = {
        "page_number": raw["page_number_snapshot"],
        "page_summary": raw["page_summary"],
        "key_events": raw["key_events"],
        "continuity_notes": raw["continuity_notes"],
        "warnings": raw["warnings"],
    }
    for k in extra_keys:
        if k in raw:
            model_page_input[k] = raw[k]

    normalized = normalize_page_analysis(
        {"pages": [model_page_input]},
        page_id=raw["page_id"],
        source_asset_id=raw["source_asset_id"],
        source_checksum=raw["source_checksum"],
        page_number=raw["page_number_snapshot"],
        extra_entities=tuple(extra_keys),
    )
    if dict(raw) != normalized:
        raise InvalidPageAnalysis("persisted page analysis is not canonical")
    return normalized


def _extract_page(
    raw: Mapping[str, Any],
) -> Mapping[str, Any]:
    if set(raw) != {"pages"}:
        raise InvalidPageAnalysis("model result must contain only pages")
    pages = raw["pages"]
    if not isinstance(pages, list):
        raise InvalidPageAnalysis("pages must be an array")
    if len(pages) != 1 or not isinstance(pages[0], Mapping):
        raise InvalidPageAnalysis("pages must contain exactly one page object")
    return pages[0]


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidPageAnalysis(f"{field} must be a string")
    return value.strip()


def _required_text(value: object, field: str) -> str:
    normalized = _text(value, field)
    if not normalized:
        raise InvalidPageAnalysis(f"{field} is required")
    return normalized


def _require_identity(
    *,
    page_id: object,
    source_asset_id: object,
    source_checksum: object,
    page_number: object,
) -> None:
    if not isinstance(page_id, str) or not page_id:
        raise InvalidPageAnalysis("page_id must be a non-empty string")
    if not isinstance(source_asset_id, str) or not source_asset_id:
        raise InvalidPageAnalysis("source_asset_id must be a non-empty string")
    if (
        not isinstance(source_checksum, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_checksum) is None
    ):
        raise InvalidPageAnalysis("source_checksum must be a lowercase SHA-256 digest")
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise InvalidPageAnalysis("page_number must be a positive integer")


def get_run_extra_entities(
    run_id: str,
    target: Connection | Engine,
) -> tuple[str, ...]:
    """Read extra_entities configuration from analysis_runs.config_json for the specified run."""
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    stmt = select(analysis_runs.c.config_json).where(analysis_runs.c.id == run_id)
    if isinstance(target, Engine):
        with target.connect() as conn:
            raw_json = conn.execute(stmt).scalar_one_or_none()
    else:
        raw_json = target.execute(stmt).scalar_one_or_none()

    if raw_json is None:
        return ()

    if isinstance(raw_json, (bytes, bytearray)):
        raw_json = raw_json.decode("utf-8")
    if isinstance(raw_json, str):
        config = json.loads(raw_json)
    elif isinstance(raw_json, Mapping):
        config = raw_json
    else:
        config = {}

    analysis_cfg = config.get("analysis")
    if isinstance(analysis_cfg, Mapping):
        knowledge_mode = analysis_cfg.get("knowledgeMode")
        extra_entities = analysis_cfg.get("extraEntities")
    else:
        knowledge_mode = config.get("knowledgeMode")
        extra_entities = config.get("extraEntities")

    if extra_entities is not None:
        if isinstance(extra_entities, (list, tuple)):
            return tuple(str(x) for x in extra_entities if isinstance(x, str) and x.strip())
    elif knowledge_mode:
        return ("characters", "locations", "abilities", "items")
    return ()


def get_run_slug_mode(
    run_id: str,
    target: Connection | Engine,
) -> str:
    """Read slugMode / slug_mode configuration from analysis_runs.config_json for the specified run."""
    if not isinstance(run_id, str) or not run_id:
        return "pinyin"
    stmt = select(analysis_runs.c.config_json).where(analysis_runs.c.id == run_id)
    if isinstance(target, Engine):
        with target.connect() as conn:
            raw_json = conn.execute(stmt).scalar_one_or_none()
    else:
        raw_json = target.execute(stmt).scalar_one_or_none()

    if raw_json is None:
        return "pinyin"

    if isinstance(raw_json, (bytes, bytearray)):
        raw_json = raw_json.decode("utf-8")
    if isinstance(raw_json, str):
        try:
            config = json.loads(raw_json)
        except Exception:
            return "pinyin"
    elif isinstance(raw_json, Mapping):
        config = raw_json
    else:
        return "pinyin"

    analysis_cfg = config.get("analysis")
    slug_mode = None
    if isinstance(analysis_cfg, Mapping):
        slug_mode = analysis_cfg.get("slugMode") or analysis_cfg.get("slug_mode")
    if not slug_mode:
        slug_mode = config.get("slugMode") or config.get("slug_mode")

    if isinstance(slug_mode, str) and slug_mode.strip():
        mode = slug_mode.strip().lower()
        if mode == "english":
            mode = "romanized"
        if mode in {"pinyin", "romanized"}:
            return mode
    return "pinyin"


def deserialize_page_analysis(
    payload: object,
    *,
    run_id: str,
    target: Connection | Engine,
) -> dict[str, Any]:
    """Uniform entry point to deserialize and validate page analysis using run's dynamic configuration."""
    extra_entities = get_run_extra_entities(run_id, target)
    return validate_persisted_page_analysis(payload, extra_entities=extra_entities)
