"""Simulated models, for evaluations, chaos tests and the demo.

Each simulated model has a true pass rate per task that the router never sees; it only sees what
the evaluations measure. Whether a model answers a given item correctly is a deterministic function
of (model, item), so repeated calls agree, as they would at temperature 0.

When an answer is wrong, it is sometimes visibly wrong (malformed JSON, a label outside the allowed
set, code that does not compile) and sometimes plausibly wrong. The share that is visibly wrong is
the cheap verifier's catch rate; the router has to measure it, not assume it.

The simulator keeps its own record of what it charged per request, which is the ground truth the
billing tests reconcile the ledger against. Faults can be injected: errors before or after a charge,
outages per model, and streams cut off by the client.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from mrp.providers.base import Completion, Delta, ProviderError

TASKS = ["extraction", "classification", "code", "summarization", "reasoning", "chat"]

# True pass rates per task: hidden from the router. Columns follow TASKS.
TRUE_QUALITY: dict[str, list[float]] = {
    "frontier-large": [0.985, 0.975, 0.96, 0.97, 0.95, 0.975],
    "frontier-mid": [0.975, 0.965, 0.94, 0.96, 0.91, 0.965],
    "general-b": [0.965, 0.955, 0.92, 0.95, 0.88, 0.955],
    "fast-small": [0.93, 0.94, 0.84, 0.92, 0.74, 0.93],
    "general-b-mini": [0.92, 0.935, 0.82, 0.91, 0.70, 0.925],
    "open-70b": [0.935, 0.93, 0.85, 0.915, 0.78, 0.93],
    "open-8b": [0.80, 0.88, 0.62, 0.84, 0.48, 0.86],
    "open-8b-extract": [0.97, 0.86, 0.50, 0.70, 0.40, 0.70],  # fine-tuned for extraction
}
# Share of wrong answers that a cheap check can see (invalid JSON, unknown label, syntax error).
VISIBLE_WRONG = {"extraction": 0.75, "classification": 0.6, "code": 0.55}
FALSE_ALARM = 0.01  # correct answers that still fail the check
OUT_TOKENS = {
    "extraction": 60,
    "classification": 4,
    "code": 120,
    "summarization": 160,
    "reasoning": 420,
    "chat": 140,
}
LATENCY_MS = {
    "frontier-large": 1400,
    "frontier-mid": 700,
    "general-b": 650,
    "fast-small": 280,
    "general-b-mini": 260,
    "open-70b": 420,
    "open-8b": 120,
    "open-8b-extract": 120,
}

_TAG = re.compile(r"⟦task:(\w+)\|item:(\w+)⟧")


def _u(*parts: str) -> float:
    h = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


def sim_item(task: str, item: str) -> str:
    """The hidden tag the evaluation adds to a request so the simulator knows what it is answering."""
    return f"⟦task:{task}|item:{item}⟧"


def outcome(model: str, task: str, item: str) -> tuple[bool, bool]:
    """(correct, passes the cheap check) for this model on this item."""
    q = TRUE_QUALITY[model][TASKS.index(task)]
    correct = _u(model, item, "correct") < q
    if task not in VISIBLE_WRONG:
        return correct, True
    if correct:
        return True, _u(model, item, "alarm") >= FALSE_ALARM
    return False, _u(model, item, "visible") >= VISIBLE_WRONG[task]


def render(task: str, correct: bool, passes: bool, item: str) -> str:
    if task == "extraction":
        if not passes:
            return '{"invoice_id": "INV-' + item + '", "amount": '  # truncated JSON
        return json.dumps(
            {"invoice_id": f"INV-{item}", "amount": 120.5 if correct else 102.5, "currency": "EUR"}
        )
    if task == "classification":
        return ("positive" if correct else "negative") if passes else "probably positive?"
    if task == "code":
        return (
            "def total(xs):\n    return sum(xs)\n"
            if passes and correct
            else ("def total(xs):\n    return len(xs)\n" if passes else "def total(xs)\n    return sum(xs\n")
        )
    words = {"summarization": 120, "reasoning": 300, "chat": 100}[task]
    return ("A consistent answer. " if correct else "A plausible but wrong answer. ") * (words // 4)


@dataclass
class SimProvider:
    name: str = "sim"
    seed: int = 0
    fail_before: float = 0.0  # retryable error, nothing charged
    fail_after_charge: float = 0.0  # retryable error after the provider has billed the call
    latency_scale: float = 0.0  # 0 = no sleeping (evaluations); 1 = realistic latency
    down: set[str] = field(default_factory=set)
    charges: dict[str, tuple[str, int, int]] = field(default_factory=dict)  # request id -> (model, in, out)
    calls: int = 0
    faults: Counter = field(default_factory=Counter)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    @staticmethod
    def _parse(messages: list[dict]) -> tuple[str, str]:
        text = " ".join(str(m.get("content", "")) for m in messages)
        m = _TAG.search(text)
        if m:
            return m.group(1), m.group(2)
        from mrp.router import classify  # untagged traffic (demo, tests): answer the task it looks like

        return classify(messages), hashlib.sha256(text.encode()).hexdigest()[:12]

    def _model(self, upstream: str) -> str:
        """Simulated models answer to the catalogue's upstream names as well as their own ids."""
        if upstream in TRUE_QUALITY:
            return upstream
        from mrp.catalog import load

        by_upstream = {m.upstream: m.id for m in load().models.values()}
        return by_upstream.get(upstream, upstream)

    async def _latency(self, model: str) -> None:
        if self.latency_scale:
            await asyncio.sleep(LATENCY_MS.get(model, 300) / 1000 * self.latency_scale)

    def _maybe_fail(self, model: str, request_id: str, tin: int, tout: int) -> None:
        if model in self.down:
            raise ProviderError(f"{model} unavailable", retryable=True)
        r = self.rng.random()
        if r < self.fail_before:
            self.faults["provider_failed_before_charge"] += 1
            raise ProviderError("upstream 503", retryable=True)
        if r < self.fail_before + self.fail_after_charge:
            self.faults["provider_failed_after_charge"] += 1
            self.charges[request_id] = (model, tin, tout)
            raise ProviderError("upstream timeout after completion", retryable=True, charged=(tin, tout))

    async def complete(self, upstream, messages, max_tokens, request_id, api_key=None) -> Completion:
        self.calls += 1
        model = self._model(upstream)
        task, item = self._parse(messages)
        tin = sum(max(1, len(str(m.get("content", ""))) // 4) + 4 for m in messages)
        tout = min(max_tokens, OUT_TOKENS.get(task, 100))
        await self._latency(model)
        self._maybe_fail(model, request_id, tin, tout)
        correct, passes = outcome(model, task, item)
        self.charges[request_id] = (model, tin, tout)
        return Completion(
            render(task, correct, passes, item),
            tin,
            tout,
            request_id,
            meta={"task": task, "item": item, "correct": correct},
        )

    async def stream(
        self, upstream, messages, max_tokens, request_id, api_key=None
    ) -> AsyncIterator[Delta | Completion]:
        self.calls += 1
        model = self._model(upstream)
        task, item = self._parse(messages)
        tin = sum(max(1, len(str(m.get("content", ""))) // 4) + 4 for m in messages)
        if model in self.down or self.rng.random() < self.fail_before:
            raise ProviderError(f"{model} unavailable", retryable=True)
        correct, passes = outcome(model, task, item)
        text = render(task, correct, passes, item)
        words = text.split(" ")
        sent = 0
        try:
            for i in range(0, len(words), 4):
                chunk = " ".join(words[i : i + 4]) + (" " if i + 4 < len(words) else "")
                sent += max(1, len(chunk) // 4)
                if self.latency_scale:
                    await asyncio.sleep(0.01 * self.latency_scale)
                yield Delta(chunk)
            tout = min(max_tokens, max(sent, OUT_TOKENS.get(task, 100)))
            self.charges[request_id] = (model, tin, tout)
            yield Completion("", tin, tout, request_id, meta={"task": task, "item": item, "correct": correct})
        finally:
            if request_id not in self.charges:  # the client went away: tokens generated so far are billed
                self.charges[request_id] = (model, tin, sent)

    async def usage_for(self, request_id: str) -> tuple[int, int] | None:
        c = self.charges.get(request_id)
        return (c[1], c[2]) if c else None
