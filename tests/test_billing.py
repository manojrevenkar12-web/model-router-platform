"""Usage and billing: exactly once, estimates apart from spend, reconciled with providers."""

import asyncio
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pytest

from mrp import reconcile
from mrp.catalog import load
from mrp.ledger import CENT8
from tests.conftest import chat

pytestmark = pytest.mark.invariant


async def usage_rows(env, ws):
    async with env.db.tenant(ws) as conn:
        return await conn.fetch("SELECT * FROM mrp.usage ORDER BY created_at")


async def test_a_retried_request_is_answered_and_billed_once(env):
    ws, h = await env.workspace()
    body = chat("Summarize the attached release notes in three bullet points.")
    first = await env.client.post("/v1/chat/completions", json=body, headers={**h, "Idempotency-Key": "k-1"})
    second = await env.client.post("/v1/chat/completions", json=body, headers={**h, "Idempotency-Key": "k-1"})
    assert first.status_code == second.status_code == 200
    assert second.headers.get("x-mrp-replayed") == "true" and second.json() == first.json()
    rows = await usage_rows(env, ws)
    assert len(rows) == 1 and rows[0]["billed_amount"] > 0
    assert sum(p.calls for p in env.providers.values()) == 1


async def test_the_same_key_with_a_different_body_is_refused(env):
    _, h = await env.workspace()
    await env.client.post(
        "/v1/chat/completions", json=chat("Summarize A."), headers={**h, "Idempotency-Key": "k"}
    )
    r = await env.client.post(
        "/v1/chat/completions", json=chat("Summarize B."), headers={**h, "Idempotency-Key": "k"}
    )
    assert r.status_code == 422


async def test_concurrent_duplicates_run_once(env, monkeypatch):
    from mrp import ledger

    monkeypatch.setattr(ledger, "LEASE", ledger.timedelta(seconds=120))
    ws, h = await env.workspace()
    for p in env.providers.values():
        p.latency_scale = 0.2
    body = chat("Summarize the incident report.", model="frontier-large")
    rs = await asyncio.gather(
        *[
            env.client.post("/v1/chat/completions", json=body, headers={**h, "Idempotency-Key": "dup"})
            for _ in range(4)
        ]
    )
    codes = sorted(r.status_code for r in rs)
    assert codes == [200, 409, 409, 409]  # one runs; the others are told to retry
    assert len(await usage_rows(env, ws)) == 1
    assert sum(p.calls for p in env.providers.values()) == 1


async def test_failed_calls_are_our_cost_never_the_customers(env):
    ws, h = await env.workspace()
    env.providers["closed-a"].down = {"frontier-large"}
    env.providers["closed-a"].fail_after_charge = 1.0  # also: frontier-mid charges, then fails
    r = await env.client.post(
        "/v1/chat/completions",
        json=chat(
            "Summarize the meeting.", model="frontier-large", mrp={"fallbacks": ["frontier-mid", "open-70b"]}
        ),
        headers=h,
    )
    assert (
        r.status_code == 200
        and r.headers["x-mrp-model"] == "open-70b"
        and r.headers["x-mrp-fallback"] == "true"
    )
    row = (await usage_rows(env, ws))[0]
    catalog = load()
    async with env.db.tenant(ws) as conn:
        calls = await conn.fetch(
            "SELECT model, status, cost, delivered FROM mrp.provider_calls ORDER BY created_at"
        )
    assert [c["status"] for c in calls] == ["failed", "failed", "ok"]
    assert calls[0]["cost"] == 0 and calls[1]["cost"] > 0  # the second failure was charged by the provider
    delivered = calls[2]["cost"]
    assert row["actual_cost"] == calls[1]["cost"] + delivered
    assert row["billed_amount"] == (delivered * (1 + catalog.plans["team"].markup)).quantize(
        CENT8, rounding=ROUND_HALF_UP
    )


