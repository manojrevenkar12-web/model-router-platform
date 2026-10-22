"""Billing under failure: is every provider charge in the ledger, and is every customer billed once?

Drives the real gateway (in process, against PostgreSQL and Redis) with simulated providers and
injected faults:

- providers that fail before charging, and after charging (a timeout after the completion);
- the gateway crashing between a provider's answer and its ledger write;
- clients retrying with the same idempotency key, sending duplicates, and disconnecting mid-stream.

Afterwards the reconciler runs, and the ledger is compared with the simulator's own record of what it
charged, call by call. Then a provider invoice with planted errors is reconciled.
"""

from __future__ import annotations

import asyncio
import base64
import os
import random
from collections import Counter
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import asyncpg
import httpx
from redis.asyncio import Redis

from mrp import reconcile
from mrp.app import Chaos, Settings, create_app
from mrp.catalog import load
from mrp.ledger import CENT8
from mrp.providers.sim import TASKS, SimProvider, sim_item
from mrp.secrets import LocalKms

PROMPTS = {
    "extraction": "Extract the invoice id and amount as JSON from document {i}.",
    "classification": "Classify the sentiment of review {i}.",
    "code": "Write a function that totals order {i}.",
    "summarization": "Summarize the meeting notes number {i}.",
    "reasoning": "Explain step by step why plan {i} works.",
    "chat": "Tell me something about topic {i}.",
}


async def _reset(db_url: str, redis_url: str) -> None:
    conn = await asyncpg.connect(db_url)
    await conn.execute("DROP SCHEMA IF EXISTS mrp CASCADE")
    await conn.close()
    r = Redis.from_url(redis_url)
    await r.flushdb()
    await r.aclose()


