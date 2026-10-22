"""Fixtures. Tests run against real PostgreSQL (with pgvector) and Redis: from MRP_TEST_DATABASE_URL /
MRP_TEST_REDIS_URL when set (CI services, a laptop), otherwise started with testcontainers."""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator

import asyncpg
import httpx
import pytest
from redis.asyncio import Redis

from mrp.app import Chaos, Settings, create_app
from mrp.catalog import load
from mrp.providers.sim import SimProvider
from mrp.secrets import LocalKms

ADMIN = "test-admin-token"
KEK1 = base64.b64encode(b"1" * 32).decode()
KEK2 = base64.b64encode(b"2" * 32).decode()


@pytest.fixture(scope="session")
def urls():
    db, redis = os.environ.get("MRP_TEST_DATABASE_URL"), os.environ.get("MRP_TEST_REDIS_URL")
    if db and redis:
        yield db, redis
        return
    from testcontainers.postgres import PostgresContainer
    from testcontainers.redis import RedisContainer

    with PostgresContainer("pgvector/pgvector:pg16", driver=None) as pg, RedisContainer("redis:7") as rd:
        yield pg.get_connection_url(), f"redis://{rd.get_container_host_ip()}:{rd.get_exposed_port(6379)}/0"


async def reset(db_url: str, redis_url: str) -> None:
    conn = await asyncpg.connect(db_url)
    await conn.execute("DROP SCHEMA IF EXISTS mrp CASCADE")
    await conn.close()
    r = Redis.from_url(redis_url)
    await r.flushdb()
    await r.aclose()


class Env:
    def __init__(self, client: httpx.AsyncClient, app, providers: dict[str, SimProvider], chaos: Chaos):
        self.client, self.app, self.providers, self.chaos = client, app, providers, chaos

    async def workspace(self, plan: str = "team", name: str = "acme") -> tuple[str, dict]:
        r = await self.client.post(
            "/admin/workspaces", json={"name": name, "plan": plan}, headers={"X-Admin-Token": ADMIN}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        return body["workspace_id"], {"Authorization": f"Bearer {body['api_key']}"}

    @property
    def db(self):
        return self.app.state.db


def sim_providers() -> dict[str, SimProvider]:
    return {
        n: SimProvider(name=n, seed=i)
        for i, n in enumerate(sorted({m.provider for m in load().models.values()}))
    }


@pytest.fixture
async def env(urls) -> AsyncIterator[Env]:
    db_url, redis_url = urls
    await reset(db_url, redis_url)
    providers = sim_providers()
    chaos = Chaos()
    settings = Settings(
        database_url=db_url, redis_url=redis_url, admin_token=ADMIN, retries=0, lease_seconds=0
    )
    app = create_app(
        settings, providers=providers, kms=LocalKms({"v1": base64.b64decode(KEK1)}, "v1"), chaos=chaos
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw", timeout=30) as client:
            yield Env(client, app, providers, chaos)


def chat(content: str, **kw) -> dict:
    return {"model": kw.pop("model", "auto"), "messages": [{"role": "user", "content": content}], **kw}
