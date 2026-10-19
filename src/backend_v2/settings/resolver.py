"""Resolve mutable settings into immutable, secret-free job configuration."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Mapping

from sqlalchemy import Engine, select

from src.backend_v2.auth.credential_broker import credential_reference
from src.backend_v2.auth.ownership import effective_owner_id
from src.backend_v2.runtime_profile import resolve_runtime_profile
from src.shared.ai_providers import get_provider_manifest
from src.backend_v2.settings.validation import (
    compose_editable_settings,
    validate_book_setting_payload,
    validate_provider_setting_payload,
)
from src.backend_v2.storage.database import read_transaction
from src.backend_v2.storage.schema import (
    app_settings,
    book_settings,
    books,
    prompts,
    provider_settings,
)


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _deep_merge(
    base: Mapping[str, Any],
    override: Mapping[str, Any],
) -> dict[str, Any]:
    result = deepcopy(dict(base))
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(current, value)
        else:
            result[key] = deepcopy(value)
    return result


def _validated_provider_row(row: Mapping[str, Any]) -> dict[str, Any]:
    domain = str(row["domain"])
    provider = str(row["provider"])
    payload = validate_provider_setting_payload(
        domain,
        provider,
        json.loads(row["payload_json"]),
    )
    return {
        "payload": payload,
        "credentialVersionId": row["credential_version_id"],
        "revision": row["revision"],
    }


def _frozen_openai_options(
    value: object,
    *,
    wire_format: bool,
) -> dict[str, Any]:
    options = _object(value, "OpenAI options")
    request = _object(options["request"], "OpenAI request options")
    execution = _object(options["execution"], "OpenAI execution options")
    if wire_format:
        return {
            "request": {
                "force_json_output": request["force_json_output"],
                "temperature": request["temperature"],
                "extra_body": deepcopy(request["extra_body"]),
            },
            "execution": {
                "use_stream": execution["use_stream"],
                "rpm_limit": execution["rpm_limit"],
                "transport_retries": execution["transport_retries"],
                "business_retries": execution["business_retries"],
            },
        }
    return {
        "request": {
            "force_json_output": request["forceJsonOutput"],
            "temperature": request.get("temperature"),
            "extra_body": deepcopy(dict(request.get("extraBody", {}))),
        },
        "execution": {
            "use_stream": execution["useStream"],
            "rpm_limit": execution["rpmLimit"],
            "transport_retries": execution["transportRetries"],
            "business_retries": execution["businessRetries"],
        },
    }


def _provider_payload(
    *,
    domain: str,
    selected: Mapping[str, Any],
    provider_rows: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, Any]:
    provider = selected["provider"]
    row = provider_rows.get((domain, provider), {})
    return _deep_merge(selected, dict(row["payload"]) if row else {})


def _browser_credential_reference(domain: str, provider: str) -> str | None:
    if not resolve_runtime_profile().browser_credentials:
        return None
    if domain == "ocr" and provider == "baidu":
        return credential_reference(domain, provider)
    try:
        requires_api_key = get_provider_manifest(provider).requires_api_key
    except ValueError:
        requires_api_key = False
    return credential_reference(domain, provider) if requires_api_key else None


def _provider_section(
    *,
    domain: str,
    payload: Mapping[str, Any],
    provider_rows: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, Any]:
    provider = payload["provider"]
    row = provider_rows.get((domain, provider), {})
    section = {
        "provider": provider,
        "model_name": payload["modelName"],
        "custom_base_url": payload["customBaseUrl"],
    }
    if "openaiOptions" in payload:
        section["openai_options"] = _frozen_openai_options(
            payload["openaiOptions"],
            wire_format=domain in {"insight_vlm", "insight_chat"},
        )
    for source_key, target_key in {
        "translationMode": "translation_mode",
        "promptMode": "prompt_mode",
        "batchSize": "batch_size",
        "minImageSize": "min_image_size",
        "imageMaxSize": "image_max_size",
        "analyzeConcurrency": "analyze_concurrency",
        "rpmLimit": "rpm_limit",
        "transportRetries": "transport_retries",
        "businessRetries": "business_retries",
        "timeoutSeconds": "timeout_seconds",
        "version": "version",
        "sourceLanguage": "source_language",
    }.items():
        if source_key in payload:
            section[target_key] = payload[source_key]
    prompt = payload.get("prompt")
    if prompt is not None:
        section["prompt_content"] = prompt
    credential_version_id = row.get("credentialVersionId") or (
        _browser_credential_reference(domain, str(provider))
    )
    if credential_version_id:
        section["credentialVersionId"] = credential_version_id
    return section


def _insight_provider_factory_payload(
    domain: str,
    provider: str,
) -> dict[str, Any]:
    model_type = {
        "insight_vlm": "vlm",
        "insight_chat": "chat",
        "insight_embedding": "embedding",
        "insight_reranker": "reranker",
        "insight_image_gen": "image_gen",
    }[domain]
    payload: dict[str, Any] = {
        "modelName": get_provider_manifest(provider).default_models.get(
            model_type,
            "",
        ),
        "customBaseUrl": "",
    }
    if domain in {"insight_vlm", "insight_chat"}:
        payload["openaiOptions"] = {
            "request": {
                "force_json_output": False,
                "temperature": 0.3 if domain == "insight_vlm" else None,
                "extra_body": {},
            },
            "execution": {
                "use_stream": True,
                "rpm_limit": 0,
                "transport_retries": 3,
                "business_retries": 3,
            },
        }
        if domain == "insight_vlm":
            payload["imageMaxSize"] = 0
        return payload
    if domain == "insight_embedding":
        payload["rpmLimit"] = 0
    payload.update(
        {
            "transportRetries": 3,
            "businessRetries": 3,
            "timeoutSeconds": 0,
        }
    )
    return payload


_INSIGHT_LAYER_PRESETS: dict[str, tuple[dict[str, Any], ...]] = {
    "simple": (
        {"name": "批量分析", "unitsPerGroup": 5, "alignToChapter": False},
        {"name": "全书总结", "unitsPerGroup": 0, "alignToChapter": False},
    ),
    "standard": (
        {"name": "批量分析", "unitsPerGroup": 5, "alignToChapter": False},
        {"name": "段落总结", "unitsPerGroup": 5, "alignToChapter": False},
        {"name": "全书总结", "unitsPerGroup": 0, "alignToChapter": False},
    ),
    "chapter_based": (
        {"name": "批量分析", "unitsPerGroup": 5, "alignToChapter": True},
        {"name": "章节总结", "unitsPerGroup": 0, "alignToChapter": True},
        {"name": "全书总结", "unitsPerGroup": 0, "alignToChapter": False},
    ),
    "full": (
        {"name": "批量分析", "unitsPerGroup": 5, "alignToChapter": False},
        {"name": "小总结", "unitsPerGroup": 5, "alignToChapter": False},
        {"name": "章节总结", "unitsPerGroup": 0, "alignToChapter": True},
        {"name": "全书总结", "unitsPerGroup": 0, "alignToChapter": False},
    ),
}


def _insight_layers(
    preset: str,
    custom_layers: object,
) -> list[dict[str, Any]]:
    if preset == "custom":
        if not isinstance(custom_layers, list):
            raise ValueError("Insight custom layers must be an array")
        raw_layers: object = custom_layers
    else:
        try:
            raw_layers = _INSIGHT_LAYER_PRESETS[preset]
        except KeyError as exc:
            raise ValueError("Insight architecture preset is invalid") from exc
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_layers):
        if not isinstance(raw, Mapping):
            raise ValueError("Insight layer definitions must be objects")
        if set(raw) != {"name", "unitsPerGroup", "alignToChapter"}:
            raise ValueError("Insight layer definition fields are invalid")
        name_value = raw["name"]
        if not isinstance(name_value, str) or not name_value.strip():
            raise ValueError("Insight layer name is required")
        name = name_value.strip()
        units = raw["unitsPerGroup"]
        if isinstance(units, bool) or not isinstance(units, int) or units < 0:
            raise ValueError("Insight layer unitsPerGroup must be a nonnegative integer")
        align = raw["alignToChapter"]
        if not isinstance(align, bool):
            raise ValueError("Insight layer alignToChapter must be boolean")
        result.append(
            {
                "index": index,
                "name": name,
                "unitsPerGroup": units,
                "alignToChapter": align,
            }
        )
    if len(result) < 2:
        raise ValueError("Insight architecture must contain at least 2 layers")
    return result


class SettingsResolver:
    """Read current backend facts once and freeze the effective task config."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def resolve_web_import(
        self,
        *,
        source_url: str,
    ) -> dict[str, Any]:
        with read_transaction(self.engine) as connection:
            app_row = connection.execute(
                select(
                    app_settings.c.payload_json,
                    app_settings.c.revision,
                ).where(
                    app_settings.c.domain == "web_import",
                    app_settings.c.owner_user_id == effective_owner_id(),
                )
            ).mappings().one_or_none()
            raw_provider_rows = connection.execute(
                select(
                    provider_settings.c.domain,
                    provider_settings.c.provider,
                    provider_settings.c.payload_json,
                    provider_settings.c.credential_version_id,
                    provider_settings.c.revision,
                ).where(
                    provider_settings.c.owner_user_id == effective_owner_id(),
                    provider_settings.c.domain.in_(
                        (
                            "web_import_agent",
                            "web_import_firecrawl",
                            "web_import_http",
                        )
                    )
                )
            ).mappings()
            provider_rows = {
                (str(row["domain"]), str(row["provider"])):
                    _validated_provider_row(row)
                for row in raw_provider_rows
            }

        if app_row is None:
            raise ValueError("web_import settings are missing")
        effective = compose_editable_settings(
            "web_import",
            json.loads(app_row["payload_json"]),
        )
        download = dict(effective["download"])
        extraction = dict(effective["extraction"])
        agent_selected = _provider_payload(
            domain="web_import_agent",
            selected=dict(effective["agent"]),
            provider_rows=provider_rows,
        )
        agent = _provider_section(
            domain="web_import_agent",
            payload=agent_selected,
            provider_rows=provider_rows,
        )
        agent.update(
            {
                "useStream": agent_selected["useStream"],
                "forceJsonOutput": agent_selected["forceJsonOutput"],
                "maxRetries": agent_selected["maxRetries"],
                "timeout": agent_selected["timeout"],
            }
        )
        firecrawl_row = provider_rows.get(
            ("web_import_firecrawl", "firecrawl"),
            {},
        )
        http_row = provider_rows.get(("web_import_http", "headers"), {})
        options: dict[str, Any] = {
            "concurrency": download["concurrency"],
            "timeout": download["timeout"],
            "retries": download["retries"],
            "delay": download["delay"],
            "referer": source_url if download["useReferer"] else None,
            "agent": agent,
            "extraction": {
                "prompt": extraction["prompt"],
                "maxIterations": extraction["maxIterations"],
            },
            "imagePreprocess": dict(effective["imagePreprocess"]),
            "bypassProxy": dict(effective["advanced"])["bypassProxy"],
            "autoImport": dict(effective["ui"])["autoImport"],
            "settingsSnapshot": {
                "appRevision": int(app_row["revision"]),
                "agentProviderRevision": int(
                    provider_rows.get(
                        (
                            "web_import_agent",
                            agent_selected["provider"],
                        ),
                        {},
                    ).get("revision", 0)
                ),
            },
        }
        if firecrawl_row.get("credentialVersionId"):
            options["firecrawl"] = {
                "credentialVersionId": firecrawl_row["credentialVersionId"]
            }
        if http_row.get("credentialVersionId"):
            options["http"] = {
                "credentialVersionId": http_row["credentialVersionId"]
            }
        return options

    def resolve_insight(
        self,
        *,
        book_id: str,
        scope: str,
    ) -> dict[str, Any]:
        """Freeze the complete, secret-free configuration for one Insight run."""

        prompt_types = (
            "batch_analysis",
            "segment_summary",
            "chapter_summary",
            "book_overview",
            "group_summary",
            "qa_response",
            "analysis_system",
        )
        provider_domains = (
            "insight_vlm",
            "insight_chat",
            "insight_embedding",
            "insight_reranker",
            "insight_image_gen",
        )
        with read_transaction(self.engine) as connection:
            app_row = connection.execute(
                select(
                    app_settings.c.payload_json,
                    app_settings.c.revision,
                ).where(
                    app_settings.c.domain == "insight",
                    app_settings.c.owner_user_id == effective_owner_id(),
                )
            ).mappings().one_or_none()
            book_row = connection.execute(
                select(
                    book_settings.c.payload_json,
                    book_settings.c.revision,
                ).where(
                    book_settings.c.book_id == book_id,
                    book_settings.c.domain == "insight",
                    select(books.c.id)
                    .where(
                        books.c.id == book_id,
                        books.c.owner_user_id == effective_owner_id(),
                    )
                    .exists(),
                )
            ).mappings().one_or_none()
            raw_provider_rows = connection.execute(
                select(
                    provider_settings.c.domain,
                    provider_settings.c.provider,
                    provider_settings.c.payload_json,
                    provider_settings.c.credential_version_id,
                    provider_settings.c.revision,
                ).where(
                    provider_settings.c.domain.in_(provider_domains),
                    provider_settings.c.owner_user_id == effective_owner_id(),
                )
            ).mappings()
            provider_rows = {
                (str(row["domain"]), str(row["provider"])):
                    _validated_provider_row(row)
                for row in raw_provider_rows
            }
            prompt_rows = list(
                connection.execute(
                    select(
                        prompts.c.id,
                        prompts.c.type,
                        prompts.c.content,
                        prompts.c.revision,
                        prompts.c.is_factory_default,
                    ).where(
                        prompts.c.type.in_(prompt_types),
                        prompts.c.owner_user_id == effective_owner_id(),
                    )
                ).mappings()
            )

        if app_row is None:
            raise ValueError("insight settings are missing")
        global_settings = compose_editable_settings(
            "insight",
            json.loads(app_row["payload_json"]),
        )
        per_book = (
            validate_book_setting_payload(
                "insight",
                json.loads(book_row["payload_json"]),
            )
            if book_row
            else {}
        )
        # Book settings are a complete current profile, not a sparse patch.
        effective = per_book if book_row is not None else global_settings
        factory_by_type: dict[str, Mapping[str, Any]] = {}
        for row in prompt_rows:
            if not row["is_factory_default"]:
                continue
            prompt_type = str(row["type"])
            if prompt_type in factory_by_type:
                raise ValueError(
                    f"multiple Insight factory prompts exist for type {prompt_type}"
                )
            factory_by_type[prompt_type] = row
        frozen_prompts: dict[str, dict[str, Any]] = {}
        for prompt_type in prompt_types:
            row = factory_by_type.get(prompt_type)
            if row is None:
                raise ValueError(
                    f"Insight prompt is missing for type {prompt_type}"
                )
            frozen_prompts[prompt_type] = {
                "promptId": str(row["id"]),
                "revision": int(row["revision"]),
                "content": str(row["content"]),
            }

        def provider(domain: str, key: str) -> dict[str, Any]:
            selected = dict(effective[key])
            selected_provider = selected["provider"]
            row_key = (domain, selected_provider)
            effective_rows = provider_rows
            if row_key not in provider_rows:
                effective_rows = {
                    **provider_rows,
                    row_key: {
                        "payload": _insight_provider_factory_payload(
                            domain,
                            selected_provider,
                        ),
                        "credentialVersionId": None,
                        "revision": 0,
                    },
                }
            payload = _provider_payload(
                domain=domain,
                selected=selected,
                provider_rows=effective_rows,
            )
            return _provider_section(
                domain=domain,
                payload=payload,
                provider_rows=effective_rows,
            )

        analysis = dict(effective["analysis"])
        batch = dict(analysis["batch"])
        architecture_preset = batch["architecturePreset"]
        layers = _insight_layers(
            architecture_preset,
            batch["customLayers"],
        )
        pages_per_batch = batch["pagesPerBatch"]
        context_batch_count = batch["contextBatchCount"]
        knowledge_mode = batch.get("knowledgeMode", False)
        extra_entities = batch.get("extraEntities", ["characters", "locations", "abilities", "items", "concepts"] if knowledge_mode else [])
        slug_mode = batch.get("slugMode", "pinyin")
        if isinstance(slug_mode, str):
            slug_mode = slug_mode.strip().lower()
            if slug_mode == "english":
                slug_mode = "romanized"
            if slug_mode not in {"pinyin", "romanized"}:
                slug_mode = "pinyin"
        else:
            slug_mode = "pinyin"

        vlm_section = provider("insight_vlm", "vlm")
        chat_settings = dict(effective["chat"])
        chat_section = (
            {
                key: deepcopy(vlm_section[key])
                for key in (
                    "provider",
                    "model_name",
                    "custom_base_url",
                    "openai_options",
                    "credentialVersionId",
                )
                if key in vlm_section
            }
            if chat_settings["useSameAsVlm"]
            else provider("insight_chat", "chat")
        )
        sections = {
            "vlm": vlm_section,
            "chat": chat_section,
            "embedding": provider("insight_embedding", "embedding"),
            "reranker": provider("insight_reranker", "reranker"),
            "imageGen": provider("insight_image_gen", "imageGen"),
        }
        provider_revisions: dict[str, int] = {}
        for domain, key in (
            ("insight_vlm", "vlm"),
            ("insight_chat", "chat"),
            ("insight_embedding", "embedding"),
            ("insight_reranker", "reranker"),
            ("insight_image_gen", "imageGen"),
        ):
            selected = dict(effective[key])
            provider_revisions[domain] = int(
                provider_rows.get(
                    (domain, selected["provider"]),
                    {},
                ).get("revision", 0)
            )

        return {
            "executionMode": "sequential",
            "scope": scope,
            "analysis": {
                "pagesPerBatch": pages_per_batch,
                "contextBatchCount": context_batch_count,
                "architecturePreset": architecture_preset,
                "layers": layers,
                "knowledgeMode": knowledge_mode,
                "extraEntities": list(extra_entities),
                "slugMode": slug_mode,
            },
            "slugMode": slug_mode,
            **sections,
            "prompts": frozen_prompts,
            "settingsSnapshot": {
                "appRevision": int(app_row["revision"]),
                "bookRevision": int(book_row["revision"]) if book_row else 0,
                "providerRevisions": provider_revisions,
            },
        }

