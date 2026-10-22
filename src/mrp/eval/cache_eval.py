"""Semantic cache: how many repeats does it catch, and how often would it serve a wrong answer?

Pairs of prompts:
- *repeats*: the same question asked again with different casing, punctuation, filler words or
  word order. Serving the cached answer is right.
- *near misses*: the same wording with one thing changed (an amount, a country, a product, a date
  or a negation). Serving the cached answer is wrong.

For each similarity threshold: the share of repeats served from cache and the share of near misses
served (false hits), with and without the slot check.
"""

from __future__ import annotations

import random

import numpy as np

from mrp.cache import HashingEmbedder, slots

TEMPLATES = [
    ("What is the refund limit for orders above {amount} EUR in {country}?", "amount", "country"),
    ("How many days does delivery to {country} take for {product}?", "country", "product"),
    ("Is {product} available in {country} before {date}?", "product", "date"),
    ("Can customers in {country} pay with {method} for orders under {amount} EUR?", "method", "amount"),
    ("What is the fee for transferring {amount} EUR to {country}?", "amount", "country"),
    ("Does the {product} plan include {feature}?", "product", "feature"),
    ("Which documents does a business in {country} need to open an account?", "country", None),
    ("How do I cancel the {product} subscription on {date}?", "product", "date"),
]
VALUES = {
    "amount": ["50", "120", "500", "900", "1500", "2000"],
    "country": ["Germany", "France", "Austria", "Spain", "Italy", "Poland"],
    "product": ["Basic", "Pro", "Team", "Enterprise", "Starter"],
    "date": ["1 March", "15 April", "30 June", "1 October"],
    "method": ["SEPA direct debit", "credit card", "PayPal", "invoice"],
    "feature": ["SSO", "audit logs", "priority support", "API access"],
}
FILLERS = ["please", "quick question:", "hi,", "could you tell me", "I'd like to know"]


def fill(t: str, rng: random.Random) -> tuple[str, dict]:
    vals = {k: rng.choice(v) for k, v in VALUES.items()}
    return t.format(**vals), vals


def repeat(text: str, rng: random.Random) -> str:
    variants = [
        lambda s: s.lower(),
        lambda s: s.rstrip("?") + " ?",
        lambda s: f"{rng.choice(FILLERS)} {s[0].lower()}{s[1:]}",
        lambda s: s.replace("What is", "what's").replace("How many", "how many"),
        lambda s: s + " Thanks!",
        lambda s: s.upper(),
    ]
    out = text
    for f in rng.sample(variants, 2):
        out = f(out)
    return out


def near_miss(template: str, vals: dict, slot: str, rng: random.Random) -> str:
    other = dict(vals)
    if slot == "negation":
        return (
            template.format(**vals)
            .replace("Can ", "Can't ")
            .replace("Is ", "Isn't ")
            .replace("Does ", "Doesn't ")
            if any(template.startswith(w) for w in ("Can ", "Is ", "Does "))
            else template.format(**vals) + " Not for business accounts."
        )
    other[slot] = rng.choice([v for v in VALUES[slot] if v != vals[slot]])
    return template.format(**other)


def pairs(n: int, seed: int) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    rng = random.Random(seed)
    reps, misses = [], []
    for _ in range(n):
        t, a, b = rng.choice(TEMPLATES)
        text, vals = fill(t, rng)
        reps.append((text, repeat(text, rng)))
        slot = rng.choice([s for s in (a, b, "negation") if s])
        misses.append((text, near_miss(t, vals, slot, rng)))
    return reps, misses


def run(n: int = 600, seed: int = 9) -> dict:
    emb = HashingEmbedder()
    reps, misses = pairs(n, seed)

    def sims(ps):
        return [float(np.dot(emb.embed(a), emb.embed(b))) for a, b in ps]

    rs, ms = sims(reps), sims(misses)
    rep_slot_ok = [slots(a) == slots(b) for a, b in reps]
    miss_slot_ok = [slots(a) == slots(b) for a, b in misses]
    rows = []
    for th in (0.80, 0.85, 0.88, 0.90, 0.92, 0.94, 0.96):
        rows.append(
            {
                "threshold": th,
                "repeats_served": round(sum(s >= th for s in rs) / n, 4),
                "near_misses_served": round(sum(s >= th for s in ms) / n, 4),
                "repeats_served_with_slot_check": round(
                    sum(s >= th and ok for s, ok in zip(rs, rep_slot_ok, strict=True)) / n, 4
                ),
                "near_misses_served_with_slot_check": round(
                    sum(s >= th and ok for s, ok in zip(ms, miss_slot_ok, strict=True)) / n, 4
                ),
            }
        )
    return {
        "pairs": n,
        "embedder": "character n-gram hashing (no model download); production uses a sentence-embedding model",
        "median_similarity": {
            "repeats": round(float(np.median(rs)), 3),
            "near_misses": round(float(np.median(ms)), 3),
        },
        "by_threshold": rows,
    }
