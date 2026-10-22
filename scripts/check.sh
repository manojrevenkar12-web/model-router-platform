#!/usr/bin/env bash
# The feedback loop for people and coding agents: fail fast, in order of cost.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env.agent ] && source .env.agent
start=$(date +%s)
step() { printf '\n== %s\n' "$1"; }
step "format";     python3 -m ruff format --check .
step "lint";       python3 -m ruff check .
step "types";      python3 -m mypy
if [ "${1:-}" = "--fast" ]; then
  step "unit tests (no database)"; python3 -m pytest -q tests/test_router.py tests/test_litellm.py -k "not env" -p no:warnings
else
  step "invariants"; python3 -m pytest -q -m invariant -x -p no:warnings
  step "all tests";  python3 -m pytest -q -m "not invariant" -p no:warnings
fi
printf '\nall checks passed in %ss\n' "$(( $(date +%s) - start ))"
