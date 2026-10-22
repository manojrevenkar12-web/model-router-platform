"""Real providers through LiteLLM: closed APIs and self-hosted open-weight models (vLLM) alike.

Errors are classified once, here, so the router's fallback logic sees only "retryable or not".
Customers' own keys (bring your own key) are passed per call and never stored on the client.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import litellm

from mrp.providers.base import Completion, Delta, ProviderError

RETRYABLE = (
    litellm.exceptions.RateLimitError,
    litellm.exceptions.ServiceUnavailableError,
    litellm.exceptions.Timeout,
    litellm.exceptions.APIConnectionError,
    litellm.exceptions.InternalServerError,
)


class LiteLLMProvider:
    def __init__(
        self,
        name: str,
        api_base: str | None = None,
        default_key: str | None = None,
        timeout: float = 60.0,
        **extra: Any,
    ):
        self.name, self.api_base, self.default_key, self.timeout, self.extra = (
            name,
            api_base,
            default_key,
            timeout,
            extra,
        )

    def _kwargs(
        self, upstream: str, messages: list[dict], max_tokens: int, request_id: str, api_key: str | None
    ) -> dict:
        kw: dict[str, Any] = {
            "model": upstream,
            "messages": messages,
            "max_tokens": max_tokens,
            "timeout": self.timeout,
            "api_key": api_key or self.default_key,
            "metadata": {"mrp_request_id": request_id},
            **self.extra,
        }
        if self.api_base:
            kw["api_base"] = self.api_base
        return kw

    @staticmethod
    def _error(exc: Exception) -> ProviderError:
        if isinstance(exc, RETRYABLE):
            return ProviderError(f"{type(exc).__name__}", retryable=True)
        return ProviderError(f"{type(exc).__name__}: {str(exc)[:200]}", retryable=False)

    async def complete(self, upstream, messages, max_tokens, request_id, api_key=None) -> Completion:
        try:
            r = await litellm.acompletion(**self._kwargs(upstream, messages, max_tokens, request_id, api_key))
        except Exception as exc:  # noqa: BLE001 - every provider error is classified, none escapes raw
            raise self._error(exc) from exc
        usage = r.usage
        return Completion(
            r.choices[0].message.content or "",
            int(usage.prompt_tokens),
            int(usage.completion_tokens),
            r.id or request_id,
            r.choices[0].finish_reason or "stop",
        )

    async def stream(
        self, upstream, messages, max_tokens, request_id, api_key=None
    ) -> AsyncIterator[Delta | Completion]:
        kw = self._kwargs(upstream, messages, max_tokens, request_id, api_key)
        kw.update(stream=True, stream_options={"include_usage": True})
        try:
            resp = await litellm.acompletion(**kw)
        except Exception as exc:  # noqa: BLE001
            raise self._error(exc) from exc
        tin = tout = 0
        rid = request_id
        async for chunk in resp:
            rid = getattr(chunk, "id", None) or rid
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                yield Delta(chunk.choices[0].delta.content)
            usage = getattr(chunk, "usage", None)
            if usage:
                tin, tout = int(usage.prompt_tokens or 0), int(usage.completion_tokens or 0)
        yield Completion("", tin, tout, rid)

    async def usage_for(self, request_id: str) -> tuple[int, int] | None:
        return None  # closed APIs do not offer per-request lookups; invoices are reconciled per day
