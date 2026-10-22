"""Tenant isolation, enforced by PostgreSQL row-level security."""

import uuid

import asyncpg
import pytest

from tests.conftest import chat

pytestmark = pytest.mark.invariant


async def test_every_pooled_connection_runs_as_the_app_role(env):
    # asyncpg resets connections on release (RESET ALL drops SET ROLE); the role must come back each time
    for _ in range(25):
        async with env.db.app.acquire() as conn:
            assert await conn.fetchval("SELECT current_user") == "mrp_app"


async def test_a_query_without_a_workspace_filter_sees_only_its_workspace(env):
    a, ha = await env.workspace(name="a")
    b, hb = await env.workspace(name="b")
    for h in (ha, hb):
        r = await env.client.post(
            "/v1/chat/completions", json=chat("Summarize the key points of our Q3 call."), headers=h
        )
        assert r.status_code == 200, r.text
    async with env.db.tenant(a) as conn:
        rows = await conn.fetch("SELECT workspace_id FROM mrp.usage")  # no WHERE on purpose
        assert {str(r["workspace_id"]) for r in rows} == {a}
        assert await conn.fetchval("SELECT count(*) FROM mrp.provider_calls") >= 1
    async with env.db.app.acquire() as conn:  # no workspace set at all
        assert await conn.fetchval("SELECT count(*) FROM mrp.usage") == 0


async def test_writing_into_another_workspace_is_refused(env):
    a, _ = await env.workspace(name="a")
    b, _ = await env.workspace(name="b")
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        async with env.db.tenant(a) as conn:
            await conn.execute(
                "INSERT INTO mrp.usage (id, workspace_id, idempotency_key, request_hash, status) VALUES ($1, $2, 'k', 'h', 'ok')",
                uuid.uuid4(),
                uuid.UUID(b),
            )


async def test_the_app_role_cannot_read_workspaces_or_keys_directly(env):
    await env.workspace()
    async with env.db.app.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.fetch("SELECT * FROM mrp.workspaces")
        assert await conn.fetchval("SELECT count(*) FROM mrp.api_keys") == 0


async def test_api_respects_isolation_for_runs_usage_and_cache(env):
    a, ha = await env.workspace(name="a")
    b, hb = await env.workspace(name="b")
    r = await env.client.post(
        "/v1/agents/runs",
        json={"goal": "post", "steps": [{"kind": "tool", "name": "slack.post", "args": {"text": "hi"}}]},
        headers=ha,
    )
    run_id = r.json()["run_id"]
    assert (await env.client.get(f"/v1/agents/runs/{run_id}", headers=hb)).status_code == 404
    q = chat("Extract the invoice id and amount as JSON from: invoice INV-7 for 120.50 EUR", temperature=0)
    first = await env.client.post("/v1/chat/completions", json=q, headers=ha)
    assert first.headers["x-mrp-cache"] == "miss"
    other = await env.client.post("/v1/chat/completions", json=q, headers=hb)
    assert other.headers["x-mrp-cache"] == "miss"  # B never gets A's cached answer
    again = await env.client.post("/v1/chat/completions", json=q, headers=ha)
    assert again.headers["x-mrp-cache"] == "exact"


async def test_bad_keys_are_rejected(env):
    await env.workspace()
    assert (await env.client.get("/v1/models", headers={"Authorization": "Bearer nope"})).status_code == 401
    assert (
        await env.client.get("/v1/models", headers={"Authorization": "Bearer mrp_" + "x" * 40})
    ).status_code == 401
