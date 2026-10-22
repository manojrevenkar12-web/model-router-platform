# Architecture

## The request path

```
client ──▶ auth (key hash → workspace, plan) ──▶ compress history ──▶ classify task ──▶ route
                                                                                          │
     ┌────────────────────────────────────────────────────────────────────────────────────┘
     ▼
 ledger.begin (idempotency key; replay / 409 / take over)  ──▶  cache (exact → semantic + slot check)
     │                                                                   │ hit: settle as cache_hit
     ▼                                                                   ▼
 for model in [cheap first try?, primary, fallbacks]:                 response
     ledger.call_started (committed before the call)
     provider.complete / stream          ── retryable error → retry, then next model
     ledger.call_finished (tokens, actual cost)
     cheap check fails → escalate (and tell the health monitor)
     ▼
 ledger.settle (delivered call, billed = its actual cost × (1 + markup)) ──▶ cache.put ──▶ response
```

Every database step is its own short transaction inside `db.tenant(workspace)`, which sets
`app.workspace_id` for that transaction only; row-level security does the rest.

## Data model

| Table | Holds | Isolation |
|---|---|---|
| `workspaces`, `api_keys` | tenants and hashed keys | owner only; keys checked through `authenticate()` (security definer) |
| `provider_keys` | customers' provider keys, envelope-encrypted | RLS |
| `usage` | one row per client request (idempotency key) | RLS |
| `provider_calls` | every provider call, recorded before it is made | RLS |
| `responses` | answers kept for idempotent replays | RLS |
| `cache_entries` | semantic cache (pgvector) | RLS |
| `agent_runs`, `run_steps` | runs, checkpoints, approvals | RLS |
| `provider_invoice_lines` | imported invoices for reconciliation | owner only |

## Failure modes

| Failure | What happens | Where it is tested |
|---|---|---|
| Provider 5xx / 429 / timeout before the answer | retry with backoff, then the next model; the client sees one answer | `test_gateway`, billing evaluation |
| Provider times out *after* charging | the call's charged tokens are recorded as our cost; not billed | `test_billing`, billing evaluation |
| Gateway dies between the provider's answer and the ledger | the call row exists with unknown cost; the client retries with the same key and the retry takes over; the reconciler fills the lost call's cost from the provider | `test_billing`, billing evaluation (42 crashes) |
| Client retries / duplicates | same key, same body: the stored answer; still running: 409 with Retry-After; same key, different body: 422 | `test_billing` |
| Client disconnects mid-stream | the stream task is cancelled; the ledger is settled under a shielded scope as `cancelled`; the cost arrives via reconciliation and is billed | `test_gateway`, billing evaluation (47 disconnects) |
| A model gets worse | the health monitor sees its check pass rate fall and takes it out of rotation for that task | routing evaluation (drift) |
| Redis down | caches become misses; requests still served | `test_gateway` |
| PostgreSQL down | `/readyz` fails, the pod leaves the load balancer; requests fail fast (the ledger is not optional) | — |
| Worker dies mid-run | the run's lease expires; another worker resumes from the last checkpoint; effects are not repeated | `test_agents` |
| Pooled connection loses its role | the app role is re-applied on every acquire (asyncpg resets connections with `RESET ALL`) | `test_tenancy` |

## Migrations and releases

Migrations are additive and idempotent (expand, then contract in a later release), and run under an
advisory lock at start and in a pre-rollout job. Rolling back means deploying the previous image: the
schema it needs is still there. Destructive changes (dropping a column) ship only after no running
version reads it.

## Not built yet

- Request decomposition (splitting a multi-part request across models) and the fine-tuning pipeline;
  the catalogue has a fine-tuned extraction model as if one had been trained.
- Real Gmail / Slack / calendar connectors (the fakes have the same once-per-key contract), the
  Next.js frontend, Langfuse and Sentry (OpenTelemetry is wired; exporters are configuration).
- A sentence-embedding model for the semantic cache (the hashing embedder needs no download).
