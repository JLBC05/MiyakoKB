"""Closed backend validation for persisted settings and provider facts."""

from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from src.backend_v2.content.page_style import validate_text_style_payload
from src.backend_v2.jobs import MAX_DEEP_LEARNING_CONCURRENCY
from src.backend_v2.storage.defaults import (
    DEFAULT_WEB_IMPORT_SETTINGS,
)
from src.shared.ai_providers import (
    CHAT_CAPABILITY,
    EMBEDDING_CAPABILITY,
    HQ_TRANSLATION_CAPABILITY,
    IMAGE_GEN_CAPABILITY,
    RERANK_CAPABILITY,
    TRANSLATION_CAPABILITY,
    VISION_OCR_CAPABILITY,
    VLM_CAPABILITY,
    WEB_IMPORT_AGENT_CAPABILITY,
    get_provider_manifest,
    provider_supports_capability,
)
from src.shared.constants import PADDLEOCR_VL_LANGUAGE_NAMES


APP_SETTING_DOMAINS = frozenset({
    "translation", "text_style_defaults",
    "workflow_preferences", "export_preferences", "custom_ai_profiles",
    "manga_import",
    "web_import", "insight",
})
PROVIDER_CAPABILITIES = {
    "translation": TRANSLATION_CAPABILITY,
    "hq": HQ_TRANSLATION_CAPABILITY,
    "ai_vision_ocr": VISION_OCR_CAPABILITY,
    "web_import_agent": WEB_IMPORT_AGENT_CAPABILITY,
    "insight_vlm": VLM_CAPABILITY,
    "insight_chat": CHAT_CAPABILITY,
    "insight_embedding": EMBEDDING_CAPABILITY,
    "insight_reranker": RERANK_CAPABILITY,
    "insight_image_gen": IMAGE_GEN_CAPABILITY,
}
_UUID_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_PROOFREADING_DOMAIN = re.compile(
    r"^proofreading_[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_CUSTOM_AI_PROFILE_KINDS = frozenset(
    {"chatVision", "embedding", "reranker", "imageGen"}
)
_SECRET_KEYS = frozenset(
    {
        "apikey",
        "apikeys",
        "secretkey",
        "secret",
        "token",
        "password",
        "cookie",
        "headers",
        "credentialversionid",
    }
)
_WORKFLOW_MODES = frozenset(
    {
        "translate-current",
        "translate-batch",
        "hq-batch",
        "proofread-batch",
        "remove-current",
        "remove-batch",
        "retry-failed",
        "delete-current",
        "clear-all",
    }
)
_PROVIDER_PAYLOAD_FIELDS_BY_DOMAIN = {
    "ocr": frozenset({"version", "sourceLanguage"}),
    "translation": frozenset(
        {"modelName", "customBaseUrl", "openaiOptions", "translationMode"}
    ),
    "hq": frozenset(
        {"modelName", "customBaseUrl", "openaiOptions", "batchSize", "prompt"}
    ),
    "ai_vision_ocr": frozenset(
        {
            "modelName",
            "customBaseUrl",
            "openaiOptions",
            "prompt",
            "promptMode",
            "minImageSize",
        }
    ),
}
_PROOFREADING_PROVIDER_PAYLOAD_FIELDS = frozenset(
    {"modelName", "customBaseUrl", "openaiOptions", "batchSize", "prompt"}
)
TRANSLATION_PROVIDER_SECTIONS = {
    "translation": "translation",
    "hqTranslation": "hq",
    "aiVisionOcr": "ai_vision_ocr",
    "baiduOcr": "ocr",
}