async def run(db_url: str, redis_url: str, n: int = 1500, seed: int = 5) -> dict:
    await _reset(db_url, redis_url)
    rng = random.Random(seed)
    catalog = load()
    providers = {
        name: SimProvider(name=name, seed=i, fail_before=0.03, fail_after_charge=0.03)
        for i, name in enumerate(sorted({m.provider for m in catalog.models.values()}))
    }
    chaos = Chaos(crash_after_provider=0.02, rng=random.Random(seed + 1))
    settings = Settings(
        database_url=db_url, redis_url=redis_url, admin_token="eval", retries=1, lease_seconds=0
    )
    kms = LocalKms({"v1": base64.b64decode(base64.b64encode(os.urandom(32)))}, "v1")
    app = create_app(settings, providers=providers, kms=kms, chaos=chaos)
    injected: Counter = Counter()
    outcome: Counter = Counter()
    keys_with_answer: set[tuple[str, str]] = set()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw", timeout=60) as client:
            workspaces = []
            for plan in ("team", "enterprise", "team"):
                r = await client.post(
                    "/admin/workspaces", json={"name": plan, "plan": plan}, headers={"X-Admin-Token": "eval"}
                )
                body = r.json()
                workspaces.append((body["workspace_id"], {"Authorization": f"Bearer {body['api_key']}"}))

            async def one(i: int) -> None:
                ws, h = workspaces[i % len(workspaces)]
                task = TASKS[i % len(TASKS)]
                content = PROMPTS[task].format(i=i) + " " + sim_item(task, f"b{i}")
                stream = rng.random() < 0.12
                body = {
                    "model": "auto",
                    "messages": [{"role": "user", "content": content}],
                    "stream": stream,
                    "mrp": {"task": task},
                }
                key = f"req-{i}"
                hk = {**h, "Idempotency-Key": key}
                if stream and rng.random() < 0.25:
                    injected["client_disconnects_mid_stream"] += 1
                    for p in providers.values():
                        p.latency_scale = 0.3
                    try:
                        await disconnect_after_first_chunk(app, hk, body)
                    finally:
                        for p in providers.values():
                            p.latency_scale = 0.0
                    outcome["disconnected"] += 1
                    return
                for _attempt in range(4):
                    r = await client.post("/v1/chat/completions", json=body, headers=hk)
                    if r.status_code == 200:
                        if stream and "[DONE]" not in r.text:
                            outcome["stream_failed_mid_way"] += 1
                        else:
                            keys_with_answer.add((ws, key))
                            outcome["answered"] += 1
                        break
                    if r.status_code in (500, 409):
                        injected["client_retries"] += 1
                        continue
                    outcome[f"http_{r.status_code}"] += 1
                    break
                else:
                    outcome["gave_up"] += 1
                if rng.random() < 0.05:  # a client that sends the same request again later
                    injected["late_duplicates"] += 1
                    r2 = await client.post("/v1/chat/completions", json=body, headers=hk)
                    outcome[
                        "duplicate_replayed"
                        if r2.headers.get("x-mrp-replayed") == "true"
                        else f"duplicate_{r2.status_code}"
                    ] += 1

            for start in range(0, n, 8):
                await asyncio.gather(*(one(i) for i in range(start, min(n, start + 8))))

            rec = await reconcile.settle_unknown_calls(
                app.state.db, catalog, providers, older_than=timedelta(0)
            )

            # ---- compare the ledger with the providers' own records -------------------------------
            charges: dict[str, tuple[str, str, Decimal]] = {}
            for name, p in providers.items():
                for rid, (model, tin, tout) in p.charges.items():
                    charges[rid] = (name, model, catalog.model(model).cost(tin, tout))
            async with app.state.db.owner() as conn:
                calls = {
                    r["provider_request_id"]: r for r in await conn.fetch("SELECT * FROM mrp.provider_calls")
                }
                usage = await conn.fetch(
                    "SELECT u.*, w.plan FROM mrp.usage u JOIN mrp.workspaces w ON w.id = u.workspace_id"
                )
            missing = [rid for rid in charges if rid not in calls or calls[rid]["cost"] is None]
            mismatched = [
                rid
                for rid, (_, _, cost) in charges.items()
                if rid in calls and calls[rid]["cost"] is not None and calls[rid]["cost"] != cost
            ]
            phantom = [rid for rid, c in calls.items() if (c["cost"] or 0) > 0 and rid not in charges]
            keys = Counter((str(u["workspace_id"]), u["idempotency_key"]) for u in usage)
            double_rows = [k for k, v in keys.items() if v > 1]
            wrong_bill, billed_without_answer, answered_not_billed = [], [], []
            for u in usage:
                delivered = [c for c in calls.values() if c["usage_id"] == u["id"] and c["delivered"]]
                billed = u["billed_amount"] or Decimal(0)
                if u["status"] in ("ok", "cancelled") and delivered and delivered[0]["cost"] is not None:
                    expect = (delivered[0]["cost"] * (1 + catalog.plans[u["plan"]].markup)).quantize(
                        CENT8, rounding=ROUND_HALF_UP
                    )
                    if billed != expect:
                        wrong_bill.append(u["idempotency_key"])
                elif u["status"] in ("failed", "pending") and billed > 0:
                    billed_without_answer.append(u["idempotency_key"])
                if (str(u["workspace_id"]), u["idempotency_key"]) in keys_with_answer and u["status"] not in (
                    "ok",
                    "cache_hit",
                ):
                    answered_not_billed.append(u["idempotency_key"])
            charged_calls = Counter(c["status"] for c in calls.values())

            # ---- invoice with planted errors ---------------------------------------------------
            today = date.today()
            prov = "closed-a"
            lines = [
                reconcile.InvoiceLine(prov, today, m, cost, rid)
                for rid, (pname, m, cost) in charges.items()
                if pname == prov
            ]
            rng.shuffle(lines)
            k = max(3, len(lines) // 100)
            over = lines[:k]
            for ln in over:
                ln.amount += Decimal("0.0005")
            dropped = lines[k : 2 * k]
            kept = lines[:k] + lines[2 * k :]
            ghosts = [
                reconcile.InvoiceLine(prov, today, "frontier-mid", Decimal("0.0040"), f"mrp-ghost-{j}")
                for j in range(k)
            ]
            rep = await reconcile.reconcile_invoice(app.state.db, prov, kept + ghosts, today, today)
            found_over = {d["request_id"] for d in rep.amount_mismatch} == {ln.request_id for ln in over}
            found_dropped = {d["request_id"] for d in rep.in_ledger_not_charged} == {
                ln.request_id for ln in dropped
            }
            found_ghosts = {d["request_id"] for d in rep.charged_not_in_ledger} == {
                g.request_id for g in ghosts
            }

    for p in providers.values():
        injected.update(p.faults)
    return {
        "requests": n,
        "provider_calls": len(calls),
        "provider_calls_by_status": dict(charged_calls),
        "injected": {
            "provider_error_rate_before_charge": 0.03,
            "provider_error_rate_after_charge": 0.03,
            "gateway_crash_rate_after_answer": 0.02,
            "gateway_crashes": chaos.crashes,
            **dict(injected),
        },
        "outcomes": dict(outcome),
        "reconciler": rec,
        "ledger_vs_provider_records": {
            "provider_charges": len(charges),
            "charges_missing_from_ledger": len(missing),
            "cost_mismatches": len(mismatched),
            "ledger_costs_never_charged": len(phantom),
        },
        "customer_billing": {
            "usage_rows": len(usage),
            "keys_with_more_than_one_row": len(double_rows),
            "billed_amount_wrong": len(wrong_bill),
            "billed_without_an_answer": len(billed_without_answer),
            "answered_but_not_recorded_as_answered": len(answered_not_billed),
            "actual_spend_eur": str(sum((c["cost"] or Decimal(0)) for c in calls.values())),
            "billed_eur": str(sum((u["billed_amount"] or Decimal(0)) for u in usage)),
        },
        "invoice_reconciliation": {
            **rep.summary(),
            "planted": {
                "overbilled": len(over),
                "missing_from_invoice": len(dropped),
                "not_ours": len(ghosts),
            },
            "all_found": found_over and found_dropped and found_ghosts,
        },
    }


async def disconnect_after_first_chunk(app, headers: dict, body: dict) -> None:
    """Drives the ASGI app directly so the client can vanish mid-stream, as a real one does."""
    import json as _json

    got = asyncio.Event()
    payload = _json.dumps(body).encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions", "query_string": b"", "root_path": "",
        "headers": [(b"content-type", b"application/json")] + [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("eval", 1), "server": ("gw", 80),
    }  # fmt: skip
    pending = [{"type": "http.request", "body": payload, "more_body": False}]

    async def receive():
        if pending:
            return pending.pop(0)
        await got.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.body" and msg.get("body"):
            got.set()
        if msg["type"] == "http.response.start" and msg["status"] != 200:
            got.set()

    try:
        await app(scope, receive, send)
    except Exception:  # noqa: BLE001 - an injected crash surfaces here like any client-side 500
        got.set()
