"""The background worker (arq): agent runs and reconciliation.

arq mrp.worker.WorkerSettings
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

from arq import cron
from arq.connections import RedisSettings

from mrp import reconcile, router
from mrp.agents import FakeConnectors, Runner
from mrp.app import DEFAULT_TABLE, build_providers
from mrp.catalog import load
from mrp.db import Database


async def startup(ctx: dict) -> None:
    catalog = load()
    db = Database(os.environ["MRP_DATABASE_URL"], os.environ.get("MRP_APP_ROLE", "mrp_app") or None)
    await db.start(migrate=True)  # idempotent, under an advisory lock; safe if the gateway migrates too
    providers = build_providers(os.environ.get("MRP_PROVIDERS", "sim"), catalog)
    table = router.QualityTable.load(DEFAULT_TABLE)
    # Real Gmail / Slack / calendar connectors plug in here; the fakes keep the same once-per-key contract.
    ctx.update(
        db=db,
        catalog=catalog,
        providers=providers,
        runner=Runner(db, catalog, table, providers, FakeConnectors()),
    )


async def shutdown(ctx: dict) -> None:
    await ctx["db"].close()


async def run_agent(ctx: dict, workspace_id: str, plan: str, run_id: str) -> str:
    return await ctx["runner"].run(uuid.UUID(workspace_id), plan, uuid.UUID(run_id))


async def reconcile_calls(ctx: dict) -> dict:
    return await reconcile.settle_unknown_calls(
        ctx["db"], ctx["catalog"], ctx["providers"], timedelta(minutes=5)
    )


class WorkerSettings:
    functions = [run_agent]
    cron_jobs = [cron(reconcile_calls, minute=set(range(0, 60, 5)))]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(os.environ.get("MRP_REDIS_URL", "redis://localhost:6379/0"))
    max_tries = 5
    job_timeout = 600