_INSIGHT_PROVIDER_PAYLOAD_FIELDS = {
    "insight_vlm": frozenset(
        {
            "modelName",
            "customBaseUrl",
            "openaiOptions",
            "imageMaxSize",
            "analyzeConcurrency",
        }
    ),
    "insight_chat": frozenset(
        {"modelName", "customBaseUrl", "openaiOptions"}
    ),
    "insight_embedding": frozenset(
        {
            "modelName",
            "customBaseUrl",
            "rpmLimit",
            "transportRetries",
            "businessRetries",
            "timeoutSeconds",
        }
    ),
    "insight_reranker": frozenset(
        {
            "modelName",
            "customBaseUrl",
            "transportRetries",
            "businessRetries",
            "timeoutSeconds",
        }
    ),
    "insight_image_gen": frozenset(
        {
            "modelName",
            "customBaseUrl",
            "transportRetries",
            "businessRetries",
            "timeoutSeconds",
        }
    ),
}
# 可选键：老库里的 provider 载荷没有这些键，读取时必须继续合法。
_OPTIONAL_INSIGHT_PROVIDER_PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    "insight_vlm": frozenset({"analyzeConcurrency"}),
}
_WEB_IMPORT_AGENT_PROVIDER_PAYLOAD_FIELDS = frozenset(
    {"modelName", "customBaseUrl"}
)


def is_proofreading_provider_domain(value: object) -> bool:
    return isinstance(value, str) and _PROOFREADING_DOMAIN.fullmatch(value) is not None


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return deepcopy(dict(value))


