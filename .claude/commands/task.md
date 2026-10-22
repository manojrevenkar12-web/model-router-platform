Implement this task: $ARGUMENTS

1. Read AGENTS.md. Restate the task, the files you expect to touch and which invariants are at risk.
2. Write or extend tests first where behaviour changes; for billing, tenancy, secrets or agent
   effects, add an `invariant` test that would fail without your change.
3. Implement in small steps, running `scripts/check.sh --fast` after each and `scripts/check.sh`
   before you finish.
4. If routing, caching or billing changed: run `mrp evaluate --quick` and report the measured
   effect, including what got worse.
5. Finish with: what changed, what you tested, what you did not do, and anything a reviewer should
   look at first.
