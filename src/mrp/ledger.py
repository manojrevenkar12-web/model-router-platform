"""Usage and billing: every call accounted once, estimates kept apart from spend.

The lifecycle of a request:

1. `begin` records it as pending under the client's idempotency key, in its own transaction. A
   retry with the same key finds the row: it gets the stored answer if there is one, a 409 while
   the first attempt is still running, or takes over an attempt that died (its lease expired).
2. Before each provider call, `call_started` records the call with our request id, committed
   before the call is made, so a crash afterwards still leaves a trace to reconcile.
3. `call_finished` records the provider's reported tokens and the actual cost.
4. `settle` marks the request done, links the delivered call and computes what the customer pays:
   the actual cost of the call that produced the answer, plus the plan's markup. Failed retries,
   escalations and calls lost to crashes are our cost, recorded, never billed.

Estimated costs (before the call) and avoided costs (cache hits) are separate columns and are never
summed into spend.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import asyncpg

from mrp.catalog import Catalog, Plan

CENT8 = Decimal("0.00000001")  # money is kept to 8 decimals, rounded half up as PostgreSQL's round() does

LEASE = timedelta(seconds=120)


def request_hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


class Conflict(Exception):
    """Same idempotency key, different request body."""


class InProgress(Exception):
    """The first attempt with this key is still running."""


class Entitlement(Exception):
    def __init__(self, message: str, status: int = 402):
        super().__init__(message)
        self.status = status


@dataclass
class Begun:
    usage_id: uuid.UUID
    replay: dict | None = None  # the stored response of a completed earlier attempt
    took_over: bool = False


async def check_entitlements(
    conn: asyncpg.Connection, plan: Plan, estimated_cost: Decimal, feature: str | None = None
) -> None:
    if feature and feature not in plan.features:
        raise Entitlement(f"the {plan.name} plan does not include {feature}", 403)
    if estimated_cost > plan.max_request_cost:
        raise Entitlement(
            f"estimated cost {estimated_cost:.4f} exceeds the plan's per-request limit {plan.max_request_cost}"
        )
    if plan.monthly_tokens:
        start = date.today().replace(day=1)
        used = await conn.fetchval(
            "SELECT coalesce(sum(tokens_in + tokens_out), 0) FROM mrp.usage WHERE created_at >= $1 AND status IN ('ok', 'cancelled')",
            datetime(start.year, start.month, 1, tzinfo=UTC),
        )
        if used >= plan.monthly_tokens:
            raise Entitlement(f"monthly token allowance of {plan.monthly_tokens:,} used")


async def begin(
    conn: asyncpg.Connection,
    workspace_id: uuid.UUID,
    key: str,
    rhash: str,
    task: str,
    model: str,
    estimated: Decimal,
) -> Begun:
    usage_id = uuid.uuid4()
    row = await conn.fetchrow(
        "INSERT INTO mrp.usage (id, workspace_id, idempotency_key, request_hash, status, task, model, estimated_cost) "
        "VALUES ($1, $2, $3, $4, 'pending', $5, $6, $7) ON CONFLICT (workspace_id, idempotency_key) DO NOTHING RETURNING id",
        usage_id,
        workspace_id,
        key,
        rhash,
        task,
        model,
        estimated,
    )
    if row:
        return Begun(usage_id)
    prev = await conn.fetchrow(
        "SELECT id, request_hash, status, created_at, settled_at FROM mrp.usage WHERE idempotency_key = $1 FOR UPDATE",
        key,
    )
    if prev["request_hash"] != rhash:
        raise Conflict("this idempotency key was used for a different request")
    if prev["status"] in ("ok", "cache_hit", "cancelled"):
        body = await conn.fetchval("SELECT body FROM mrp.responses WHERE idempotency_key = $1", key)
        return Begun(prev["id"], replay=body or {"error": "completed earlier; response no longer stored"})
    if prev["status"] == "pending" and datetime.now(UTC) - prev["created_at"] < LEASE:
        raise InProgress("the first attempt with this idempotency key is still running")
    # an earlier attempt failed or died: take over the same row; its provider calls stay recorded
    await conn.execute(
        "UPDATE mrp.provider_calls SET status = 'abandoned' WHERE usage_id = $1 AND status = 'started'",
        prev["id"],
    )
    await conn.execute(
        "UPDATE mrp.usage SET status = 'pending', created_at = now(), error = NULL, task = $2, model = $3, estimated_cost = $4 WHERE id = $1",
        prev["id"],
        task,
        model,
        estimated,
    )
    return Begun(prev["id"], took_over=True)


async def call_started(
    conn: asyncpg.Connection,
    workspace_id: uuid.UUID,
    usage_id: uuid.UUID,
    request_id: str,
    provider: str,
    model: str,
) -> None:
    await conn.execute(
        "INSERT INTO mrp.provider_calls (provider_request_id, usage_id, workspace_id, provider, model, status) VALUES ($1, $2, $3, $4, $5, 'started')",
        request_id,
        usage_id,
        workspace_id,
        provider,
        model,
    )


async def call_finished(
    conn: asyncpg.Connection,
    catalog: Catalog,
    request_id: str,
    model: str,
    status: str,
    tokens: tuple[int, int] | None,
) -> Decimal | None:
    cost = catalog.model(model).cost(*tokens) if tokens else None
    await conn.execute(
        "UPDATE mrp.provider_calls SET status = $2, tokens_in = $3, tokens_out = $4, cost = coalesce($5, cost), settled_at = now() WHERE provider_request_id = $1",
        request_id,
        status,
        tokens[0] if tokens else None,
        tokens[1] if tokens else None,
        cost if tokens else (Decimal(0) if status == "failed" else None),
    )
    return cost


async def settle(
    conn: asyncpg.Connection,
    usage_id: uuid.UUID,
    plan: Plan,
    delivered_request_id: str | None,
    status: str,
    response: dict | None,
    key: str,
    workspace_id: uuid.UUID,
    latency_ms: int,
    fallback: bool = False,
    escalated: bool = False,
    error: str | None = None,
) -> Decimal:
    """Marks the request done and returns the amount billed to the customer."""
    billed = Decimal(0)
    if delivered_request_id:
        call = await conn.fetchrow(
            "UPDATE mrp.provider_calls SET delivered = true WHERE provider_request_id = $1 RETURNING model, provider, tokens_in, tokens_out, cost",
            delivered_request_id,
        )
        if call["cost"] is not None:
            billed = (call["cost"] * (1 + plan.markup)).quantize(CENT8, rounding=ROUND_HALF_UP)
        await conn.execute(
            "UPDATE mrp.usage SET model = $2, provider = $3, provider_request_id = $4, tokens_in = $5, tokens_out = $6 WHERE id = $1",
            usage_id,
            call["model"],
            call["provider"],
            delivered_request_id,
            call["tokens_in"] or 0,
            call["tokens_out"] or 0,
        )
    actual = await conn.fetchval("SELECT sum(cost) FROM mrp.provider_calls WHERE usage_id = $1", usage_id)
    await conn.execute(
        "UPDATE mrp.usage SET status = $2, actual_cost = $3, billed_amount = $4, latency_ms = $5, fallback = $6, escalated = $7, error = $8, settled_at = now() WHERE id = $1",
        usage_id,
        status,
        actual,
        billed,
        latency_ms,
        fallback,
        escalated,
        error,
    )
    if response is not None:
        await conn.execute(
            "INSERT INTO mrp.responses (workspace_id, idempotency_key, body) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
            workspace_id,
            key,
            response,
        )
    return billed


async def record_cache_hit(
    conn: asyncpg.Connection,
    usage_id: uuid.UUID,
    workspace_id: uuid.UUID,
    key: str,
    model: str,
    original_cost: Decimal,
    avoided_estimate: Decimal,
    catalog: Catalog,
    plan: Plan,
    response: dict,
) -> Decimal:
    """Settles a pending request as a cache hit: no provider spend; the customer pays a share of
    what the original call actually cost; the avoided cost is kept as an estimate for reporting."""
    billed = (original_cost * catalog.cached_answer_charge * (1 + plan.markup)).quantize(
        CENT8, rounding=ROUND_HALF_UP
    )
    await conn.execute(
        "UPDATE mrp.usage SET status = 'cache_hit', model = $2, actual_cost = 0, avoided_cost_est = $3, billed_amount = $4, settled_at = now() WHERE id = $1",
        usage_id,
        model,
        avoided_estimate,
        billed,
    )
    await conn.execute(
        "INSERT INTO mrp.responses (workspace_id, idempotency_key, body) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
        workspace_id,
        key,
        response,
    )
    return billed


async def usage_report(conn: asyncpg.Connection, month: str) -> dict:
    """The workspace's month, by model and task. Spend and estimates are separate columns."""
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    end = (start + timedelta(days=32)).replace(day=1)
    rows = await conn.fetch(
        """SELECT coalesce(model, '-') AS model, coalesce(task, '-') AS task, count(*) AS requests,
                  count(*) FILTER (WHERE status = 'cache_hit') AS cache_hits,
                  sum(tokens_in) AS tokens_in, sum(tokens_out) AS tokens_out,
                  sum(estimated_cost) AS estimated_cost, coalesce(sum(actual_cost), 0) AS actual_cost,
                  sum(avoided_cost_est) AS avoided_cost_estimate, coalesce(sum(billed_amount), 0) AS billed
           FROM mrp.usage WHERE created_at >= $1 AND created_at < $2 AND status <> 'pending'
           GROUP BY 1, 2 ORDER BY actual_cost DESC""",
        start,
        end,
    )
    lines = [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in dict(r).items()} for r in rows]
    total = {
        k: str(sum(Decimal(str(line[k])) for line in lines))
        for k in ("estimated_cost", "actual_cost", "avoided_cost_estimate", "billed")
    }
    return {
        "month": month,
        "lines": lines,
        "totals": total,
        "note": "estimated_cost and avoided_cost_estimate are estimates; actual_cost is provider-reported spend",
    }
