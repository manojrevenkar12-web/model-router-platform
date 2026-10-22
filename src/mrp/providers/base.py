"""The provider interface: one shape for closed APIs, self-hosted open-weight models and simulators."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Completion:
    text: str
    tokens_in: int
    tokens_out: int
    provider_request_id: str
    finish_reason: str = "stop"
    meta: dict = field(default_factory=dict)  # simulators put ground truth here for evaluations


@dataclass
class Delta:
    text: str


class ProviderError(Exception):
    """`retryable`: another attempt or provider may succeed (429, 5xx, timeouts).
    `charged`: the provider billed tokens despite failing (tokens_in, tokens_out), if known."""

    def __init__(self, message: str, retryable: bool, charged: tuple[int, int] | None = None):
        super().__init__(message)
        self.retryable, self.charged = retryable, charged


class Provider(Protocol):
    name: str

    async def complete(
        self,
        upstream: str,
        messages: list[dict],
        max_tokens: int,
        request_id: str,
        api_key: str | None = None,
    ) -> Completion: ...

    def stream(
        self,
        upstream: str,
        messages: list[dict],
        max_tokens: int,
        request_id: str,
        api_key: str | None = None,
    ) -> AsyncIterator[Delta | Completion]: ...

    async def usage_for(self, request_id: str) -> tuple[int, int] | None:
        """Tokens the provider billed for one request, where the provider can tell us (None if not)."""
        ...
