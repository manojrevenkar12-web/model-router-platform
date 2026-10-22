"""Agent runs: checkpoints, effects once, cost limits, approvals; and the worker wiring."""

import uuid
from decimal import Decimal

import pytest

from mrp import agents
from mrp.agents import Crash, FakeConnectors, Runner
from mrp.catalog import load
from mrp.router import QualityTable

pytestmark = pytest.mark.invariant
TABLE = QualityTable.load(__import__("mrp.app", fromlist=["DEFAULT_TABLE"]).DEFAULT_TABLE)
STEPS = [
    {
        "kind": "model",
        "prompt": "Summarize this week's incidents in three bullet points.",
        "task": "summarization",
    },
    {"kind": "tool", "name": "slack.post", "args": {"channel": "#ops", "text": "weekly summary"}},
    {
        "kind": "tool",
        "name": "calendar.create",
        "args": {"title": "Incident review", "attendees": ["a@example.com"]},
    },
    {
        "kind": "tool",
        "name": "gmail.send",
        "args": {"to": ["partner@other.org"], "subject": "Weekly summary"},
    },
]


def runner(env, connectors=None, worker="w1"):
    return Runner(env.db, load(), TABLE, env.providers, connectors or FakeConnectors(), worker_id=worker)


async def expire_lease(env, ws, run_id):
    async with env.db.tenant(ws) as conn:
        await conn.execute(
            "UPDATE mrp.agent_runs SET lease_until = now() - interval '1 second' WHERE id = $1", run_id
        )


async def test_a_run_pauses_for_approval_and_resumes(env):
    ws, _ = await env.workspace()
    ws = uuid.UUID(ws)
    conn = FakeConnectors()
    r = runner(env, conn)
    run_id = await r.create(ws, "weekly ops summary", STEPS, Decimal("0.50"))
    assert await r.run(ws, "team", run_id) == "waiting_approval"
    assert [e[0] for e in conn.effects] == ["slack.post", "calendar.create"]  # the external email waits
    with pytest.raises(ValueError):
        await r.decide(ws, run_id, 1, True, "lead@example.com")  # nothing pending at step 1
    await r.decide(ws, run_id, 3, True, "lead@example.com")
    assert await r.run(ws, "team", run_id) == "succeeded"
    assert [e[0] for e in conn.effects] == ["slack.post", "calendar.create", "gmail.send"]
    async with env.db.tenant(ws) as c:
        step = await c.fetchrow(
            "SELECT status, decided_by FROM mrp.run_steps WHERE run_id = $1 AND step_no = 3", run_id
        )
        spent = await c.fetchval("SELECT cost_spent FROM mrp.agent_runs WHERE id = $1", run_id)
    assert step["status"] == "done" and step["decided_by"] == "lead@example.com" and spent > 0


async def test_rejection_stops_the_run(env):
    ws = uuid.UUID((await env.workspace())[0])
    r = runner(env)
    run_id = await r.create(ws, "x", STEPS[3:], Decimal("0.5"))
    assert await r.run(ws, "team", run_id) == "waiting_approval"
    await r.decide(ws, run_id, 0, False, "lead@example.com")
    assert await r.run(ws, "team", run_id) == "rejected"


@pytest.mark.parametrize("point", ["before_effect", "after_effect"])
async def test_a_crash_never_repeats_an_effect_or_a_charge(env, point):
    ws = uuid.UUID((await env.workspace())[0])
    conn = FakeConnectors()
    steps = STEPS[:3]
    first = runner(env, conn, "w1")
    first.crash_at = (1, point)
    run_id = await first.create(ws, "x", steps, Decimal("0.5"))
    with pytest.raises(Crash):
        await first.run(ws, "team", run_id)
    second = runner(env, conn, "w2")
    assert await second.run(ws, "team", run_id) == "not_runnable"  # w1 still holds the lease
    await expire_lease(env, ws, run_id)
    assert await second.run(ws, "team", run_id) == "succeeded"
    assert [e[0] for e in conn.effects] == ["slack.post", "calendar.create"]  # each effect exactly once
    async with env.db.tenant(ws) as c:
        assert await c.fetchval("SELECT count(*) FROM mrp.usage") == 1  # the model step billed once
        assert await c.fetchval("SELECT count(*) FROM mrp.provider_calls") == 1


async def test_the_cost_limit_stops_a_run(env):
    ws = uuid.UUID((await env.workspace())[0])
    r = runner(env)
    steps = [
        {
            "kind": "model",
            "prompt": "Prove step by step " + "x " * 4000,
            "task": "reasoning",
            "quality": "high",
        }
    ]
    run_id = await r.create(ws, "x", steps, Decimal("0.0001"))
    assert await r.run(ws, "team", run_id) == "budget_exhausted"


async def test_approval_policy():
    assert agents.needs_approval("gmail.send", {"to": ["x@other.org"]})
    assert not agents.needs_approval("gmail.send", {"to": ["x@example.com"]})
    assert agents.needs_approval("calendar.create", {"attendees": ["a"] * 11})
    assert not agents.needs_approval("slack.post", {})


async def test_runs_through_the_api_and_the_arq_worker(env, urls):
    from arq.worker import Worker

    ws, h = await env.workspace()
    r = await env.client.post("/v1/agents/runs", json={"goal": "post", "steps": STEPS[:2]}, headers=h)
    assert r.status_code == 202
    run_id = r.json()["run_id"]

    async def run_agent(ctx, workspace_id, plan, rid):
        return await runner(env).run(uuid.UUID(workspace_id), plan, uuid.UUID(rid))

    from arq.connections import RedisSettings

    w = Worker(
        functions=[agents_job(run_agent)],
        redis_settings=RedisSettings.from_dsn(urls[1]),
        burst=True,
        poll_delay=0.05,
        handle_signals=False,
    )
    await w.async_run()
    await w.close()
    body = (await env.client.get(f"/v1/agents/runs/{run_id}", headers=h)).json()
    assert body["status"] == "succeeded" and [s["status"] for s in body["steps"]] == ["done", "done"]


def agents_job(fn):
    from arq.worker import func

    return func(fn, name="run_agent")
