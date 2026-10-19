"""Legacy browser settings document for translation-domain tests.

The v2 factory now stores an empty placeholder because production translation
settings were decoupled from the database, but the strict frontend parser still
hydrates a complete document.  Tests that assert validation and locking rules
for such a payload build it from the pre-decoupling factory defaults.
"""

from __future__ import annotations

from src.shared.constants import (
    BATCH_TRANSLATE_JSON_SYSTEM_TEMPLATE,
    BATCH_TRANSLATE_SYSTEM_TEMPLATE,
    DEFAULT_AI_VISION_OCR_PROMPT,
    DEFAULT_HQ_TRANSLATE_PROMPT,
    DEFAULT_PROMPT,
    DEFAULT_TRANSLATE_JSON_PROMPT,
)


def legacy_translation_settings() -> dict[str, object]:
    """Return the complete current browser settings document.

    The v2 database is the first source of truth, so a fresh database must
    contain a document the strict frontend parser can hydrate directly.
    """

    def openai_options(
        *,
        use_stream: bool,
        rpm_limit: int,
        transport_retries: int,
        business_retries: int,
    ) -> dict[str, object]:
        return {
            "request": {"forceJsonOutput": False},
            "execution": {
                "useStream": use_stream,
                "rpmLimit": rpm_limit,
                "transportRetries": transport_retries,
                "businessRetries": business_retries,
            },
        }

    return {
        "ocrEngine": "manga_ocr",
        "textDetector": "default",
        "minTextBlockAreaPercent": 0.05,
        "enableAuxYoloDetection": False,
        "auxYoloConfThreshold": 0.4,
        "auxYoloOverlapThreshold": 0.1,
        "enableSaberYoloRefine": True,
        "saberYoloRefineOverlapThreshold": 50.0,
        "baiduOcr": {
            "version": "standard",
            "sourceLanguage": "JAP",
        },
        "paddleOcrVl": {"sourceLanguage": "japanese"},
        "aiVisionOcr": {
            "provider": "gemini",
            "modelName": "",
            "prompt": DEFAULT_AI_VISION_OCR_PROMPT,
            "promptMode": "normal",
            "customBaseUrl": "",
            "openaiOptions": openai_options(
                use_stream=False,
                rpm_limit=0,
                transport_retries=3,
                business_retries=3,
            ),
            "minImageSize": 32,
        },
        "hybridOcr": {
            "enabled": False,
            "secondaryEngine": "48px_ocr",
            "confidenceThreshold": 0.2,
        },
        "translation": {
            "provider": "siliconflow",
            "modelName": "",
            "customBaseUrl": "",
            "openaiOptions": openai_options(
                use_stream=True,
                rpm_limit=0,
                transport_retries=3,
                business_retries=3,
            ),
            "translationMode": "batch",
            "batchNormalPrompt": BATCH_TRANSLATE_SYSTEM_TEMPLATE,
            "batchJsonPrompt": BATCH_TRANSLATE_JSON_SYSTEM_TEMPLATE,
            "singleNormalPrompt": DEFAULT_PROMPT,
            "singleJsonPrompt": DEFAULT_TRANSLATE_JSON_PROMPT,
        },
        "targetLanguage": "zh",
        "translatePrompt": BATCH_TRANSLATE_SYSTEM_TEMPLATE,
        "useTextboxPrompt": False,
        "textboxPrompt": "",
        "hqTranslation": {
            "provider": "siliconflow",
            "modelName": "",
            "customBaseUrl": "",
            "openaiOptions": openai_options(
                use_stream=True,
                rpm_limit=7,
                transport_retries=3,
                business_retries=3,
            ),
            "batchSize": 3,
            "prompt": DEFAULT_HQ_TRANSLATE_PROMPT,
        },
        "pluginAgent": {
            "provider": "siliconflow",
            "modelName": "",
            "customBaseUrl": "",
            "openaiOptions": openai_options(
                use_stream=True,
                rpm_limit=0,
                transport_retries=3,
                business_retries=3,
            ),
        },
        "proofreading": {
            "enabled": False,
            "rounds": [],
        },
        "boxExpand": {
            "ratio": 2.0,
            "top": 0.0,
            "bottom": 0.0,
            "left": 0.0,
            "right": 0.0,
        },
        "preciseMask": {"dilateSize": 10, "boxExpandRatio": 20.0},
        "showDetectionDebug": False,
        "parallel": {"enabled": False, "deepLearningLockSize": 1},
        "removeTextWithOcr": False,
        "compressVisionImages": True,
        "lamaDisableResize": False,
        "lamaRegionalInpainting": False,
    }
