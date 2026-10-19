"""Strict current OCR result models shared by Worker algorithms and rendering."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional


OCR_RESULT_FIELDS = frozenset(
    {
        "text",
        "confidence",
        "confidenceSupported",
        "engine",
        "primaryEngine",
        "fallbackUsed",
    }
)


def _confidence(value: object, *, supported: bool) -> Optional[float]:
    if value is None:
        if supported:
            raise ValueError("supported OCR confidence cannot be null")
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise ValueError("OCR confidence must be between zero and one")
    if not supported:
        raise ValueError("unsupported OCR confidence must be null")
    return float(value)


def _non_empty_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


@dataclass
class OcrResult:
    text: str
    confidence: Optional[float]
    confidence_supported: bool
    engine: str
    primary_engine: str
    fallback_used: bool

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("OCR text must be a string")
        if not isinstance(self.confidence_supported, bool):
            raise TypeError("OCR confidenceSupported must be boolean")
        if not isinstance(self.fallback_used, bool):
            raise TypeError("OCR fallbackUsed must be boolean")
        self.confidence = _confidence(
            self.confidence,
            supported=self.confidence_supported,
        )
        self.engine = _non_empty_string(self.engine, field_name="OCR engine")
        self.primary_engine = _non_empty_string(
            self.primary_engine,
            field_name="OCR primaryEngine",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "confidenceSupported": self.confidence_supported,
            "engine": self.engine,
            "primaryEngine": self.primary_engine,
            "fallbackUsed": self.fallback_used,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OcrResult":
        if not isinstance(data, Mapping) or set(data) != OCR_RESULT_FIELDS:
            raise ValueError("OCR result does not match the current schema")
        return cls(
            text=data["text"],
            confidence=data["confidence"],
            confidence_supported=data["confidenceSupported"],
            engine=data["engine"],
            primary_engine=data["primaryEngine"],
            fallback_used=data["fallbackUsed"],
        )
