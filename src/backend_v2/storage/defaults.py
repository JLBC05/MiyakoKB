"""Factory-owned v2 settings and prompt defaults."""

from __future__ import annotations

from src.shared.constants import (
    DEFAULT_WEB_IMPORT_EXTRACTION_PROMPT,
)


DEFAULT_FONT_ID = "00000000-0000-0000-0000-000000000010"

DEFAULT_TEXT_STYLE: dict[str, object] = {
    "fontSize": 26,
    "autoFontSize": True,
    "fontFamily": DEFAULT_FONT_ID,
    "layoutDirection": "auto",
    "textColor": "#000000",
    "fillColor": "#FFFFFF",
    "inpaintMethod": "lama_manga",
    "useAutoTextColor": False,
    "strokeEnabled": True,
    "strokeColor": "#FFFFFF",
    "strokeWidth": 3,
    "lineSpacing": 1.0,
    "inlineAlign": "start",
    "blockAlign": "start",
}

DEFAULT_WORKFLOW_PREFERENCES: dict[str, object] = {
    "rememberWorkflowModeEnabled": False,
    "lastWorkflowMode": "translate-current",
}

DEFAULT_EXPORT_PREFERENCES: dict[str, object] = {
    "preserveOriginalFilenames": False,
}

DEFAULT_CUSTOM_AI_PROFILES: dict[str, object] = {
    "profiles": [],
}

DEFAULT_BROWSER_DOM_AGENT = {
    "provider": "siliconflow",
    "modelName": "",
    "customBaseUrl": "",
    "openaiOptions": {
        "request": {"forceJsonOutput": True},
        "execution": {
            "useStream": False, "rpmLimit": 0,
            "transportRetries": 3, "businessRetries": 3,
        },
    },
}

DEFAULT_WEB_IMPORT_SETTINGS: dict[str, object] = {
    "firecrawl": {},
    "agent": {
        "provider": "openai",
        "customBaseUrl": "",
        "modelName": "gpt-4o-mini",
        "useStream": False,
        "forceJsonOutput": True,
        "maxRetries": 3,
        "timeout": 120,
    },
    "extraction": {
        "prompt": DEFAULT_WEB_IMPORT_EXTRACTION_PROMPT,
        "maxIterations": 10,
    },
    "download": {
        "concurrency": 3,
        "timeout": 30,
        "retries": 3,
        "delay": 100,
        "useReferer": True,
    },
    "imagePreprocess": {
        "enabled": False,
        "autoRotate": True,
        "compression": {
            "enabled": False,
            "quality": 85,
            "maxWidth": 0,
            "maxHeight": 0,
        },
        "formatConvert": {
            "enabled": False,
            "targetFormat": "original",
        },
    },
    "advanced": {"bypassProxy": False},
    "ui": {
        "showAgentLogs": True,
        "autoImport": False,
    },
}

DEFAULT_INSIGHT_SETTINGS: dict[str, object] = {
    "analysis": {
        "batch": {
            "pagesPerBatch": 5,
            "contextBatchCount": 3,
            "architecturePreset": "standard",
            "customLayers": [],
            "knowledgeMode": False,
            "extraEntities": ["characters", "locations", "abilities", "items", "concepts"],
            "slugMode": "pinyin",
        }
    },
    "vlm": {"provider": "gemini"},
    "chat": {"provider": "gemini", "useSameAsVlm": False},
    "embedding": {"provider": "openai"},
    "reranker": {"provider": "jina"},
    "imageGen": {"provider": "gpt2api"},
}

FACTORY_PROMPTS: dict[str, str] = {
    "batch_analysis": (
        "分析漫画页面，输出符合给定 schema 的摘要、关键事件、连续性与警告。"
    ),
    "segment_summary": (
        "汇总给定页面段落，保留事件因果、角色状态变化与未解决线索。"
    ),
    "chapter_summary": (
        "汇总章节内容，保持事件顺序，并指出关键转折与连续性信息。"
    ),
    "book_overview": (
        "基于已发布分析生成全书概览，不添加输入中没有依据的事实。"
    ),
    "group_summary": (
        "将下级分析结果归并为结构化摘要，保留来源覆盖范围和警告。"
    ),
    "qa_response": (
        "你是一个严谨的漫画分析助手。请只依据检索到的漫画分析上下文和资料回答问题；证据不足时明确说明。\n\n"
        "要求：\n"
        "1. 结论必须严格基于提供的资料，区分事实与推断。\n"
        "2. 如果资料中没有提及，请明确回答不知道或资料不足，切勿编造。\n"
        "3. 保持客观、准确、精炼。\n"
        "4. 引用具体页码或章节时，确保信息与资料一致。\n"
        "5. 如果资料中包含 [实体知识库数据]，对于统计类问题（第几页、第几次、最多、最早等），优先使用实体数据中的页码和频次直接回答，不要仅依赖页面摘要推测。"
    ),
    "analysis_system": (
        "你是漫画分析助手。所有结论必须来自当前分析资料，区分事实与推断。"
    ),
}


def default_translation_settings() -> dict[str, object]:
    """Return empty placeholder for decoupled translation settings."""
    return {}

