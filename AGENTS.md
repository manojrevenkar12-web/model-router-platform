# Working on this repository (for people and coding agents)

This file is the context a coding agent needs before touching the code. Keep it short and true.

## What must never break

These are tested by `pytest -m invariant` and CI runs them first. Never weaken, skip or delete one
to make a change pass; if a change seems to need it, stop and ask.

1. **Tenant isolation**: every workspace table has row-level security; the request path uses
   `db.tenant(workspace_id)`; the app role (`mrp_app`) is re-applied on every pool acquire.
   Never query workspace tables through `db.owner()` on the request path.
2. **Billing exactness**: one usage row per idempotency key; every provider call is recorded
   *before* it is made; customers pay only for the call that produced their answer. Estimates
   (`estimated_cost`, `avoided_cost_est`) are never added to spend (`actual_cost`).
3. **Secrets and content stay out of logs and traces**: log ids, counts, costs and timings, never
   messages or keys. Customer keys are sealed with workspace-bound associated data.
4. **Agent effects happen once**: tool calls carry `run_id:step` as the idempotency key; checkpoints
   commit output and the next step together.

## Fast feedback

```bash
scripts/check.sh          # format, lint, types, invariants, all tests (one to two minutes)
scripts/check.sh --fast   # format, lint, types and the tests that need no database (under a minute)
```

Tests need PostgreSQL with pgvector and Redis: set `MRP_TEST_DATABASE_URL` / `MRP_TEST_REDIS_URL`,
or have Docker running and testcontainers starts them.

## Working in parallel

`scripts/worktree.sh <name>` creates a git worktree on a new branch with its **own test database
and Redis index**, so several agents (or people) can run the full suite at the same time without
corrupting each other's state. Each worktree gets an `.env.agent` to source.

## Conventions

- Python 3.11+, async throughout the request path, typed (`mypy` is clean and stays clean).
- SQL lives next to the code that uses it; schema changes are new files in `src/mrp/sql/` and must
  be idempotent (`IF NOT EXISTS`), because every replica runs migrations at start under a lock.
- Money is `Decimal`, 8 places, rounded half up in Python and SQL alike (`ledger.CENT8`).
- A change to routing, caching or billing reruns `mrp evaluate` and states the measured effect in
  the pull request; `mrp gate` must pass.
- Comments explain *why*. Docstrings at the top of each module say what it guarantees.

## Review checklist (for whoever merges agent-written code)

- Does any query on the request path bypass `db.tenant`? Any new table without RLS?
- Could a retry, crash or disconnect at any line produce a second charge, a lost charge or a
  second effect? Where is that tested?
- Does anything log or trace a message, a completion or a key?
- Is a new number in the README produced by `mrp evaluate`, not typed in?
