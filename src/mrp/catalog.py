"""The model catalogue, prices, plans and token estimates."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from functools import cache
from importlib import resources
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Model:
    id: str
    provider: str
    upstream: str
    price_in: Decimal  # EUR per million tokens
    price_out: Decimal
    context: int
    open_weight: bool
    fine_tuned_for: str = ""

    def cost(self, tokens_in: int, tokens_out: int) -> Decimal:
        return (self.price_in * tokens_in + self.price_out * tokens_out) / Decimal(1_000_000)


@dataclass(frozen=True)
class Plan:
    name: str
    monthly_tokens: int
    max_request_cost: Decimal
    markup: Decimal
    features: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Catalog:
    models: dict[str, Model]
    quality_targets: dict[str, float]
    plans: dict[str, Plan]
    cached_answer_charge: Decimal

    def model(self, model_id: str) -> Model:
        if model_id not in self.models:
            raise KeyError(f"unknown model '{model_id}'")
        return self.models[model_id]


@cache
def load(path: str | None = None) -> Catalog:
    text = (
        Path(path).read_text() if path else resources.files("mrp.config").joinpath("models.yaml").read_text()
    )
    raw = yaml.safe_load(text)
    models = {
        k: Model(
            k,
            v["provider"],
            v["upstream"],
            Decimal(str(v["price_in"])),
            Decimal(str(v["price_out"])),
            int(v["context"]),
            bool(v["open_weight"]),
            v.get("fine_tuned_for", ""),
        )
        for k, v in raw["models"].items()
    }
    plans = {
        k: Plan(
            k,
            int(v["monthly_tokens"]),
            Decimal(str(v["max_request_cost"])),
            Decimal(str(v["markup"])),
            frozenset(v["features"]),
        )
        for k, v in raw["plans"].items()
    }
    return Catalog(models, raw["quality_targets"], plans, Decimal(str(raw["cache"]["cached_answer_charge"])))


def estimate_tokens(text: str) -> int:
    """About four characters per token for English; used only for estimates, never for billing."""
    return max(1, math.ceil(len(text) / 4))


def messages_tokens(messages: list[dict]) -> int:
    return sum(estimate_tokens(str(m.get("content", ""))) + 4 for m in messages)
