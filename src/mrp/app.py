"""The gateway: one OpenAI-compatible API in front of closed providers and open-weight models.

A request is authenticated to a workspace, classified, routed within its cost budget, answered
from cache or by a provider (cheap first try, escalation, retries, fallbacks), accounted exactly
once and returned. Streaming falls back only before the first token; after that the client has part
of an answer and a different model's continuation would be wrong.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import secrets as pysecrets
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from mrp import cache as cache_mod
from mrp import ledger, reconcile, router, secrets
from mrp.catalog import Catalog, Model, estimate_tokens, load, messages_tokens
from mrp.db import Database
from mrp.health import Health
from mrp.providers.base import Completion, Delta, Provider, ProviderError
from mrp.telemetry import logger, tracer

log = logger("mrp.gateway")
DEFAULT_TABLE = Path(__file__).parent / "config" / "quality_table.json"
DEFAULT_OUT = {
    "extraction": 300,
    "classification": 16,
    "code": 800,
    "summarization": 600,
    "reasoning": 1500,
    "chat": 800,
}


@dataclass
class Settings:
    database_url: str
    redis_url: str
    admin_token: str
    providers: str = "sim"  # sim | litellm
    app_role: str | None = "mrp_app"
    quality_table: Path = DEFAULT_TABLE
    retries: int = 1
    history_budget_tokens: int = 6000
    lease_seconds: int = 120

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.environ["MRP_DATABASE_URL"],
            redis_url=os.environ.get("MRP_REDIS_URL", "redis://localhost:6379/0"),
            admin_token=os.environ["MRP_ADMIN_TOKEN"],
            providers=os.environ.get("MRP_PROVIDERS", "sim"),
            app_role=os.environ.get("MRP_APP_ROLE", "mrp_app") or None,
            quality_table=Path(os.environ.get("MRP_QUALITY_TABLE", str(DEFAULT_TABLE))),
        )


@dataclass
class Chaos:
    """Fault injection for the billing evaluation: crash between the provider's answer and the ledger."""

    crash_after_provider: float = 0.0
    rng: random.Random = field(default_factory=lambda: random.Random(0))
    crashes: int = 0


@dataclass
class Workspace:
    id: uuid.UUID
    plan: str


class Message(BaseModel):
    role: str
    content: Any = ""


class Options(BaseModel):
    task: str | None = None
    quality: str = "standard"
    max_cost: Decimal | None = None
    fallbacks: list[str] = Field(default_factory=list)
    compress_history: bool = True


class ChatRequest(BaseModel):
    model: str = "auto"
    messages: list[Message]
    max_tokens: int | None = None
    temperature: float = 0.0
    stream: bool = False
    response_format: dict | None = None
    mrp: Options = Field(default_factory=Options)


class ProviderKeyIn(BaseModel):
    api_key: str = Field(min_length=8)


class RunIn(BaseModel):
    goal: str
    steps: list[dict] = Field(min_length=1, max_length=50)
    cost_limit: Decimal = Decimal("0.50")


class WorkspaceIn(BaseModel):
    name: str
    plan: str


def build_providers(kind: str, catalog: Catalog) -> Mapping[str, Provider]:
    names = {m.provider for m in catalog.models.values()}
    if kind == "sim":
        from mrp.providers.sim import SimProvider

        return {n: SimProvider(name=n, seed=i) for i, n in enumerate(sorted(names))}
    from mrp.providers.litellm_provider import LiteLLMProvider

    return {
        n: LiteLLMProvider(n, api_base=os.environ.get(f"MRP_{n.upper().replace('-', '_')}_API_BASE"))
        for n in names
    }


