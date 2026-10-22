"""Gateway overhead: what the platform adds to a model call, under concurrency.

Runs the real server (uvicorn, one process) against PostgreSQL and Redis with simulated providers
that answer instantly, so every millisecond measured is the gateway's own work: authentication,
classification, routing, ledger writes (four short transactions per request), the provider call
record and the response. Caching is off (temperature 0.7) so every request takes the full path.
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import statistics
import sys
import time

import httpx

from mrp.eval.billing import PROMPTS, _reset
from mrp.providers.sim import TASKS


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def run(db_url: str, redis_url: str, n: int = 2000, concurrency: int = 16) -> dict:
    await _reset(db_url, redis_url)
    port = _free_port()
    env = {
        **os.environ,
        "MRP_DATABASE_URL": db_url,
        "MRP_REDIS_URL": redis_url,
        "MRP_ADMIN_TOKEN": "load",
        "MRP_KEKS": "v1:" + base64.b64encode(os.urandom(32)).decode(),
        "MRP_PROVIDERS": "sim",
        "MRP_LOG_LEVEL": "WARNING",
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "uvicorn",
        "mrp.main:app",
        "--port",
        str(port),
        "--log-level",
        "warning",
        "--no-access-log",
        env=env,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(
            base_url=base, timeout=30, limits=httpx.Limits(max_connections=concurrency)
        ) as c:
            for _ in range(100):
                try:
                    if (await c.get("/healthz")).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.1)
            ws = (
                await c.post(
                    "/admin/workspaces",
                    json={"name": "load", "plan": "enterprise"},
                    headers={"X-Admin-Token": "load"},
                )
            ).json()
            h = {"Authorization": f"Bearer {ws['api_key']}"}
            for i in range(50):  # warm-up
                await c.post("/v1/chat/completions", json=_body(i), headers=h)
            results = {}
            for conc, count in ((1, max(200, n // 5)), (concurrency, n)):
                lat: list[float] = []
                errors = 0
                sem = asyncio.Semaphore(conc)

                async def one(i: int, lat=lat, sem=sem) -> None:
                    nonlocal errors
                    async with sem:
                        t0 = time.perf_counter()
                        r = await c.post("/v1/chat/completions", json=_body(i), headers=h)
                        lat.append((time.perf_counter() - t0) * 1000)
                        errors += r.status_code != 200

                t0 = time.perf_counter()
                await asyncio.gather(*(one(i) for i in range(count)))
                wall = time.perf_counter() - t0
                lat.sort()
                results[f"concurrency_{conc}"] = {
                    "requests": count,
                    "errors": errors,
                    "requests_per_second": round(count / wall, 1),
                    "p50_ms": round(statistics.median(lat), 1),
                    "p95_ms": round(lat[int(count * 0.95)], 1),
                    "p99_ms": round(lat[int(count * 0.99)], 1),
                }
    finally:
        proc.terminate()
        await proc.wait()
    return {
        **results,
        "note": "one uvicorn process sharing a 2-vCPU machine with PostgreSQL, Redis and the load generator; "
        "providers answer instantly, so this is gateway overhead only",
    }


def _body(i: int) -> dict:
    task = TASKS[i % len(TASKS)]
    return {
        "model": "auto",
        "temperature": 0.7,
        "messages": [{"role": "user", "content": PROMPTS[task].format(i=i)}],
        "mrp": {"task": task},
    }
