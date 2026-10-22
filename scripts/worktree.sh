#!/usr/bin/env bash
# Creates an isolated worktree for one task: its own branch, its own test database and Redis index,
# so parallel agents can run the full test suite at the same time.
#   scripts/worktree.sh billing-refunds       -> ../mrp-billing-refunds on branch agent/billing-refunds
set -euo pipefail
name=${1:?usage: scripts/worktree.sh <task-name>}
root=$(git rev-parse --show-toplevel)
dir="$root/../mrp-$name"
git -C "$root" worktree add -b "agent/$name" "$dir"
base=${MRP_TEST_DATABASE_URL:-postgresql://postgres:postgres@localhost:5432/mrp_test}
db="mrp_test_${name//[^a-zA-Z0-9]/_}"
admin="${base%/*}/postgres"
psql "$admin" -tc "SELECT 1 FROM pg_database WHERE datname = '$db'" | grep -q 1 || psql "$admin" -c "CREATE DATABASE $db"
psql "${base%/*}/$db" -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null
index=$(( $(printf '%s' "$name" | cksum | cut -d' ' -f1) % 14 + 1 ))   # Redis databases 1..14
cat > "$dir/.env.agent" <<ENV
export MRP_TEST_DATABASE_URL=${base%/*}/$db
export MRP_TEST_REDIS_URL=redis://localhost:6379/$index
ENV
echo "worktree: $dir (branch agent/$name, database $db, redis db $index)"
echo "next: cd $dir && source .env.agent && scripts/check.sh"
