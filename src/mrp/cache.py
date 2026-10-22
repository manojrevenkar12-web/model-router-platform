"""Cost optimisation: exact and semantic caching, and history compression.

Semantic caching is where cost savings can quietly become wrong answers: "What is the refund
limit for EU customers?" and "... for US customers?" embed almost identically. So a semantic hit is
served only if the two prompts carry the same *slots*: numbers, dates, amounts, quoted strings,
capitalised names and negations. The evaluation measures hits and false hits with and without the
slot check.

Savings from a cache hit are recorded as an estimate (what the call would probably have cost),
never as spend. Only actual provider costs are spend.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import asyncpg
import numpy as np
from redis.asyncio import Redis
from redis.exceptions import RedisError

from mrp.catalog import estimate_tokens

DIM = 256


class Embedder(Protocol):
    def embed(self, text: str) -> np.ndarray: ...


class HashingEmbedder:
    """Character n-gram feature hashing: no model download, deterministic, good at near-duplicates
    and poor at real paraphrase. Production swaps in a sentence-embedding model behind the same
    interface (same dimension, or a migration of the vector column)."""

    def __init__(self, dim: int = DIM):
        self.dim = dim

    def embed(self, text: str) -> np.ndarray:
        t = " " + normalise(text) + " "
        v = np.zeros(self.dim, dtype=np.float32)
        for n in (3, 4, 5):
            for i in range(len(t) - n + 1):
                h = int.from_bytes(hashlib.blake2b(t[i : i + n].encode(), digest_size=8).digest(), "big")
                v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        for w in t.split():  # whole words too, so word order changes matter less than word changes
            h = int.from_bytes(hashlib.blake2b(("w:" + w).encode(), digest_size=8).digest(), "big")
            v[h % self.dim] += 2.0 if (h >> 63) & 1 else -2.0
        norm = np.linalg.norm(v)
        return v / norm if norm else v


def normalise(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).lower()
    t = re.sub(r"[^\w\s€$%.,:/-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


CONTRACTIONS = {
    "can't": "can not", "cannot": "can not", "won't": "will not", "don't": "do not", "doesn't": "does not",
    "isn't": "is not", "aren't": "are not", "wasn't": "was not", "didn't": "did not", "what's": "what is",
    "how's": "how is", "it's": "it is", "i'd": "i would", "i'm": "i am", "there's": "there is",
}  # fmt: skip
# Words whose presence or absence does not change what is being asked. Everything else, including
# every number, name and negation, must match exactly for a cached answer to be served.
FILLER = set(
    [
        "a",
        "an",
        "the",
        "please",
        "hi",
        "hello",
        "hey",
        "thanks",
        "thank",
        "you",
        "could",
        "would",
        "can",
        "kindly",
        "tell",
        "me",
        "i",
        "like",
        "to",
        "know",
        "quick",
        "question",
        "just",
        "so",
        "um",
        "ok",
        "okay",
        "regards",
        "cheers",
    ]
)
_TOKEN = re.compile(r"[a-z0-9€$%]+(?:[.,][0-9]+)*")


def slots(text: str) -> list[str]:
    """The words that carry the meaning of a prompt: normalised, contractions expanded, fillers dropped.

    A semantic hit is served only when both prompts have exactly the same such words. Repeats that
    differ in case, punctuation, greetings or fillers pass; a changed amount, country, product, date
    or an added "not" does not.
    """
    t = unicodedata.normalize("NFKC", text).lower().replace("’", "'")
    for k, v in CONTRACTIONS.items():
        t = re.sub(rf"\b{re.escape(k)}\b", v, t)
    return sorted(w for w in _TOKEN.findall(t) if w not in FILLER)


def exact_key(workspace_id: str, task: str, quality: str, messages: list[dict]) -> str:
    body = json.dumps(
        [
            workspace_id,
            task,
            quality,
            [(m.get("role"), normalise(str(m.get("content", "")))) for m in messages],
        ]
    )
    return "mrp:cache:" + hashlib.sha256(body.encode()).hexdigest()


@dataclass
class Hit:
    response: str
    model: str
    cost: Decimal  # actual cost of the original call
    kind: str  # exact, semantic
    similarity: float = 1.0


class Cache:
    def __init__(
        self,
        redis: Redis,
        embedder: Embedder | None = None,
        threshold: float = 0.85,
        ttl_s: int = 86_400,
        guard: bool = True,
    ):
        self.redis, self.embedder = redis, embedder or HashingEmbedder()
        self.threshold, self.ttl_s, self.guard = threshold, ttl_s, guard

    async def get(
        self,
        conn: asyncpg.Connection,
        workspace_id: str,
        task: str,
        quality: str,
        messages: list[dict],
        semantic: bool,
    ) -> Hit | None:
        try:
            raw = await self.redis.get(exact_key(workspace_id, task, quality, messages))
        except RedisError:
            raw = None  # Redis down: the cache is an optimisation, never a reason to fail a request
        if raw:
            d = json.loads(raw)
            return Hit(d["response"], d["model"], Decimal(d["cost"]), "exact")
        if (
            not semantic
            or len(messages) != 1
            and not (len(messages) == 2 and messages[0].get("role") == "system")
        ):
            return None  # semantic matching only for single-turn requests
        prompt = str(messages[-1].get("content", ""))
        vec = self.embedder.embed(prompt)
        rows = await conn.fetch(
            "SELECT id, prompt, slots, response, model, cost, 1 - (embedding <=> $1) AS sim FROM mrp.cache_entries "
            "WHERE task = $2 AND quality = $3 ORDER BY embedding <=> $1 LIMIT 5",
            vec,
            task,
            quality,
        )
        want = slots(prompt)
        for r in rows:
            if r["sim"] < self.threshold:
                break
            if self.guard and r["slots"] != want:
                continue
            await conn.execute("UPDATE mrp.cache_entries SET hits = hits + 1 WHERE id = $1", r["id"])
            return Hit(r["response"], r["model"], Decimal(r["cost"]), "semantic", float(r["sim"]))
        return None

    async def put(
        self,
        conn: asyncpg.Connection,
        workspace_id: str,
        task: str,
        quality: str,
        messages: list[dict],
        response: str,
        model: str,
        cost: Decimal,
        semantic: bool,
    ) -> None:
        with contextlib.suppress(RedisError):  # Redis down: skip the exact cache, keep serving
            await self.redis.set(
                exact_key(workspace_id, task, quality, messages),
                json.dumps({"response": response, "model": model, "cost": str(cost)}),
                ex=self.ttl_s,
            )
        if semantic and len(messages) <= 2:
            prompt = str(messages[-1].get("content", ""))
            await conn.execute(
                "INSERT INTO mrp.cache_entries (workspace_id, task, quality, prompt, slots, embedding, response, model, cost) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                uuid.UUID(workspace_id),
                task,
                quality,
                prompt,
                slots(prompt),
                self.embedder.embed(prompt),
                response,
                model,
                cost,
            )


# ---- history compression ----------------------------------------------------------------------------------


def compress(messages: list[dict], budget_tokens: int, keep_last: int = 4) -> tuple[list[dict], int]:
    """Keeps system messages and the last turns; older turns become one short digest.

    Returns the new messages and the estimated tokens removed (an estimate, reported as such).
    Deterministic and model-free, so it adds no cost and cannot invent content: the digest keeps the
    first sentence of each older turn.
    """
    before = sum(estimate_tokens(str(m.get("content", ""))) for m in messages)
    if before <= budget_tokens or len(messages) <= keep_last + 1:
        return messages, 0
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    old, recent = rest[:-keep_last], rest[-keep_last:]
    lines = []
    for m in old:
        first = re.split(r"(?<=[.!?])\s", str(m.get("content", "")).strip(), maxsplit=1)[0][:200]
        lines.append(f"{m.get('role')}: {first}")
    digest = {"role": "system", "content": "Earlier in this conversation (condensed):\n" + "\n".join(lines)}
    out = system + [digest] + recent
    after = sum(estimate_tokens(str(m.get("content", ""))) for m in out)
    return out, max(0, before - after)
