"""PostgreSQL access: one pool for the owner (migrations, admin, reconciliation) and one for the app.

App connections run as the mrp_app role, which row-level security binds. Every tenant query goes
through `tenant(workspace_id)`, which opens a transaction and sets app.workspace_id for that
transaction only (SET LOCAL semantics), so a pooled connection can never carry one workspace's
setting into another's request.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import resources

import asyncpg
from pgvector.asyncpg import register_vector

ADVISORY_LOCK = 724_031


async def _init(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await register_vector(conn)


class Database:
    def __init__(self, url: str, app_role: str | None = "mrp_app"):
        self.url, self.app_role = url, app_role
        self.admin: asyncpg.Pool | None = None
        self.app: asyncpg.Pool | None = None

    async def start(self, migrate: bool = True, min_size: int = 1, max_size: int = 10) -> None:
        if migrate:
            await self.migrate()
        self.admin = await asyncpg.create_pool(self.url, min_size=1, max_size=4, init=_init)

        async def as_app(conn: asyncpg.Connection) -> None:
            # On every acquire, not only on connect: the pool runs RESET ALL when a connection is
            # released, which would silently drop the role and with it row-level security.
            if self.app_role:
                await conn.execute(f'SET ROLE "{self.app_role}"')

        self.app = await asyncpg.create_pool(
            self.url, min_size=min_size, max_size=max_size, init=_init, setup=as_app
        )

    async def migrate(self) -> None:
        conn = await asyncpg.connect(self.url)
        try:
            await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK)  # replicas start together
            try:
                for name in sorted(
                    p.name for p in resources.files("mrp.sql").iterdir() if p.name.endswith(".sql")
                ):
                    await conn.execute(resources.files("mrp.sql").joinpath(name).read_text())
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK)
        finally:
            await conn.close()

    async def close(self) -> None:
        for pool in (self.app, self.admin):
            if pool is not None:
                await pool.close()

    @asynccontextmanager
    async def tenant(self, workspace_id: uuid.UUID | str) -> AsyncIterator[asyncpg.Connection]:
        """A transaction scoped to one workspace by row-level security."""
        assert self.app is not None
        async with self.app.acquire() as conn, conn.transaction():
            await conn.execute("SELECT set_config('app.workspace_id', $1, true)", str(workspace_id))
            yield conn

    @asynccontextmanager
    async def owner(self) -> AsyncIterator[asyncpg.Connection]:
        """Owner connection: admin operations and cross-workspace jobs only."""
        assert self.admin is not None
        async with self.admin.acquire() as conn, conn.transaction():
            yield conn

    async def authenticate(self, key_hash: str) -> tuple[uuid.UUID, str] | None:
        assert self.app is not None
        async with self.app.acquire() as conn:
            row = await conn.fetchrow("SELECT workspace_id, plan FROM mrp.authenticate($1)", key_hash)
        return (row["workspace_id"], row["plan"]) if row else None