def _reject_secret_fields(value: object, path: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if normalized in _SECRET_KEYS:
                raise ValueError(f"{path} must not contain secret field {key}")
            _reject_secret_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_fields(child, f"{path}[{index}]")


def _exact_keys(
    value: Mapping[str, object],
    expected: set[str] | frozenset[str],
    path: str,
    *,
    optional: set[str] | frozenset[str] = frozenset(),
) -> None:
    actual = set(value)
    required = set(expected) - set(optional)
    if not required <= actual or actual - set(expected):
        unknown = sorted(actual - set(expected))
        missing = sorted(required - actual)
        detail = []
        if unknown:
            detail.append("unknown=" + ",".join(unknown))
        if missing:
            detail.append("missing=" + ",".join(missing))
        raise ValueError(f"{path} has invalid fields ({'; '.join(detail)})")


def _finite_number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{path} must be a finite number")
    return result


def _integer(
    value: object,
    path: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        if maximum is None:
            raise ValueError(f"{path} must be an integer of at least {minimum}")
        raise ValueError(f"{path} must be an integer from {minimum} to {maximum}")
    return value


def _validate_openai_options(
    value: object,
    path: str,
    *,
    wire_format: bool = False,
) -> None:
    options = _object(value, path)
    _exact_keys(options, {"request", "execution"}, path)
    request = _object(options["request"], f"{path}.request")
    force_json_key = "force_json_output" if wire_format else "forceJsonOutput"
    extra_body_key = "extra_body" if wire_format else "extraBody"
    allowed_request = {force_json_key, "temperature", extra_body_key}
    unknown = set(request) - allowed_request
    required_request = (
        allowed_request if wire_format else {force_json_key}
    )
    if unknown or not required_request.issubset(request):
        raise ValueError(f"{path}.request has invalid fields")
    if not isinstance(request[force_json_key], bool):
        raise ValueError(f"{path}.request.{force_json_key} must be boolean")
    if "temperature" in request:
        if request["temperature"] is None:
            if not wire_format:
                raise ValueError(f"{path}.request.temperature must be from 0 to 2")
        else:
            temperature = _finite_number(
                request["temperature"],
                f"{path}.request.temperature",
            )
            if not 0 <= temperature <= 2:
                raise ValueError(f"{path}.request.temperature must be from 0 to 2")
    if extra_body_key in request and not isinstance(request[extra_body_key], Mapping):
        raise ValueError(f"{path}.request.{extra_body_key} must be an object")
    execution = _object(options["execution"], f"{path}.execution")
    use_stream_key = "use_stream" if wire_format else "useStream"
    rpm_limit_key = "rpm_limit" if wire_format else "rpmLimit"
    transport_retries_key = (
        "transport_retries" if wire_format else "transportRetries"
    )
    business_retries_key = (
        "business_retries" if wire_format else "businessRetries"
    )
    _exact_keys(
        execution,
        {
            use_stream_key,
            rpm_limit_key,
            transport_retries_key,
            business_retries_key,
        },
        f"{path}.execution",
    )
    if not isinstance(execution[use_stream_key], bool):
        raise ValueError(f"{path}.execution.{use_stream_key} must be boolean")
    _integer(
        execution[rpm_limit_key],
        f"{path}.execution.{rpm_limit_key}",
        minimum=0,
        maximum=100_000,
    )
    _integer(
        execution[transport_retries_key],
        f"{path}.execution.{transport_retries_key}",
        minimum=0,
        maximum=100,
    )
    _integer(
        execution[business_retries_key],
        f"{path}.execution.{business_retries_key}",
        minimum=0,
        maximum=100,
    )


def _validate_shape(value: object, template: object, path: str) -> None:
    if path.endswith("openaiOptions"):
        _validate_openai_options(value, path)
        return
    if isinstance(template, Mapping):
        current = _object(value, path)
        _exact_keys(current, set(template), path)
        for key, child_template in template.items():
            _validate_shape(current[key], child_template, f"{path}.{key}")
        return
    if isinstance(template, list):
        if not isinstance(value, list):
            raise ValueError(f"{path} must be an array")
        return
    if isinstance(template, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{path} must be boolean")
        return
    if isinstance(template, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path} must be an integer")
        return
    if isinstance(template, float):
        _finite_number(value, path)
        return
    if isinstance(template, str) and not isinstance(value, str):
        raise ValueError(f"{path} must be a string")


def _require_provider(value: object, capability: str, path: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a provider ID")
    manifest = get_provider_manifest(value)
    if not provider_supports_capability(manifest.id, capability):
        raise ValueError(f"{path} provider does not support {capability}")
    return manifest.id


def _validate_translation(payload: dict[str, Any]) -> None:
    """Validate translation payload (decoupled: accepts dictionary)."""
    if not isinstance(payload, dict):
        raise ValueError("translation setting must be an object")


def _validate_workflow_preferences(payload: dict[str, Any]) -> None:
    _exact_keys(
        payload,
        {"rememberWorkflowModeEnabled", "lastWorkflowMode"},
        "workflow_preferences",
    )
    if not isinstance(payload["rememberWorkflowModeEnabled"], bool):
        raise ValueError("rememberWorkflowModeEnabled must be boolean")
    if payload["lastWorkflowMode"] not in _WORKFLOW_MODES:
        raise ValueError("lastWorkflowMode is invalid")


def _validate_export_preferences(payload: dict[str, Any]) -> None:
    _exact_keys(
        payload,
        {"preserveOriginalFilenames"},
        "export_preferences",
    )
    if not isinstance(payload["preserveOriginalFilenames"], bool):
        raise ValueError("preserveOriginalFilenames must be boolean")


def _validate_custom_ai_profiles(payload: dict[str, Any]) -> None:
    _exact_keys(payload, {"profiles"}, "custom_ai_profiles")
    profiles = payload["profiles"]
    if not isinstance(profiles, list):
        raise ValueError("custom_ai_profiles.profiles must be an array")
    profile_ids: set[str] = set()
    profile_names: set[tuple[str, str]] = set()
    for index, value in enumerate(profiles):
        path = f"custom_ai_profiles.profiles[{index}]"
        profile = _object(value, path)
        _exact_keys(profile, {"id", "name", "kind", "baseUrl", "model"}, path)
        profile_id = profile["id"]
        if not isinstance(profile_id, str) or not _UUID_ID.fullmatch(profile_id):
            raise ValueError(f"{path}.id must be a UUID")
        if profile_id in profile_ids:
            raise ValueError("custom AI profile IDs must be unique")
        profile_ids.add(profile_id)
        name = profile["name"]
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError(f"{path}.name must contain 1 to 80 characters")
        kind = profile["kind"]
        if kind not in _CUSTOM_AI_PROFILE_KINDS:
            raise ValueError(f"{path}.kind is invalid")
        name_identity = (kind, name.strip().casefold())
        if name_identity in profile_names:
            raise ValueError(
                "custom AI profile names must be unique within each kind"
            )
        profile_names.add(name_identity)
        base_url = profile["baseUrl"]
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError(f"{path}.baseUrl is required")
        parsed_url = urlsplit(base_url.strip())
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError(f"{path}.baseUrl must be an absolute HTTP URL")
        if parsed_url.query or parsed_url.fragment:
            raise ValueError(f"{path}.baseUrl must not contain query or fragment")
        model = profile["model"]
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"{path}.model is required")


def _validate_web_import(payload: dict[str, Any]) -> None:
    expected = {
        "firecrawl",
        "agent",
        "extraction",
        "download",
        "imagePreprocess",
        "advanced",
        "ui",
    }
    _exact_keys(payload, expected, "web_import")
    nested_fields = {
        "firecrawl": set(),
        "agent": {
            "provider",
            "customBaseUrl",
            "modelName",
            "useStream",
            "forceJsonOutput",
            "maxRetries",
            "timeout",
        },
        "extraction": {"prompt", "maxIterations"},
        "download": {"concurrency", "timeout", "retries", "delay", "useReferer"},
        "advanced": {"bypassProxy"},
        "ui": {"showAgentLogs", "autoImport"},
    }
    sections: dict[str, dict[str, Any]] = {}
    for key, fields in nested_fields.items():
        section = _object(payload[key], f"web_import.{key}")
        _exact_keys(section, fields, f"web_import.{key}")
        sections[key] = section
    preprocess = _object(payload["imagePreprocess"], "web_import.imagePreprocess")
    _exact_keys(
        preprocess,
        {"enabled", "autoRotate", "compression", "formatConvert"},
        "web_import.imagePreprocess",
    )
    compression = _object(
        preprocess["compression"],
        "web_import.imagePreprocess.compression",
    )
    _exact_keys(
        compression,
        {"enabled", "quality", "maxWidth", "maxHeight"},
        "web_import.imagePreprocess.compression",
    )
    conversion = _object(
        preprocess["formatConvert"],
        "web_import.imagePreprocess.formatConvert",
    )
    _exact_keys(
        conversion,
        {"enabled", "targetFormat"},
        "web_import.imagePreprocess.formatConvert",
    )
    agent = sections["agent"]
    _require_provider(
        agent["provider"],
        WEB_IMPORT_AGENT_CAPABILITY,
        "web_import.agent.provider",
    )
    for path, value in (
        ("agent.customBaseUrl", agent["customBaseUrl"]),
        ("agent.modelName", agent["modelName"]),
        ("extraction.prompt", payload["extraction"]["prompt"]),
    ):
        if not isinstance(value, str):
            raise ValueError(f"web_import.{path} must be a string")
    for path, value in (
        ("agent.useStream", agent["useStream"]),
        ("agent.forceJsonOutput", agent["forceJsonOutput"]),
        ("download.useReferer", payload["download"]["useReferer"]),
        ("imagePreprocess.enabled", preprocess["enabled"]),
        ("imagePreprocess.autoRotate", preprocess["autoRotate"]),
        ("imagePreprocess.compression.enabled", compression["enabled"]),
        ("imagePreprocess.formatConvert.enabled", conversion["enabled"]),
        ("advanced.bypassProxy", payload["advanced"]["bypassProxy"]),
        ("ui.showAgentLogs", payload["ui"]["showAgentLogs"]),
        ("ui.autoImport", payload["ui"]["autoImport"]),
    ):
        if not isinstance(value, bool):
            raise ValueError(f"web_import.{path} must be boolean")
    if not isinstance(conversion["targetFormat"], str):
        raise ValueError("web_import targetFormat must be a string")
    if conversion["targetFormat"] not in {"jpeg", "png", "webp", "original"}:
        raise ValueError("web_import targetFormat is invalid")
    for path, value, minimum in (
        ("agent.maxRetries", agent["maxRetries"], 0),
        ("extraction.maxIterations", payload["extraction"]["maxIterations"], 1),
        ("download.concurrency", payload["download"]["concurrency"], 1),
        ("download.retries", payload["download"]["retries"], 0),
        ("download.delay", payload["download"]["delay"], 0),
        ("compression.maxWidth", compression["maxWidth"], 0),
        ("compression.maxHeight", compression["maxHeight"], 0),
    ):
        _integer(value, f"web_import.{path}", minimum=minimum)
    for path, value in (
        ("agent.timeout", agent["timeout"]),
        ("download.timeout", payload["download"]["timeout"]),
    ):
        timeout = _finite_number(value, f"web_import.{path}")
        if timeout < 1:
            raise ValueError(f"web_import.{path} must be at least 1")
    _integer(
        compression["quality"],
        "web_import.compression.quality",
        minimum=1,
        maximum=100,
    )


def _validate_insight(payload: dict[str, Any]) -> None:
    _exact_keys(
        payload,
        {"analysis", "vlm", "chat", "embedding", "reranker", "imageGen"},
        "insight",
    )
    analysis = _object(payload["analysis"], "insight.analysis")
    _exact_keys(analysis, {"batch"}, "insight.analysis")
    batch = _object(analysis["batch"], "insight.analysis.batch")
    allowed_batch_keys = {
        "pagesPerBatch",
        "contextBatchCount",
        "architecturePreset",
        "customLayers",
        "knowledgeMode",
        "extraEntities",
        "slugMode",
    }
    unknown_batch_keys = set(batch) - allowed_batch_keys
    if unknown_batch_keys:
        raise ValueError(f"insight.analysis.batch has unknown keys: {', '.join(sorted(unknown_batch_keys))}")
    required_batch_keys = {
        "pagesPerBatch",
        "contextBatchCount",
        "architecturePreset",
        "customLayers",
    }
    missing_batch_keys = required_batch_keys - set(batch)
    if missing_batch_keys:
        raise ValueError(f"insight.analysis.batch is missing keys: {', '.join(sorted(missing_batch_keys))}")
    if "knowledgeMode" in batch and not isinstance(batch["knowledgeMode"], bool):
        raise ValueError("insight knowledgeMode must be boolean")
    if "extraEntities" in batch:
        if not isinstance(batch["extraEntities"], list):
            raise ValueError("insight extraEntities must be an array")
        for ent in batch["extraEntities"]:
            if not isinstance(ent, str) or not ent.strip():
                raise ValueError("insight extraEntities entries must be non-empty strings")
    if "slugMode" in batch:
        if not isinstance(batch["slugMode"], str) or batch["slugMode"].strip().lower() not in {"pinyin", "romanized", "english"}:
            raise ValueError("insight slugMode must be 'pinyin' or 'romanized'")
    _integer(
        batch["pagesPerBatch"],
        "insight.analysis.batch.pagesPerBatch",
        minimum=1,
    )
    _integer(
        batch["contextBatchCount"],
        "insight.analysis.batch.contextBatchCount",
        minimum=0,
    )
    if batch["architecturePreset"] not in {
        "simple",
        "standard",
        "chapter_based",
        "full",
        "custom",
    }:
        raise ValueError("insight architecturePreset is invalid")
    layers = batch["customLayers"]
    if not isinstance(layers, list):
        raise ValueError("insight customLayers must be an array")
    if batch["architecturePreset"] == "custom" and len(layers) < 2:
        raise ValueError("custom Insight architecture must contain at least 2 layers")
    for index, layer_value in enumerate(layers):
        layer = _object(
            layer_value,
            f"insight.analysis.batch.customLayers[{index}]",
        )
        _exact_keys(
            layer,
            {"name", "unitsPerGroup", "alignToChapter"},
            f"insight.analysis.batch.customLayers[{index}]",
        )
        if not isinstance(layer["name"], str) or not layer["name"].strip():
            raise ValueError("insight custom layer name is required")
        _integer(
            layer["unitsPerGroup"],
            "insight custom layer unitsPerGroup",
            minimum=0,
        )
        if not isinstance(layer["alignToChapter"], bool):
            raise ValueError("insight custom layer alignToChapter must be boolean")
    capability_by_key = {
        "vlm": VLM_CAPABILITY,
        "chat": CHAT_CAPABILITY,
        "embedding": EMBEDDING_CAPABILITY,
        "reranker": RERANK_CAPABILITY,
        "imageGen": IMAGE_GEN_CAPABILITY,
    }
    for key, capability in capability_by_key.items():
        section = _object(payload[key], f"insight.{key}")
        expected = {"provider", "useSameAsVlm"} if key == "chat" else {"provider"}
        _exact_keys(section, expected, f"insight.{key}")
        provider = section["provider"]
        if provider:
            _require_provider(provider, capability, f"insight.{key}.provider")
        elif not isinstance(provider, str):
            raise ValueError(f"insight.{key}.provider must be a string")
        if key == "chat" and not isinstance(section["useSameAsVlm"], bool):
            raise ValueError("insight.chat.useSameAsVlm must be boolean")


def _validate_manga_import(payload: dict[str, Any]) -> None:
    _exact_keys(payload, {"veneraBaseDir"}, "manga_import")
    value = payload["veneraBaseDir"]
    if not isinstance(value, str):
        raise ValueError("manga_import.veneraBaseDir must be a string")
    if "\x00" in value:
        raise ValueError("manga_import.veneraBaseDir must not contain NUL")
    if len(value) > 4096:
        raise ValueError("manga_import.veneraBaseDir is too long")

def validate_setting_payload(
    domain: str,
    payload: object,
) -> dict[str, Any]:
    if domain not in APP_SETTING_DOMAINS:
        raise ValueError(f"unsupported setting domain: {domain}")
    result = _object(payload, f"{domain} setting")
    _reject_secret_fields(result, domain)
    if domain == "translation":
        _validate_translation(result)
    elif domain == "workflow_preferences":
        _validate_workflow_preferences(result)
    elif domain == "export_preferences":
        _validate_export_preferences(result)
    elif domain == "custom_ai_profiles":
        _validate_custom_ai_profiles(result)
    elif domain == "web_import":
        _validate_web_import(result)
    elif domain == "insight":
        _validate_insight(result)
    elif domain == "manga_import":
        _validate_manga_import(result)
    elif domain == "text_style_defaults":
        font_id, style = validate_text_style_payload(result)
        result = {**style, "fontFamily": font_id}
    return result


def validate_provider_setting_payload(
    domain: str,
    provider: str,
    payload: object,
) -> dict[str, Any]:
    result = _object(payload, "provider setting payload")
    _reject_secret_fields(result, f"provider_settings.{domain}.{provider}")
    if domain in {"web_import_firecrawl", "web_import_http"}:
        expected_provider = "firecrawl" if domain == "web_import_firecrawl" else "headers"
        if provider != expected_provider or result:
            raise ValueError(f"{domain} provider setting is invalid")
        return result
    capability = PROVIDER_CAPABILITIES.get(domain)
    if capability is None and is_proofreading_provider_domain(domain):
        capability = HQ_TRANSLATION_CAPABILITY
    if capability is None:
        if domain == "ocr" and provider == "baidu":
            _exact_keys(
                result,
                {"version", "sourceLanguage"},
                "provider_settings.ocr",
            )
            if result["version"] not in {"standard", "high_precision"}:
                raise ValueError("provider_settings.ocr.version is invalid")
            if result["sourceLanguage"] not in {
                "auto_detect",
                "CHN_ENG",
                "ENG",
                "JAP",
                "KOR",
                "FRE",
                "GER",
                "RUS",
            }:
                raise ValueError("provider_settings.ocr.sourceLanguage is invalid")
            return result
        raise ValueError(f"unsupported provider setting domain: {domain}")
    _require_provider(provider, capability, f"provider_settings.{domain}.provider")
    expected_insight_fields = _INSIGHT_PROVIDER_PAYLOAD_FIELDS.get(domain)
    if domain == "web_import_agent":
        _exact_keys(
            result,
            _WEB_IMPORT_AGENT_PROVIDER_PAYLOAD_FIELDS,
            "provider_settings.web_import_agent",
        )
    elif expected_insight_fields is not None:
        _exact_keys(
            result,
            expected_insight_fields,
            f"provider_settings.{domain}",
            optional=_OPTIONAL_INSIGHT_PROVIDER_PAYLOAD_FIELDS.get(
                domain,
                frozenset(),
            ),
        )
    if is_proofreading_provider_domain(domain):
        allowed_fields = _PROOFREADING_PROVIDER_PAYLOAD_FIELDS
    elif domain in _PROVIDER_PAYLOAD_FIELDS_BY_DOMAIN:
        allowed_fields = _PROVIDER_PAYLOAD_FIELDS_BY_DOMAIN[domain]
    elif expected_insight_fields is not None:
        allowed_fields = expected_insight_fields
    elif domain == "web_import_agent":
        allowed_fields = _WEB_IMPORT_AGENT_PROVIDER_PAYLOAD_FIELDS
    else:
        raise ValueError(f"unsupported provider setting domain: {domain}")
    unknown = set(result) - allowed_fields
    if unknown:
        raise ValueError(
            "provider setting has unknown fields: " + ", ".join(sorted(unknown))
        )
    if "openaiOptions" in result:
        _validate_openai_options(
            result["openaiOptions"],
            f"provider_settings.{domain}.openaiOptions",
            wire_format=domain in {"insight_vlm", "insight_chat"},
        )
    if "translationMode" in result and result["translationMode"] not in {
        "batch",
        "single",
    }:
        raise ValueError("provider_settings.translation.translationMode is invalid")
    if "promptMode" in result and result["promptMode"] not in {
        "normal",
        "json",
        "paddleocr_vl",
    }:
        raise ValueError("provider_settings.ai_vision_ocr.promptMode is invalid")
    for key, value in result.items():
        if key == "openaiOptions":
            continue
        if key == "batchSize":
            _integer(
                value,
                f"provider_settings.{domain}.{key}",
                minimum=1,
            )
        elif key == "rpmLimit":
            _integer(
                value,
                f"provider_settings.{domain}.{key}",
                minimum=0,
                maximum=100_000,
            )
        elif key in {"transportRetries", "businessRetries"}:
            _integer(
                value,
                f"provider_settings.{domain}.{key}",
                minimum=0,
                maximum=100,
            )
        elif key == "analyzeConcurrency":
            _integer(
                value,
                f"provider_settings.{domain}.{key}",
                minimum=1,
                maximum=8,
            )
        elif key in {"minImageSize", "imageMaxSize"}:
            _integer(
                value,
                f"provider_settings.{domain}.{key}",
                minimum=0,
            )
        elif key == "timeoutSeconds":
            number = _finite_number(
                value,
                f"provider_settings.{domain}.{key}",
            )
            if number < 0:
                raise ValueError(
                    f"provider_settings.{domain}.{key} must be at least 0"
                )
        elif not isinstance(value, str):
            raise ValueError(f"provider_settings.{domain}.{key} must be a string")
    return result


def validate_credential_secret(
    domain: str,
    provider: str,
    secret: object,
) -> dict[str, Any]:
    result = _object(secret, "credential secret")
    if domain == "ocr" and provider == "baidu":
        allowed = {"baidu_api_key", "baidu_secret_key"}
    elif domain == "ai_vision_ocr":
        _require_provider(
            provider,
            VISION_OCR_CAPABILITY,
            "credential.ai_vision_ocr.provider",
        )
        allowed = {"ai_vision_api_key"}
    elif domain == "web_import_http" and provider == "headers":
        allowed = {"cookie", "headers"}
    elif domain == "web_import_firecrawl" and provider == "firecrawl":
        allowed = {"api_key"}
    elif domain == "custom_ai_profile":
        if not _UUID_ID.fullmatch(provider):
            raise ValueError("custom AI profile credential provider must be a UUID")
        allowed = {"api_key"}
    elif domain in PROVIDER_CAPABILITIES:
        _require_provider(
            provider,
            PROVIDER_CAPABILITIES[domain],
            f"credential.{domain}.provider",
        )
        allowed = {"api_key"}
    elif is_proofreading_provider_domain(domain):
        _require_provider(
            provider,
            HQ_TRANSLATION_CAPABILITY,
            f"credential.{domain}.provider",
        )
        allowed = {"api_key"}
    else:
        raise ValueError(f"unsupported credential domain/provider: {domain}/{provider}")
    if domain == "web_import_http":
        if set(result) - allowed:
            raise ValueError(
                "web import HTTP credential must contain cookie and/or headers"
            )
    elif set(result) != allowed:
        raise ValueError(
            "credential secret fields must be exactly: "
            + ", ".join(sorted(allowed))
        )
    if domain == "web_import_http":
        cookie = result.get("cookie")
        headers = result.get("headers")
        if cookie is not None and not isinstance(cookie, str):
            raise ValueError("web import HTTP credential is invalid")
        if headers is not None and (
            not isinstance(headers, Mapping)
            or any(
                not isinstance(key, str)
                or not key.strip()
                or not isinstance(value, str)
                or not value.strip()
                for key, value in headers.items()
            )
        ):
            raise ValueError("web import HTTP credential is invalid")
    elif any(
        not isinstance(value, str)
        for value in result.values()
    ):
        raise ValueError("credential secret values must be strings")
    return {key: value.strip() if isinstance(value, str) else value for key, value in result.items()}


def validate_book_setting_payload(
    domain: str,
    payload: object,
) -> dict[str, Any]:
    if domain != "insight":
        raise ValueError(f"unsupported book setting domain: {domain}")
    result = _object(payload, "book setting payload")
    _reject_secret_fields(result, f"book_settings.{domain}")
    _validate_insight(result)
    return result


def setting_storage_payload(domain: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep provider parameters in provider_settings only."""
    result = deepcopy(dict(payload))
    if domain == "web_import":
        result["agent"] = {
            k: v for k, v in result["agent"].items()
            if k not in {"modelName", "customBaseUrl"}
        }
    if domain == "translation":
        return result
    return result


def compose_editable_settings(
    domain: str, payload: object, providers: Mapping | None = None,
) -> dict[str, Any]:
    """Compose the UI document from stored selections and provider parameters."""
    result = deepcopy(_object(payload, "setting payload"))
    providers = providers or {}
    if domain == "web_import":
        agent = _object(result["agent"], "web_import.agent")
        _exact_keys(
            agent,
            set(DEFAULT_WEB_IMPORT_SETTINGS["agent"]) - {"modelName", "customBaseUrl"},
            "web_import.agent",
        )
        result["agent"] = {
            **{key: DEFAULT_WEB_IMPORT_SETTINGS["agent"][key]
               for key in ("modelName", "customBaseUrl")},
            **agent,
            **deepcopy(dict(providers.get(("web_import_agent", agent["provider"]), {}))),
        }
    if domain == "translation":
        return validate_setting_payload(domain, result)
    if domain == "text_style_defaults":
        return validate_setting_payload(domain, result)
    return result
