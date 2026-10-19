from __future__ import annotations

import pytest

from src.shared.ai_providers import TRANSLATION_CAPABILITY
from src.shared.ai_transport import UnifiedChatRequest
from src.shared.openai_execution import (
    OpenAICompatibleBusinessRetryableError,
    OpenAICompatibleSyncExecutor,
)
from src.shared.openai_options import (
    OpenAICompatibleExecutionOptions,
    OpenAICompatibleOptions,
)


def test_shared_business_retry_does_not_hide_nested_memory_failure() -> None:
    class FakeTransport:
        calls = 0

        def complete(self, _request, **_kwargs):
            self.calls += 1
            return "response"

    transport = FakeTransport()
    executor = OpenAICompatibleSyncExecutor(transport)

    def parse(_content: str):
        try:
            raise MemoryError("native allocation failed")
        except MemoryError as cause:
            raise OpenAICompatibleBusinessRetryableError(
                "解析失败"
            ) from cause

    request = UnifiedChatRequest(
        provider="deepseek",
        api_key="key",
        model="model",
        messages=[{"role": "user", "content": "hello"}],
        openai_options=OpenAICompatibleOptions(
            execution=OpenAICompatibleExecutionOptions(business_retries=3)
        ),
    )
    with pytest.raises(OpenAICompatibleBusinessRetryableError, match="解析失败"):
        executor.execute(
            request,
            capability=TRANSLATION_CAPABILITY,
            parser=parse,
        )
    assert transport.calls == 1