"""The request path: streaming, fallbacks, errors, cascades and caching."""

import asyncio
import json
from datetime import timedelta

import pytest

from mrp import reconcile
from mrp.catalog import load
from mrp.providers.base import ProviderError
from tests.conftest import chat


async def test_streaming_returns_chunks_and_settles(env):
    ws, h = await env.workspace()
    r = await env.client.post(
        "/v1/chat/completions", json=chat("Tell me about the weather in Berlin.", stream=True), headers=h
    )
    assert r.status_code == 200
    events = [line[6:] for line in r.text.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]" and len(events) > 3
    text = "".join(json.loads(e)["choices"][0]["delta"]["content"] for e in events[:-1])
    assert "answer" in text
    async with env.db.tenant(ws) as conn:
        row = await conn.fetchrow("SELECT status, billed_amount, actual_cost FROM mrp.usage")
    assert row["status"] == "ok" and row["billed_amount"] > 0


async def raw_asgi_disconnect_after_first_chunk(app, headers: dict, body: dict) -> list[bytes]:
    """Drives the ASGI app directly so the client can vanish mid-stream, as a real one does."""
    sent: list[bytes] = []
    got_chunk = asyncio.Event()
    payload = json.dumps(body).encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions", "query_string": b"", "root_path": "",
        "headers": [(b"content-type", b"application/json")] + [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("test", 1), "server": ("gw", 80),
    }  # fmt: skip
    messages = [{"type": "http.request", "body": payload, "more_body": False}]

    async def receive():
        if messages:
            return messages.pop(0)
        await got_chunk.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.body" and msg.get("body"):
            sent.append(msg["body"])
            got_chunk.set()

    await app(scope, receive, send)
    return sent


async def test_a_client_that_disconnects_mid_stream_pays_for_what_was_generated(env):
    ws, h = await env.workspace()
    for p in env.providers.values():
        p.latency_scale = 1.0  # slow enough to disconnect in the middle
    sent = await raw_asgi_disconnect_after_first_chunk(
        env.app, h, chat("Write a long story about a lighthouse.", stream=True, model="open-70b")
    )
    assert sent and b"[DONE]" not in b"".join(sent)
    await asyncio.sleep(0.2)
    async with env.db.tenant(ws) as conn:
        row = await conn.fetchrow("SELECT status, billed_amount FROM mrp.usage")
        call = await conn.fetchrow("SELECT status, cost FROM mrp.provider_calls")
    assert row["status"] == "cancelled" and call["status"] == "cancelled" and call["cost"] is None
    out = await reconcile.settle_unknown_calls(env.db, load(), env.providers, older_than=timedelta(0))
    assert out["settled"] == 1
    async with env.db.tenant(ws) as conn:
        row = await conn.fetchrow("SELECT status, actual_cost, billed_amount FROM mrp.usage")
    assert row["status"] == "cancelled" and row["actual_cost"] > 0 and row["billed_amount"] > 0


async def test_falls_back_before_the_first_token_when_a_model_is_down(env):
    _, h = await env.workspace()
    env.providers["closed-a"].down = {"frontier-mid"}
    r = await env.client.post(
        "/v1/chat/completions",
        json=chat("Hello there", stream=True, model="frontier-mid", mrp={"fallbacks": ["general-b"]}),
        headers=h,
    )
    events = [
        json.loads(line[6:])
        for line in r.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert {e["model"] for e in events} == {"general-b"}


async def test_a_non_retryable_error_is_not_retried_elsewhere(env, monkeypatch):
    ws, h = await env.workspace()

    async def bad_request(*a, **k):
        raise ProviderError("400 invalid request", retryable=False)

    monkeypatch.setattr(env.providers["closed-a"], "complete", bad_request)
    r = await env.client.post(
        "/v1/chat/completions",
        json=chat("Hi", model="frontier-mid", mrp={"fallbacks": ["general-b"]}),
        headers=h,
    )
    assert r.status_code == 502
    assert env.providers["closed-b"].calls == 0
    async with env.db.tenant(ws) as conn:
        assert await conn.fetchval("SELECT status FROM mrp.usage") == "failed"
        assert await conn.fetchval("SELECT billed_amount FROM mrp.usage") == 0


async def test_cheap_first_then_escalate_on_a_visibly_wrong_answer(env):
    _, h = await env.workspace()
    seen = set()
    for i in range(40):
        r = await env.client.post(
            "/v1/chat/completions",
            json=chat(
                f"Extract the invoice id and amount as JSON: invoice {i} for {i}.50 EUR",
                mrp={"quality": "high"},
            ),
            headers=h,
        )
        assert r.status_code == 200
        seen.add((r.headers["x-mrp-model"], r.headers["x-mrp-escalated"]))
        assert json.loads(r.json()["choices"][0]["message"]["content"])  # every delivered answer parses
    assert any(esc == "true" for _, esc in seen)


async def test_semantic_cache_serves_near_duplicates_but_not_different_numbers(env):
    _, h = await env.workspace(plan="team")
    a = chat("What is the refund limit for orders above 500 EUR in Germany?", temperature=0)
    near = chat("what is the refund limit for orders above 500 EUR in Germany", temperature=0)
    other = chat("What is the refund limit for orders above 900 EUR in Germany?", temperature=0)
    assert (await env.client.post("/v1/chat/completions", json=a, headers=h)).headers["x-mrp-cache"] == "miss"
    assert (await env.client.post("/v1/chat/completions", json=near, headers=h)).headers["x-mrp-cache"] in (
        "exact",
        "semantic",
    )
    assert (await env.client.post("/v1/chat/completions", json=other, headers=h)).headers[
        "x-mrp-cache"
    ] == "miss"


async def test_long_histories_are_compressed(env):
    _, h = await env.workspace()
    history = []
    for i in range(30):
        history += [
            {"role": "user", "content": f"Question {i}. " + "Some detail. " * 80},
            {"role": "assistant", "content": f"Answer {i}. " + "More words. " * 80},
        ]
    history.append({"role": "user", "content": "And finally, how are you?"})
    r = await env.client.post("/v1/chat/completions", json={"model": "auto", "messages": history}, headers=h)
    assert r.status_code == 200 and int(r.headers["x-mrp-history-tokens-saved-est"]) > 10_000


@pytest.mark.parametrize("quality", ["economy", "high"])
async def test_explanations_come_with_the_route(env, quality):
    _, h = await env.workspace()
    r = await env.client.post(
        "/v1/chat/completions",
        json=chat("Classify the sentiment: terrible delay", mrp={"quality": quality}),
        headers=h,
    )
    assert r.headers["x-mrp-task"] == "classification" and "classification" in r.headers["x-mrp-route"]


async def test_redis_down_makes_the_cache_a_miss_not_an_error(env, monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError

    _, h = await env.workspace()
    redis = env.app.state.state["redis"]

    async def down(*a, **k):
        raise RedisConnectionError("redis unavailable")

    monkeypatch.setattr(redis, "get", down)
    monkeypatch.setattr(redis, "set", down)
    r = await env.client.post(
        "/v1/chat/completions", json=chat("Classify the sentiment: fine", temperature=0), headers=h
    )
    assert r.status_code == 200 and r.headers["x-mrp-cache"] == "miss"
