"""Agent runs that survive interruptions, stay within budget and wait for people when they must.

A run is a sequence of steps (model calls and tool calls such as sending an email or posting to
Slack) executed by a worker. The guarantees:

- **Checkpoints.** After each step its output and the next step number are committed together.
  A worker that dies mid-run is replaced by another once its lease expires, and the run continues
  from the last checkpoint.
- **Effects happen once.** Every tool call carries the idempotency key run_id:step. A step
  repeated after a crash returns the earlier result instead of sending the email twice. Model steps
  go through the usage ledger with the same kind of key, so they are never billed twice either.
- **Cost limits.** Before a model step, its estimated cost plus what the run has spent must fit
  the run's limit, or the run stops as budget_exhausted.
- **Human approval.** Tools with external effects (email to people outside the company) pause the
  run until someone approves or rejects the exact call; the decision is recorded on the step.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from mrp import ledger, router
from mrp.catalog import Catalog, messages_tokens
from mrp.db import Database
from mrp.providers.base import Provider, ProviderError

LEASE_SECONDS = 30


class Connector(Protocol):
    async def call(self, name: str, args: dict, idempotency_key: str) -> dict: ...


@dataclass
class FakeConnectors:
    """Gmail, Slack and calendar stand-ins with the property real connectors must have: an effect
    is applied once per idempotency key. (Real connectors pass the key to the API where it has one,
    and otherwise record it before acting.)"""

    effects: list[tuple[str, dict]] = field(default_factory=list)
    done: dict[str, dict] = field(default_factory=dict)
    fail_once: set[str] = field(default_factory=set)  # tool names that fail on their first call

    async def call(self, name: str, args: dict, idempotency_key: str) -> dict:
        if idempotency_key in self.done:
            return self.done[idempotency_key]
        if name in self.fail_once:
            self.fail_once.discard(name)
            raise ConnectionError(f"{name}: upstream unavailable")
        if name not in {"gmail.send", "slack.post", "calendar.create"}:
            raise ValueError(f"unknown tool {name}")
        self.effects.append((name, args))
        result = {"ok": True, "id": f"{name}-{len(self.effects)}"}
        self.done[idempotency_key] = result
        return result


def needs_approval(name: str, args: dict, internal_domains: tuple[str, ...] = ("example.com",)) -> bool:
    if name == "gmail.send":
        return any(
            not str(to).endswith(tuple("@" + d for d in internal_domains)) for to in args.get("to", [])
        )
    if name == "calendar.create":
        return len(args.get("attendees", [])) > 10
    return False


class Crash(Exception):
    """Injected by tests to simulate a worker dying at a given point."""


@dataclass
class Runner:
    db: Database
    catalog: Catalog
    table: router.QualityTable
    providers: Mapping[str, Provider]
    connectors: Connector | None  # None for API processes that only create runs and record decisions
    worker_id: str = field(default_factory=lambda: f"worker-{uuid.uuid4().hex[:8]}")
    crash_at: tuple[int, str] | None = None  # (step, "before_effect" | "after_effect") for tests

    async def create(
        self, workspace_id: uuid.UUID, goal: str, steps: list[dict], cost_limit: Decimal
    ) -> uuid.UUID:
        run_id = uuid.uuid4()
        async with self.db.tenant(workspace_id) as conn:
            await conn.execute(
                "INSERT INTO mrp.agent_runs (id, workspace_id, goal, status, cost_limit, result) VALUES ($1, $2, $3, 'queued', $4, $5)",
                run_id,
                workspace_id,
                goal,
                cost_limit,
                {"plan": steps},
            )
        return run_id

    async def _lease(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> dict | None:
        async with self.db.tenant(workspace_id) as conn:
            row = await conn.fetchrow(
                "UPDATE mrp.agent_runs SET status = 'running', lease_owner = $2, lease_until = now() + make_interval(secs => $3), updated_at = now() "
                "WHERE id = $1 AND status IN ('queued', 'running') AND (lease_until IS NULL OR lease_until < now() OR lease_owner = $2) RETURNING *",
                run_id,
                self.worker_id,
                LEASE_SECONDS,
            )
        return dict(row) if row else None

    async def _checkpoint(
        self,
        workspace_id,
        run_id,
        step_no,
        kind,
        name,
        inp,
        out,
        cost: Decimal,
        status="done",
        decided_by=None,
    ) -> None:
        async with self.db.tenant(workspace_id) as conn:
            await conn.execute(
                "INSERT INTO mrp.run_steps (run_id, workspace_id, step_no, kind, name, input, output, status, cost, decided_by) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) ON CONFLICT (run_id, step_no) DO UPDATE SET output = EXCLUDED.output, status = EXCLUDED.status, cost = EXCLUDED.cost",
                run_id, workspace_id, step_no, kind, name, inp, out, status, cost, decided_by,
            )  # fmt: skip
            await conn.execute(
                "UPDATE mrp.agent_runs SET next_step = $2, cost_spent = cost_spent + $3, lease_until = now() + make_interval(secs => $4), updated_at = now() WHERE id = $1",
                run_id,
                step_no + 1,
                cost,
                LEASE_SECONDS,
            )

    async def _finish(self, workspace_id, run_id, status: str, result: dict | None = None) -> None:
        async with self.db.tenant(workspace_id) as conn:
            await conn.execute(
                "UPDATE mrp.agent_runs SET status = $2, lease_owner = NULL, lease_until = NULL, updated_at = now(), "
                "result = coalesce(result, '{}'::jsonb) || $3::jsonb WHERE id = $1",
                run_id,
                status,
                result or {},
            )

    async def _model_step(
        self, workspace_id: uuid.UUID, plan_name: str, run: dict, step_no: int, step: dict
    ) -> tuple[dict, Decimal] | None:
        plan = self.catalog.plans[plan_name]
        messages = [{"role": "user", "content": step["prompt"]}]
        task = router.classify(messages, step.get("task"))
        tin, tout = messages_tokens(messages), step.get("max_tokens", 400)
        remaining = Decimal(run["cost_limit"]) - Decimal(run["cost_spent"])
        try:
            route = router.decide(
                self.catalog,
                self.table,
                task,
                self.catalog.quality_targets[step.get("quality", "standard")],
                tin,
                tout,
                min(remaining, plan.max_request_cost),
                cascade=False,
            )
        except router.NoRoute:
            return None  # nothing fits in what is left of the run's budget
        if route.estimated_cost > remaining:
            return None
        key = f"run:{run['id']}:{step_no}"
        async with self.db.tenant(workspace_id) as conn:
            begun = await ledger.begin(
                conn,
                workspace_id,
                key,
                ledger.request_hash(step),
                task,
                route.primary.id,
                route.estimated_cost,
            )
        if begun.replay is not None:  # the step ran before a crash: reuse its answer, no new charge
            cost = Decimal(begun.replay.get("mrp", {}).get("actual_cost", "0"))
            return begun.replay, cost
        last = ""
        for model in [route.primary, *route.fallbacks]:
            rid = f"mrp-{uuid.uuid4().hex}"
            async with self.db.tenant(workspace_id) as conn:
                await ledger.call_started(conn, workspace_id, begun.usage_id, rid, model.provider, model.id)
            try:
                comp = await self.providers[model.provider].complete(model.upstream, messages, tout, rid)
            except ProviderError as e:
                last = str(e)
                async with self.db.tenant(workspace_id) as conn:
                    await ledger.call_finished(conn, self.catalog, rid, model.id, "failed", e.charged)
                if e.retryable:
                    continue
                break
            async with self.db.tenant(workspace_id) as conn:
                await ledger.call_finished(
                    conn, self.catalog, rid, model.id, "ok", (comp.tokens_in, comp.tokens_out)
                )
                actual = await conn.fetchval(
                    "SELECT coalesce(sum(cost), 0) FROM mrp.provider_calls WHERE usage_id = $1",
                    begun.usage_id,
                )
                body = {"text": comp.text, "model": model.id, "mrp": {"actual_cost": str(actual)}}
                await ledger.settle(conn, begun.usage_id, plan, rid, "ok", body, key, workspace_id, 0)
            return body, Decimal(actual)
        raise ProviderError(f"model step failed: {last}", retryable=True)

    async def run(self, workspace_id: uuid.UUID, plan_name: str, run_id: uuid.UUID) -> str:
        """Executes a run from its last checkpoint; returns its status afterwards."""
        run = await self._lease(workspace_id, run_id)
        if run is None:
            return "not_runnable"
        steps: list[dict[str, Any]] = run["result"]["plan"]
        step_no = run["next_step"]
        while step_no < len(steps):
            step = steps[step_no]
            if step["kind"] == "model":
                out = await self._model_step(workspace_id, plan_name, run, step_no, step)
                if out is None:
                    await self._finish(workspace_id, run_id, "budget_exhausted", {"stopped_at": step_no})
                    return "budget_exhausted"
                body, cost = out
                await self._checkpoint(
                    workspace_id,
                    run_id,
                    step_no,
                    "model",
                    step.get("task", "model"),
                    step,
                    {
                        "text": body.get("text")
                        or body.get("choices", [{}])[0].get("message", {}).get("content")
                    },
                    cost,
                )
                run["cost_spent"] = Decimal(run["cost_spent"]) + cost
            else:
                name, args = step["name"], step.get("args", {})
                if needs_approval(name, args):
                    async with self.db.tenant(workspace_id) as conn:
                        decision = await conn.fetchrow(
                            "SELECT status, decided_by FROM mrp.run_steps WHERE run_id = $1 AND step_no = $2",
                            run_id,
                            step_no,
                        )
                    if decision is None or decision["status"] == "waiting_approval":
                        if decision is None:
                            await self._checkpoint_wait(workspace_id, run_id, step_no, name, step)
                        await self._finish(workspace_id, run_id, "waiting_approval", {"waiting_on": step_no})
                        return "waiting_approval"
                    if decision["status"] == "rejected":
                        await self._finish(
                            workspace_id,
                            run_id,
                            "rejected",
                            {"rejected_step": step_no, "by": decision["decided_by"]},
                        )
                        return "rejected"
                if self.crash_at == (step_no, "before_effect"):
                    self.crash_at = None
                    raise Crash("before effect")
                assert self.connectors is not None, "a worker needs connectors to run tool steps"
                result = await self.connectors.call(name, args, f"{run_id}:{step_no}")
                if self.crash_at == (step_no, "after_effect"):
                    self.crash_at = None
                    raise Crash("after effect, before checkpoint")
                await self._checkpoint(
                    workspace_id, run_id, step_no, "tool", name, step, result, Decimal(0), status="done"
                )
            step_no += 1
        await self._finish(workspace_id, run_id, "succeeded")
        return "succeeded"

    async def _checkpoint_wait(self, workspace_id, run_id, step_no, name, step) -> None:
        async with self.db.tenant(workspace_id) as conn:
            await conn.execute(
                "INSERT INTO mrp.run_steps (run_id, workspace_id, step_no, kind, name, input, status) VALUES ($1, $2, $3, 'tool', $4, $5, 'waiting_approval') ON CONFLICT DO NOTHING",
                run_id,
                workspace_id,
                step_no,
                name,
                step,
            )

    async def decide(
        self, workspace_id: uuid.UUID, run_id: uuid.UUID, step_no: int, approve: bool, who: str
    ) -> None:
        async with self.db.tenant(workspace_id) as conn:
            n = await conn.execute(
                "UPDATE mrp.run_steps SET status = $3, decided_by = $4 WHERE run_id = $1 AND step_no = $2 AND status = 'waiting_approval'",
                run_id,
                step_no,
                "approved" if approve else "rejected",
                who,
            )
            if n.endswith("0"):
                raise ValueError("no approval is pending for this step")
            await conn.execute(
                "UPDATE mrp.agent_runs SET status = 'queued', updated_at = now() WHERE id = $1", run_id
            )
