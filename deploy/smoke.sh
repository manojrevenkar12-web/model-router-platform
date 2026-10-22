#!/usr/bin/env bash
# Brings the stack up and walks the main paths: workspace, routed request, cache hit, idempotent
# retry, streaming, an agent run through the worker, the usage report.
set -euo pipefail
cd "$(dirname "$0")"
docker compose up -d --build
for _ in $(seq 1 90); do curl -fs localhost:8000/healthz >/dev/null && break; sleep 1; done
curl -fs localhost:8000/readyz
WS=$(curl -fs -X POST localhost:8000/admin/workspaces -H "X-Admin-Token: local-admin-token" -H 'content-type: application/json' -d '{"name":"smoke","plan":"team"}')
KEY=$(echo "$WS" | python3 -c "import sys, json; print(json.load(sys.stdin)['api_key'])")
AUTH="Authorization: Bearer $KEY"
REQ='{"model":"auto","temperature":0,"messages":[{"role":"user","content":"Extract the invoice id and amount as JSON: INV-9, 120.50 EUR"}]}'
curl -fs -D /tmp/h1 -o /dev/null localhost:8000/v1/chat/completions -H "$AUTH" -H 'content-type: application/json' -H 'Idempotency-Key: s-1' -d "$REQ"
grep -qi "x-mrp-model" /tmp/h1
curl -fs -D /tmp/h2 -o /dev/null localhost:8000/v1/chat/completions -H "$AUTH" -H 'content-type: application/json' -H 'Idempotency-Key: s-1' -d "$REQ"
grep -qi "x-mrp-replayed: true" /tmp/h2
curl -fs -D /tmp/h3 -o /dev/null localhost:8000/v1/chat/completions -H "$AUTH" -H 'content-type: application/json' -d "$REQ"
grep -qi "x-mrp-cache: exact" /tmp/h3
curl -fsN localhost:8000/v1/chat/completions -H "$AUTH" -H 'content-type: application/json' \
  -d '{"model":"auto","stream":true,"messages":[{"role":"user","content":"Tell me about Berlin"}]}' | grep -q "\[DONE\]"
RUN=$(curl -fs -X POST localhost:8000/v1/agents/runs -H "$AUTH" -H 'content-type: application/json' \
  -d '{"goal":"post","steps":[{"kind":"model","prompt":"Summarize the week","task":"summarization"},{"kind":"tool","name":"slack.post","args":{"text":"hi"}}]}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['run_id'])")
for _ in $(seq 1 30); do
  S=$(curl -fs localhost:8000/v1/agents/runs/$RUN -H "$AUTH" | python3 -c "import sys, json; print(json.load(sys.stdin)['status'])")
  [ "$S" = "succeeded" ] && break; sleep 1
done
[ "$S" = "succeeded" ] || { echo "agent run status: $S"; exit 1; }
curl -fs "localhost:8000/v1/usage?month=$(date +%Y-%m)" -H "$AUTH" | grep -q actual_cost
echo "smoke test passed"
