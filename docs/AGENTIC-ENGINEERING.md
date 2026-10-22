# Agentic engineering in this repository

This repository was built with a coding agent (Claude Code) doing most of the typing, and with me
deciding the design, reading every change and owning what ships. The harness is what makes that
safe and fast.

## The harness

| Piece | What it gives an agent |
|---|---|
| `AGENTS.md` | the four invariants, conventions, and a review checklist, in one page |
| `pytest -m invariant` | the invariants as tests: tenant isolation, billing exactness, secrets, agent effects. CI runs them first; an agent may not weaken them |
| `scripts/check.sh [--fast]` | one command for the whole loop: format, lint, types, invariants, tests |
| `scripts/worktree.sh <task>` | a worktree, branch, test database and Redis index per task, so parallel agents never share state |
| `.claude/commands/task.md` | the task template: restate, test first, small steps, measure, report what was not done |
| `mrp evaluate` + `mrp gate` | numbers instead of opinions for routing, caching, billing and overhead; CI fails a change that makes them worse |

## What the loop caught while building this

Each of these was found by a test or an evaluation, not by reading:

- **The pool could drop row-level security.** asyncpg resets a released connection with `RESET ALL`,
  which also resets `SET ROLE`; setting the app role only when a connection is created would have
  let later requests run as the owner and see every workspace. The role is now applied on every
  acquire, and a test checks `current_user` across 25 acquisitions.
- **A cache hit could bypass idempotency.** A retried request was answered from the cache with a
  new billing row instead of replaying the first answer. Idempotency now runs before the cache.
- **Billing rounding disagreed between Python and SQL.** The billing chaos run found two cut-off
  streams billed one hundred-millionth of a euro differently: Python rounded half to even,
  PostgreSQL half up. Money now uses one rule everywhere (`ledger.CENT8`, half up).
- **The health monitor took healthy models out of rotation.** The load test showed a model
  excluded with nothing wrong: a 95% test repeated after every answer fires by chance. The test is
  now z = 3.1 with a 100-answer minimum, and the evaluation checks 6,000 healthy requests for false
  exclusions.
- **Untyped numeric parameters in SQL.** `round($2 * (1 + $3), 8)` let PostgreSQL infer integers and
  billed a cut-off stream zero. Parameters in arithmetic are now cast explicitly.
- **The first semantic-cache guard was poor.** A guard comparing numbers and capitalised names served
  only 38.5% of repeats and still served 12% of near misses (lower-case text defeated it). Comparing
  normalised content words instead serves 90.8% of repeats and no near misses on the same pairs.

## Review rules

Agent-written code merges when a person can answer the review checklist in `AGENTS.md` for it. In
practice that means reading every SQL statement and every place money or a workspace id is handled,
and being able to explain each test that guards it.