async def test_a_crash_after_the_provider_answered_is_recovered_by_reconciliation(env):
    ws, h = await env.workspace()
    env.chaos.crash_after_provider = 1.0
    body = chat("Summarize the quarterly numbers.", model="open-70b")
    with pytest.raises(RuntimeError):
        await env.client.post("/v1/chat/completions", json=body, headers={**h, "Idempotency-Key": "c-1"})
    env.chaos.crash_after_provider = 0.0
    r = await env.client.post("/v1/chat/completions", json=body, headers={**h, "Idempotency-Key": "c-1"})
    assert r.status_code == 200  # the retry takes over the dead attempt
    out = await reconcile.settle_unknown_calls(
        env.db, load(), env.providers, older_than=__import__("datetime").timedelta(0)
    )
    assert out["settled"] == 1
    async with env.db.tenant(ws) as conn:
        calls = await conn.fetch(
            "SELECT provider_request_id, status, cost, delivered FROM mrp.provider_calls ORDER BY created_at"
        )
        row = await conn.fetchrow("SELECT * FROM mrp.usage")
    assert [c["status"] for c in calls] == ["abandoned", "ok"] and all(c["cost"] > 0 for c in calls)
    charged = env.providers["self-hosted"].charges
    assert set(charged) == {c["provider_request_id"] for c in calls}  # every provider charge is in the ledger
    assert row["actual_cost"] == sum(c["cost"] for c in calls)
    assert row["billed_amount"] < row["actual_cost"]  # the lost call is not billed


async def test_entitlements_and_feature_gates(env):
    _, h = await env.workspace(plan="free")
    big = chat("Explain step by step and prove the result " + "x " * 20000, model="frontier-large")
    r = await env.client.post("/v1/chat/completions", json=big, headers=h)
    assert r.status_code == 402 and "per-request limit" in r.text
    r = await env.client.post(
        "/v1/agents/runs", json={"goal": "x", "steps": [{"kind": "tool", "name": "slack.post"}]}, headers=h
    )
    assert r.status_code == 403


async def test_usage_report_keeps_estimates_apart_from_spend(env):
    _, h = await env.workspace()
    q = chat("Classify the sentiment of: great service", temperature=0)
    await env.client.post("/v1/chat/completions", json=q, headers=h)
    hit = await env.client.post("/v1/chat/completions", json=q, headers=h)
    assert hit.headers["x-mrp-cache"] == "exact" and hit.headers["x-mrp-actual-cost"] == "0"
    month = date.today().strftime("%Y-%m")
    rep = (await env.client.get(f"/v1/usage?month={month}", headers=h)).json()
    t = rep["totals"]
    assert Decimal(t["avoided_cost_estimate"]) > 0 and Decimal(t["actual_cost"]) > 0
    lines = rep["lines"]
    assert sum(line["cache_hits"] for line in lines) == 1


async def test_invoice_reconciliation_finds_every_difference(env):
    ws, h = await env.workspace()
    for i in range(12):
        await env.client.post(
            "/v1/chat/completions", json=chat(f"Summarize report number {i}.", model="open-70b"), headers=h
        )
    async with env.db.tenant(ws) as conn:
        calls = await conn.fetch("SELECT provider_request_id, model, cost FROM mrp.provider_calls")
    today = date.today()
    lines = [
        reconcile.InvoiceLine("self-hosted", today, c["model"], c["cost"], c["provider_request_id"])
        for c in calls
    ]
    lines[0].amount += Decimal("0.01")  # overbilled
    dropped = lines.pop(1)  # not on the invoice
    lines.append(
        reconcile.InvoiceLine("self-hosted", today, "open-70b", Decimal("0.02"), "mrp-unknown")
    )  # not ours
    rep = await reconcile.reconcile_invoice(env.db, "self-hosted", lines, today, today)
    assert (
        len(rep.amount_mismatch) == 1
        and len(rep.charged_not_in_ledger) == 1
        and len(rep.in_ledger_not_charged) == 1
    )
    assert rep.in_ledger_not_charged[0]["request_id"] == dropped.request_id
    daily = [reconcile.InvoiceLine("self-hosted", today, "open-70b", sum(c["cost"] for c in calls))]
    assert (await reconcile.reconcile_invoice(env.db, "self-hosted", daily, today, today)).clean
