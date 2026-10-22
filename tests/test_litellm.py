"""The LiteLLM adapter, exercised with LiteLLM's built-in mock responses (no network)."""

import litellm
import pytest

from mrp.providers.base import Completion, Delta, ProviderError
from mrp.providers.litellm_provider import LiteLLMProvider


async def test_completion_maps_usage_and_ids():
    p = LiteLLMProvider("closed-b", mock_response="Hello from the mock")
    c = await p.complete(
        "openai/gpt-4.1-mini", [{"role": "user", "content": "hi"}], 50, "mrp-1", api_key="sk-test"
    )
    assert c.text == "Hello from the mock" and c.tokens_in > 0 and c.tokens_out > 0 and c.provider_request_id


async def test_streaming_yields_deltas_then_usage():
    p = LiteLLMProvider("closed-b", mock_response="one two three four")
    parts = [
        x
        async for x in p.stream(
            "openai/gpt-4.1-mini", [{"role": "user", "content": "hi"}], 50, "mrp-2", api_key="sk-test"
        )
    ]
    assert any(isinstance(x, Delta) for x in parts) and isinstance(parts[-1], Completion)
    assert "".join(x.text for x in parts if isinstance(x, Delta)) == "one two three four"


@pytest.mark.parametrize(
    "exc,retryable",
    [
        (litellm.exceptions.RateLimitError("slow down", llm_provider="openai", model="m"), True),
        (litellm.exceptions.ServiceUnavailableError("down", llm_provider="openai", model="m"), True),
        (litellm.exceptions.BadRequestError("bad", llm_provider="openai", model="m"), False),
        (litellm.exceptions.AuthenticationError("no", llm_provider="openai", model="m"), False),
    ],
)
async def test_errors_are_classified(exc, retryable, monkeypatch):
    async def boom(**_):
        raise exc

    monkeypatch.setattr(litellm, "acompletion", boom)
    with pytest.raises(ProviderError) as e:
        await LiteLLMProvider("closed-b").complete("openai/gpt-4.1-mini", [], 10, "r")
    assert e.value.retryable is retryable
