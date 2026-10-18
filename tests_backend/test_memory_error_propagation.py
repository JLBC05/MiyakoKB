from __future__ import annotations

from unittest import mock

import pytest

from src.core import rendering
from src.core.manga_insight import vlm_client
from src.shared.memory_errors import is_memory_allocation_error


@pytest.mark.parametrize(
    "error",
    [
        MemoryError(),
        RuntimeError("CUDA out of memory"),
        RuntimeError("std::bad_alloc: bad allocation"),
        RuntimeError("OpenCV: Insufficient memory: Failed to allocate 4096 bytes"),
    ],
)
def test_memory_allocation_error_classifier(error: BaseException) -> None:
    assert is_memory_allocation_error(error)


def test_non_memory_model_error_is_not_classified_as_allocation_failure() -> None:
    assert not is_memory_allocation_error(RuntimeError("invalid model output"))


def test_rendering_metric_allocation_failure_is_not_converted_to_zero_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rendering.np,
        "array",
        mock.Mock(side_effect=MemoryError("native allocation failed")),
    )

    with pytest.raises(MemoryError, match="allocation failed"):
        rendering.get_char_ink_offset("A", rendering.ImageFont.load_default())


def test_insight_image_resize_does_not_fallback_after_memory_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        vlm_client.Image,
        "open",
        mock.Mock(side_effect=RuntimeError("OpenCV: Insufficient memory")),
    )

    with pytest.raises(RuntimeError, match="Insufficient memory"):
        vlm_client._prepare_image(b"image", 1024)