def create_app(
    settings: Settings,
    providers: Mapping[str, Provider] | None = None,
    kms: secrets.Kms | None = None,
    chaos: Chaos | None = None,
) -> FastAPI:
    catalog = load()
    db = Database(settings.database_url, settings.app_role)
    table = router.QualityTable.load(settings.quality_table)
    health = Health(table)
    ledger.LEASE = ledger.timedelta(seconds=settings.lease_seconds)
    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.start()
        state["redis"] = Redis.from_url(settings.redis_url)
        state["cache"] = cache_mod.Cache(state["redis"])
        state["providers"] = providers or build_providers(settings.providers, catalog)
        state["kms"] = kms or secrets.kms_from_env()
        from arq import create_pool
        from arq.connections import RedisSettings

        state["arq"] = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        yield
        await state["arq"].aclose()
        await state["redis"].aclose()
        await db.close()

    app = FastAPI(title="model router platform", lifespan=lifespan)
    app.state.db, app.state.table, app.state.health, app.state.state = db, table, health, state
    app.state.chaos = chaos or Chaos()

    # ---- auth --------------------------------------------------------------------------------------

    async def workspace(authorization: str = Header("")) -> Workspace:
        token = authorization.removeprefix("Bearer ").strip()
        if not token.startswith("mrp_"):
            raise HTTPException(401, "missing or malformed API key")
        found = await db.authenticate(hashlib.sha256(token.encode()).hexdigest())
        if not found:
            raise HTTPException(401, "invalid or revoked API key")
        return Workspace(*found)

    def admin(x_admin_token: str = Header("")) -> None:
        if not pysecrets.compare_digest(x_admin_token, settings.admin_token):
            raise HTTPException(401, "admin token required")

    @app.exception_handler(ledger.Entitlement)
    async def _ent(_: Request, exc: ledger.Entitlement):
        return JSONResponse({"error": {"type": "entitlement", "message": str(exc)}}, status_code=exc.status)

    @app.exception_handler(ledger.Conflict)
    async def _conf(_: Request, exc: ledger.Conflict):
        return JSONResponse({"error": {"type": "idempotency_conflict", "message": str(exc)}}, status_code=422)

    @app.exception_handler(ledger.InProgress)
    async def _prog(_: Request, exc: ledger.InProgress):
        return JSONResponse(
            {"error": {"type": "in_progress", "message": str(exc)}},
            status_code=409,
            headers={"Retry-After": "2"},
        )

    @app.exception_handler(router.NoRoute)
    async def _noroute(_: Request, exc: router.NoRoute):
        return JSONResponse({"error": {"type": "no_route", "message": str(exc)}}, status_code=422)

    # ---- helpers -----------------------------------------------------------------------------------

    async def customer_key(ws: Workspace, provider: str) -> str | None:
        if "byok" not in catalog.plans[ws.plan].features:
            return None
        async with db.tenant(ws.id) as conn:
            r = await conn.fetchrow("SELECT * FROM mrp.provider_keys WHERE provider = $1", provider)
        if not r:
            return None
        sealed = secrets.Sealed(
            r["kek_version"], r["wrapped_dek"], r["nonce"], r["ciphertext"], r["fingerprint"]
        )
        return secrets.open_sealed(state["kms"], str(ws.id), provider, sealed)

    def response_body(text: str, model: str, comp_in: int, comp_out: int, task: str) -> dict:
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
            ],
            "usage": {
                "prompt_tokens": comp_in,
                "completion_tokens": comp_out,
                "total_tokens": comp_in + comp_out,
            },
            "mrp": {"task": task},
        }

    def plan_route(ws: Workspace, req: ChatRequest, messages: list[dict]) -> router.Route:
        task = router.classify(messages, req.mrp.task, req.response_format)
        if req.mrp.quality not in catalog.quality_targets:
            raise HTTPException(422, f"quality must be one of {sorted(catalog.quality_targets)}")
        target = catalog.quality_targets[req.mrp.quality]
        plan = catalog.plans[ws.plan]
        tin = messages_tokens(messages)
        tout = req.max_tokens or DEFAULT_OUT[task]
        max_cost = (
            min(plan.max_request_cost, req.mrp.max_cost)
            if req.mrp.max_cost is not None
            else plan.max_request_cost
        )
        if req.model != "auto":
            m = catalog.model(req.model) if req.model in catalog.models else None
            if m is None:
                raise HTTPException(404, f"unknown model '{req.model}'")
            fb = [catalog.model(f) for f in req.mrp.fallbacks if f in catalog.models]
            return router.Route(
                task,
                target,
                m,
                fb,
                None,
                m.cost(tin, tout),
                "requested by the caller",
                table.cell(task, m.id).lower,
            )
        allowed = set(catalog.models) - health.excluded(task)
        return router.decide(
            catalog, table, task, target, tin, tout, max_cost, allowed, cascade=not req.stream
        )

    async def call(
        ws: Workspace, usage_id: uuid.UUID, model: Model, messages: list[dict], max_tokens: int
    ) -> tuple[str, Completion]:
        rid = f"mrp-{uuid.uuid4().hex}"
        async with db.tenant(ws.id) as conn:
            await ledger.call_started(conn, ws.id, usage_id, rid, model.provider, model.id)
        provider = state["providers"][model.provider]
        key = await customer_key(ws, model.provider)
        with tracer.start_as_current_span(
            "provider.call", attributes={"mrp.model": model.id, "mrp.provider": model.provider}
        ):
            try:
                comp = await provider.complete(model.upstream, messages, max_tokens, rid, key)
            except ProviderError as e:
                async with db.tenant(ws.id) as conn:
                    await ledger.call_finished(conn, catalog, rid, model.id, "failed", e.charged)
                raise
        if (
            app.state.chaos.crash_after_provider
            and app.state.chaos.rng.random() < app.state.chaos.crash_after_provider
        ):
            app.state.chaos.crashes += 1
            raise RuntimeError("injected crash between the provider's answer and the ledger")
        async with db.tenant(ws.id) as conn:
            await ledger.call_finished(conn, catalog, rid, model.id, "ok", (comp.tokens_in, comp.tokens_out))
        return rid, comp

    # ---- the API -------------------------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/readyz")
    async def readyz():
        async with db.owner() as conn:
            await conn.fetchval("SELECT 1")
        await state["redis"].ping()
        return {"ok": True}

    @app.get("/v1/models")
    async def models(ws: Workspace = Depends(workspace)):
        return {
            "object": "list",
            "data": [{"id": "auto", "object": "model", "owned_by": "router"}]
            + [
                {"id": m.id, "object": "model", "owned_by": m.provider, "open_weight": m.open_weight}
                for m in catalog.models.values()
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat(
        req: ChatRequest, ws: Workspace = Depends(workspace), idempotency_key: str | None = Header(None)
    ):
        t0 = time.perf_counter()
        plan = catalog.plans[ws.plan]
        key = idempotency_key or f"auto-{uuid.uuid4()}"
        rhash = ledger.request_hash(req.model_dump(mode="json"))
        messages = [m.model_dump() for m in req.messages]
        saved = 0
        if req.mrp.compress_history:
            messages, saved = cache_mod.compress(messages, settings.history_budget_tokens)
        with tracer.start_as_current_span("route") as span:
            route = plan_route(ws, req, messages)
            span.set_attributes(
                {
                    "mrp.task": route.task,
                    "mrp.model": route.primary.id,
                    "mrp.first_try": route.first_try.id if route.first_try else "",
                    "mrp.estimated_cost": float(route.estimated_cost),
                }
            )
        max_tokens = req.max_tokens or DEFAULT_OUT[route.task]
        semantic = "semantic_cache" in plan.features
        headers = {
            "x-mrp-task": route.task,
            "x-mrp-route": route.reason,
            "x-mrp-estimated-cost": f"{route.estimated_cost:.8f}",
            "x-mrp-history-tokens-saved-est": str(saved),
        }

        async with db.tenant(ws.id) as conn:
            await ledger.check_entitlements(conn, plan, route.estimated_cost)
            begun = await ledger.begin(
                conn, ws.id, key, rhash, route.task, route.primary.id, route.estimated_cost
            )
        if begun.replay is not None:
            return JSONResponse(begun.replay, headers={**headers, "x-mrp-replayed": "true"})
        if req.temperature == 0 and not req.stream:
            async with db.tenant(ws.id) as conn:
                hit = await state["cache"].get(
                    conn, str(ws.id), route.task, req.mrp.quality, messages, semantic
                )
                if hit:
                    body = response_body(hit.response, hit.model, 0, 0, route.task)
                    billed = await ledger.record_cache_hit(
                        conn,
                        begun.usage_id,
                        ws.id,
                        key,
                        hit.model,
                        hit.cost,
                        route.estimated_cost,
                        catalog,
                        plan,
                        body,
                    )
                    return JSONResponse(
                        body,
                        headers={
                            **headers,
                            "x-mrp-cache": hit.kind,
                            "x-mrp-model": hit.model,
                            "x-mrp-billed": f"{billed:.8f}",
                            "x-mrp-actual-cost": "0",
                        },
                    )
        if req.stream:
            return StreamingResponse(
                stream(ws, plan, route, messages, max_tokens, begun.usage_id, key, t0),
                media_type="text/event-stream",
                headers=headers,
            )

        order: list[tuple[Model, str]] = (
            ([(route.first_try, "first_try")] if route.first_try else [])
            + [(route.primary, "primary")]
            + [(m, "fallback") for m in route.fallbacks]
        )
        escalated = fallback = False
        delivered: tuple[str, Completion, Model] | None = None
        last_error = ""
        fatal = False
        for model, role in order:
            if fatal:
                break
            if role == "fallback" and delivered is None:
                fallback = True
            for attempt in range(settings.retries + 1):
                try:
                    rid, comp = await call(ws, begun.usage_id, model, messages, max_tokens)
                except ProviderError as e:
                    last_error = str(e)
                    if not e.retryable:  # the request itself is wrong: another model will not help
                        fatal = True
                        break
                    await asyncio.sleep(min(0.05 * 2**attempt, 0.5) if settings.retries else 0)
                    continue
                passed = router.check(route.task, comp.text)
                if passed is not None and health.observe(route.task, model.id, passed):
                    log.warning(
                        "model %s taken out of rotation for %s: check pass rate below expectation",
                        model.id,
                        route.task,
                    )
                if passed is False and model is not order[-1][0]:
                    # a visibly wrong answer (bad JSON, unknown label, code that does not compile):
                    # escalate to the next model; the last one's answer is returned whatever it is
                    escalated = True
                    break
                delivered = (rid, comp, model)
                break
            if delivered:
                break
        latency = int((time.perf_counter() - t0) * 1000)
        if delivered is None:
            async with db.tenant(ws.id) as conn:
                await ledger.settle(
                    conn,
                    begun.usage_id,
                    plan,
                    None,
                    "failed",
                    None,
                    key,
                    ws.id,
                    latency,
                    fallback,
                    escalated,
                    last_error[:300],
                )
            raise HTTPException(502, f"no provider could answer: {last_error}")
        rid, comp, model = delivered
        body = response_body(comp.text, model.id, comp.tokens_in, comp.tokens_out, route.task)
        async with db.tenant(ws.id) as conn:
            billed = await ledger.settle(
                conn, begun.usage_id, plan, rid, "ok", body, key, ws.id, latency, fallback, escalated
            )
            actual = await conn.fetchval("SELECT actual_cost FROM mrp.usage WHERE id = $1", begun.usage_id)
            if req.temperature == 0 and router.check(route.task, comp.text) is not False:
                await state["cache"].put(
                    conn,
                    str(ws.id),
                    route.task,
                    req.mrp.quality,
                    messages,
                    comp.text,
                    model.id,
                    model.cost(comp.tokens_in, comp.tokens_out),
                    semantic,
                )
        log.info(
            "chat ws=%s task=%s model=%s escalated=%s fallback=%s actual=%s billed=%s ms=%s",
            ws.id,
            route.task,
            model.id,
            escalated,
            fallback,
            actual,
            billed,
            latency,
        )
        return JSONResponse(
            body,
            headers={
                **headers,
                "x-mrp-model": model.id,
                "x-mrp-actual-cost": f"{actual:.8f}",
                "x-mrp-billed": f"{billed:.8f}",
                "x-mrp-escalated": str(escalated).lower(),
                "x-mrp-fallback": str(fallback).lower(),
                "x-mrp-cache": "miss",
            },
        )

    async def stream(
        ws: Workspace,
        plan,
        route: router.Route,
        messages: list[dict],
        max_tokens: int,
        usage_id: uuid.UUID,
        key: str,
        t0: float,
    ) -> AsyncIterator[str]:
        started = fallback = False
        delivered_rid: str | None = None
        current_rid: str | None = None
        text: list[str] = []
        model = route.primary
        status, error = "failed", None
        try:
            for i, model in enumerate([route.primary, *route.fallbacks]):
                fallback = i > 0
                current_rid = f"mrp-{uuid.uuid4().hex}"
                async with db.tenant(ws.id) as conn:
                    await ledger.call_started(conn, ws.id, usage_id, current_rid, model.provider, model.id)
                provider = state["providers"][model.provider]
                try:
                    comp: Completion | None = None
                    async for part in provider.stream(
                        model.upstream,
                        messages,
                        max_tokens,
                        current_rid,
                        await customer_key(ws, model.provider),
                    ):
                        if isinstance(part, Delta):
                            started = True
                            text.append(part.text)
                            chunk = {
                                "id": key,
                                "object": "chat.completion.chunk",
                                "model": model.id,
                                "choices": [{"index": 0, "delta": {"content": part.text}}],
                            }
                            yield f"data: {json.dumps(chunk)}\n\n"
                        else:
                            comp = part
                    async with db.tenant(ws.id) as conn:
                        await ledger.call_finished(
                            conn,
                            catalog,
                            current_rid,
                            model.id,
                            "ok",
                            (comp.tokens_in, comp.tokens_out) if comp else None,
                        )
                    delivered_rid, status = current_rid, "ok"
                    break
                except ProviderError as e:
                    async with db.tenant(ws.id) as conn:
                        await ledger.call_finished(conn, catalog, current_rid, model.id, "failed", e.charged)
                    error = str(e)
                    if started:  # the client has part of an answer: never splice in another model
                        yield f"data: {json.dumps({'error': {'type': 'provider_error', 'message': error}})}\n\n"
                        break
            if status == "ok":
                yield "data: [DONE]\n\n"
            else:
                yield f"data: {json.dumps({'error': {'type': 'no_provider', 'message': error}})}\n\n"
        finally:
            # also runs when the client disconnects (the task is cancelled): settle what was used
            with anyio.CancelScope(shield=True):
                latency = int((time.perf_counter() - t0) * 1000)
                async with db.tenant(ws.id) as conn:
                    if status == "ok" and delivered_rid:
                        body = response_body("".join(text), model.id, 0, 0, route.task)
                        await ledger.settle(
                            conn, usage_id, plan, delivered_rid, "ok", body, key, ws.id, latency, fallback
                        )
                    elif started and current_rid:
                        # cut off mid-stream: the provider bills what it generated; the cost arrives via reconciliation
                        await conn.execute(
                            "UPDATE mrp.provider_calls SET status = 'cancelled' WHERE provider_request_id = $1 AND cost IS NULL",
                            current_rid,
                        )
                        await ledger.settle(
                            conn,
                            usage_id,
                            plan,
                            current_rid,
                            "cancelled",
                            None,
                            key,
                            ws.id,
                            latency,
                            fallback,
                            error="client disconnected or stream failed",
                        )
                    else:
                        await ledger.settle(
                            conn,
                            usage_id,
                            plan,
                            None,
                            "failed",
                            None,
                            key,
                            ws.id,
                            latency,
                            fallback,
                            error=error,
                        )

    @app.get("/v1/usage")
    async def usage(month: str, ws: Workspace = Depends(workspace)):
        async with db.tenant(ws.id) as conn:
            return await ledger.usage_report(conn, month)

    @app.put("/v1/provider-keys/{provider}")
    async def put_key(provider: str, body: ProviderKeyIn, ws: Workspace = Depends(workspace)):
        if "byok" not in catalog.plans[ws.plan].features:
            raise HTTPException(403, f"the {ws.plan} plan does not include bring-your-own-key")
        if provider not in {m.provider for m in catalog.models.values()}:
            raise HTTPException(404, "unknown provider")
        s = secrets.seal(state["kms"], str(ws.id), provider, body.api_key)
        async with db.tenant(ws.id) as conn:
            await conn.execute(
                "INSERT INTO mrp.provider_keys (workspace_id, provider, kek_version, wrapped_dek, nonce, ciphertext, fingerprint) VALUES ($1, $2, $3, $4, $5, $6, $7) "
                "ON CONFLICT (workspace_id, provider) DO UPDATE SET kek_version = EXCLUDED.kek_version, wrapped_dek = EXCLUDED.wrapped_dek, nonce = EXCLUDED.nonce, "
                "ciphertext = EXCLUDED.ciphertext, fingerprint = EXCLUDED.fingerprint, created_at = now()",
                ws.id, provider, s.kek_version, s.wrapped_dek, s.nonce, s.ciphertext, s.fingerprint,
            )  # fmt: skip
        return {"provider": provider, "fingerprint": s.fingerprint, "kek_version": s.kek_version}

    @app.get("/v1/provider-keys")
    async def list_keys(ws: Workspace = Depends(workspace)):
        async with db.tenant(ws.id) as conn:
            rows = await conn.fetch(
                "SELECT provider, fingerprint, kek_version, created_at FROM mrp.provider_keys ORDER BY provider"
            )
        return [dict(r) for r in rows]

    @app.delete("/v1/provider-keys/{provider}")
    async def delete_key(provider: str, ws: Workspace = Depends(workspace)):
        async with db.tenant(ws.id) as conn:
            n = await conn.execute("DELETE FROM mrp.provider_keys WHERE provider = $1", provider)
        return {"deleted": n.endswith("1")}

    # ---- agent runs ----------------------------------------------------------------------------------

    @app.post("/v1/agents/runs", status_code=202)
    async def create_run(body: RunIn, ws: Workspace = Depends(workspace)):
        if "agents" not in catalog.plans[ws.plan].features:
            raise HTTPException(403, f"the {ws.plan} plan does not include agent runs")
        for st in body.steps:
            if (
                st.get("kind") not in ("model", "tool")
                or (st["kind"] == "model" and "prompt" not in st)
                or (st["kind"] == "tool" and "name" not in st)
            ):
                raise HTTPException(422, "each step is {kind: model, prompt} or {kind: tool, name, args}")
        from mrp.agents import Runner

        runner = Runner(db, catalog, table, state["providers"], connectors=None)
        run_id = await runner.create(ws.id, body.goal, body.steps, body.cost_limit)
        await state["arq"].enqueue_job("run_agent", str(ws.id), ws.plan, str(run_id), _job_id=f"{run_id}:0")
        return {"run_id": str(run_id), "status": "queued"}

    @app.get("/v1/agents/runs/{run_id}")
    async def get_run(run_id: uuid.UUID, ws: Workspace = Depends(workspace)):
        async with db.tenant(ws.id) as conn:
            run = await conn.fetchrow(
                "SELECT id, goal, status, cost_limit, cost_spent, next_step, result, created_at, updated_at FROM mrp.agent_runs WHERE id = $1",
                run_id,
            )
            if run is None:
                raise HTTPException(404, "no such run")
            steps = await conn.fetch(
                "SELECT step_no, kind, name, status, cost, decided_by, output FROM mrp.run_steps WHERE run_id = $1 ORDER BY step_no",
                run_id,
            )
        return {
            **{k: (str(v) if isinstance(v, (Decimal, uuid.UUID)) else v) for k, v in dict(run).items()},
            "steps": [{**dict(r), "cost": str(r["cost"])} for r in steps],
        }

    @app.post("/v1/agents/runs/{run_id}/steps/{step_no}/{decision}")
    async def decide_step(
        run_id: uuid.UUID,
        step_no: int,
        decision: str,
        ws: Workspace = Depends(workspace),
        x_mrp_user: str = Header("api-key"),
    ):
        if decision not in ("approve", "reject"):
            raise HTTPException(404, "approve or reject")
        from mrp.agents import Runner

        try:
            await Runner(db, catalog, table, state["providers"], connectors=None).decide(
                ws.id, run_id, step_no, decision == "approve", x_mrp_user
            )
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        await state["arq"].enqueue_job(
            "run_agent", str(ws.id), ws.plan, str(run_id), _job_id=f"{run_id}:{step_no}:{decision}"
        )
        return {"run_id": str(run_id), "step": step_no, "decision": decision}

    # ---- admin -------------------------------------------------------------------------------------

    @app.post("/admin/workspaces", dependencies=[Depends(admin)])
    async def create_workspace(body: WorkspaceIn):
        if body.plan not in catalog.plans:
            raise HTTPException(422, "unknown plan")
        wid = uuid.uuid4()
        api_key = "mrp_" + pysecrets.token_urlsafe(32)
        async with db.owner() as conn:
            await conn.execute(
                "INSERT INTO mrp.workspaces (id, name, plan) VALUES ($1, $2, $3)", wid, body.name, body.plan
            )
            await conn.execute(
                "INSERT INTO mrp.api_keys (id, workspace_id, key_hash, prefix) VALUES ($1, $2, $3, $4)",
                uuid.uuid4(), wid, hashlib.sha256(api_key.encode()).hexdigest(), api_key[:8],
            )  # fmt: skip
        return {"workspace_id": str(wid), "api_key": api_key, "note": "the key is shown once"}

    @app.post("/admin/reconcile/calls", dependencies=[Depends(admin)])
    async def reconcile_calls(older_than_s: int = 300):
        return await reconcile.settle_unknown_calls(
            db, catalog, state["providers"], ledger.timedelta(seconds=older_than_s)
        )

    @app.post("/admin/reconcile/invoice/{provider}", dependencies=[Depends(admin)])
    async def reconcile_invoice(provider: str, request: Request, start: str, end: str):
        lines = reconcile.parse_invoice_csv(provider, (await request.body()).decode())
        rep = await reconcile.reconcile_invoice(
            db, provider, lines, ledger.date.fromisoformat(start), ledger.date.fromisoformat(end)
        )
        return {
            **rep.summary(),
            "details": {
                "charged_not_in_ledger": rep.charged_not_in_ledger[:100],
                "in_ledger_not_charged": rep.in_ledger_not_charged[:100],
                "amount_mismatch": rep.amount_mismatch[:100],
            },
        }

    @app.post("/admin/kek/rotate", dependencies=[Depends(admin)])
    async def rotate():
        """Re-wraps every customer data key with the current key-encryption key."""
        kms_ = state["kms"]
        async with db.owner() as conn:
            rows = await conn.fetch(
                "SELECT workspace_id, provider, kek_version, wrapped_dek, nonce, ciphertext, fingerprint FROM mrp.provider_keys"
            )
            n = 0
            for r in rows:
                if r["kek_version"] == kms_.current_version:
                    continue
                s = secrets.rewrap(
                    kms_,
                    str(r["workspace_id"]),
                    r["provider"],
                    secrets.Sealed(
                        r["kek_version"], r["wrapped_dek"], r["nonce"], r["ciphertext"], r["fingerprint"]
                    ),
                )
                await conn.execute(
                    "UPDATE mrp.provider_keys SET kek_version = $3, wrapped_dek = $4 WHERE workspace_id = $1 AND provider = $2",
                    r["workspace_id"], r["provider"], s.kek_version, s.wrapped_dek,
                )  # fmt: skip
                n += 1
        return {"rewrapped": n, "kek_version": kms_.current_version}

    return app


def estimated(text: str) -> int:  # re-exported for callers that only need an estimate
    return estimate_tokens(text)


__all__ = ["Chaos", "Settings", "create_app", "Decimal"]
