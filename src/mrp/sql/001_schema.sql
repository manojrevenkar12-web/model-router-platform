-- Schema for the model router platform.
--
-- Tenant isolation is enforced by PostgreSQL, not by remembering WHERE clauses: every table that
-- holds workspace data has row-level security keyed on the transaction setting app.workspace_id.
-- The application serves requests as mrp_app, which has no BYPASSRLS, so a query that forgets its
-- workspace filter returns nothing. Only the schema owner (migrations, reconciliation, key rotation)
-- sees across workspaces, and the request path never uses it.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS mrp;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mrp_app') THEN
    CREATE ROLE mrp_app NOLOGIN NOBYPASSRLS;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS mrp.workspaces (
  id          uuid PRIMARY KEY,
  name        text NOT NULL,
  plan        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mrp.api_keys (
  id            uuid PRIMARY KEY,
  workspace_id  uuid NOT NULL REFERENCES mrp.workspaces(id),
  key_hash      text NOT NULL UNIQUE,      -- sha256 of the key; the key itself is shown once
  prefix        text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  revoked_at    timestamptz
);

-- Customers' own provider keys (bring your own key), envelope-encrypted: the secret is sealed with
-- a per-secret data key, the data key is wrapped by the key management service. Both seals bind
-- the workspace and provider as associated data, so a row copied to another workspace cannot be
-- opened there.
CREATE TABLE IF NOT EXISTS mrp.provider_keys (
  workspace_id  uuid NOT NULL REFERENCES mrp.workspaces(id),
  provider      text NOT NULL,
  kek_version   text NOT NULL,
  wrapped_dek   bytea NOT NULL,
  nonce         bytea NOT NULL,
  ciphertext    bytea NOT NULL,
  fingerprint   text NOT NULL,             -- last 4 characters, for people to recognise the key
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, provider)
);

-- The usage ledger. One row per billable attempt, keyed by the client's idempotency key so a
-- retried request can never be billed twice. Estimates and actual spend are separate columns and
-- are never added together.
CREATE TABLE IF NOT EXISTS mrp.usage (
  id                 uuid PRIMARY KEY,
  workspace_id       uuid NOT NULL REFERENCES mrp.workspaces(id),
  idempotency_key    text NOT NULL,
  request_hash       text NOT NULL,         -- the same key with a different body is refused
  status             text NOT NULL,         -- pending, ok, failed, cache_hit
  task               text,
  model              text,
  provider           text,
  provider_request_id text,
  tokens_in          integer NOT NULL DEFAULT 0,
  tokens_out         integer NOT NULL DEFAULT 0,
  estimated_cost     numeric(14,8) NOT NULL DEFAULT 0,   -- before the call, from token estimates
  actual_cost        numeric(14,8),                       -- from the provider's reported usage
  avoided_cost_est   numeric(14,8) NOT NULL DEFAULT 0,   -- cache hits: what the call would have cost (an estimate)
  billed_amount      numeric(14,8),                       -- what the customer is charged
  fallback           boolean NOT NULL DEFAULT false,
  escalated          boolean NOT NULL DEFAULT false,
  latency_ms         integer,
  error              text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  settled_at         timestamptz,
  UNIQUE (workspace_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS usage_ws_time ON mrp.usage (workspace_id, created_at);
CREATE INDEX IF NOT EXISTS usage_pending ON mrp.usage (status, created_at) WHERE status = 'pending';

-- Every call to a provider, including retries, fallbacks and the cheap first try of a cascade.
-- Our actual spend is the sum of these; reconciliation matches them to providers' records.
CREATE TABLE IF NOT EXISTS mrp.provider_calls (
  provider_request_id text PRIMARY KEY,
  usage_id       uuid NOT NULL REFERENCES mrp.usage(id),
  workspace_id   uuid NOT NULL REFERENCES mrp.workspaces(id),
  provider       text NOT NULL,
  model          text NOT NULL,
  status         text NOT NULL,       -- started, ok, failed, abandoned, cancelled
  tokens_in      integer,
  tokens_out     integer,
  cost           numeric(14,8),       -- actual, from the provider's usage; null until known
  delivered      boolean NOT NULL DEFAULT false,  -- this call produced the answer the customer got
  created_at     timestamptz NOT NULL DEFAULT now(),
  settled_at     timestamptz
);
CREATE INDEX IF NOT EXISTS calls_usage ON mrp.provider_calls (usage_id);
CREATE INDEX IF NOT EXISTS calls_unsettled ON mrp.provider_calls (status, created_at) WHERE cost IS NULL;

-- Responses kept for 24 hours so a retried request gets the same answer without a new call.
CREATE TABLE IF NOT EXISTS mrp.responses (
  workspace_id     uuid NOT NULL REFERENCES mrp.workspaces(id),
  idempotency_key  text NOT NULL,
  body             jsonb NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS mrp.cache_entries (
  id            bigserial PRIMARY KEY,
  workspace_id  uuid NOT NULL REFERENCES mrp.workspaces(id),
  task          text NOT NULL,
  quality       text NOT NULL,
  prompt        text NOT NULL,
  slots         jsonb NOT NULL,
  embedding     vector(256) NOT NULL,
  response      text NOT NULL,
  model         text NOT NULL,
  cost          numeric(14,8) NOT NULL,
  hits          integer NOT NULL DEFAULT 0,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cache_ws ON mrp.cache_entries (workspace_id, task, quality);

CREATE TABLE IF NOT EXISTS mrp.agent_runs (
  id            uuid PRIMARY KEY,
  workspace_id  uuid NOT NULL REFERENCES mrp.workspaces(id),
  goal          text NOT NULL,
  status        text NOT NULL,   -- queued, running, waiting_approval, succeeded, failed, budget_exhausted, rejected
  cost_limit    numeric(14,8) NOT NULL,
  cost_spent    numeric(14,8) NOT NULL DEFAULT 0,
  next_step     integer NOT NULL DEFAULT 0,
  lease_until   timestamptz,     -- a worker owns the run until then; another may take over after
  lease_owner   text,
  result        jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mrp.run_steps (
  run_id        uuid NOT NULL REFERENCES mrp.agent_runs(id),
  workspace_id  uuid NOT NULL REFERENCES mrp.workspaces(id),
  step_no       integer NOT NULL,
  kind          text NOT NULL,        -- model, tool
  name          text NOT NULL,
  input         jsonb NOT NULL,
  output        jsonb,
  status        text NOT NULL,        -- done, waiting_approval, approved, rejected
  cost          numeric(14,8) NOT NULL DEFAULT 0,
  decided_by    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, step_no)
);

-- Row-level security on every workspace table.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['api_keys', 'provider_keys', 'usage', 'provider_calls', 'responses', 'cache_entries', 'agent_runs', 'run_steps'] LOOP
    EXECUTE format('ALTER TABLE mrp.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant ON mrp.%I', t);
    EXECUTE format(
      'CREATE POLICY tenant ON mrp.%I USING (workspace_id = nullif(current_setting(''app.workspace_id'', true), '''')::uuid) '
      'WITH CHECK (workspace_id = nullif(current_setting(''app.workspace_id'', true), '''')::uuid)', t);
  END LOOP;
END $$;

-- Authentication happens before the workspace is known, so it goes through one narrow function
-- that runs as the owner and returns only the workspace of a valid, unrevoked key.
CREATE OR REPLACE FUNCTION mrp.authenticate(p_hash text)
RETURNS TABLE (workspace_id uuid, plan text)
LANGUAGE sql SECURITY DEFINER SET search_path = mrp, pg_temp AS $$
  SELECT k.workspace_id, w.plan FROM mrp.api_keys k JOIN mrp.workspaces w ON w.id = k.workspace_id
  WHERE k.key_hash = p_hash AND k.revoked_at IS NULL
$$;

-- Admin-side tables (no tenant data): provider invoices imported for reconciliation, and the
-- reconciler's own work queue of pending rows across workspaces, via a definer function.
CREATE TABLE IF NOT EXISTS mrp.provider_invoice_lines (
  id          bigserial PRIMARY KEY,
  provider    text NOT NULL,
  day         date NOT NULL,
  model       text NOT NULL,
  request_id  text,
  amount      numeric(14,8) NOT NULL,
  imported_at timestamptz NOT NULL DEFAULT now()
);

-- Calls whose cost is still unknown (a crash or timeout between the provider and our ledger).
CREATE OR REPLACE FUNCTION mrp.unsettled_calls(p_older_than interval)
RETURNS TABLE (workspace_id uuid, provider_request_id text, provider text)
LANGUAGE sql SECURITY DEFINER SET search_path = mrp, pg_temp AS $$
  SELECT c.workspace_id, c.provider_request_id, c.provider FROM mrp.provider_calls c
  WHERE c.cost IS NULL AND c.created_at < now() - p_older_than
$$;

CREATE OR REPLACE FUNCTION mrp.daily_actuals(p_from date, p_to date)
RETURNS TABLE (provider text, day date, model text, amount numeric, calls bigint)
LANGUAGE sql SECURITY DEFINER SET search_path = mrp, pg_temp AS $$
  SELECT c.provider, c.created_at::date, c.model, sum(c.cost), count(*)
  FROM mrp.provider_calls c
  WHERE c.cost IS NOT NULL AND c.created_at::date BETWEEN p_from AND p_to
  GROUP BY 1, 2, 3
$$;

CREATE OR REPLACE FUNCTION mrp.request_actuals(p_from date, p_to date)
RETURNS TABLE (provider text, request_id text, model text, amount numeric)
LANGUAGE sql SECURITY DEFINER SET search_path = mrp, pg_temp AS $$
  SELECT c.provider, c.provider_request_id, c.model, c.cost
  FROM mrp.provider_calls c
  WHERE c.created_at::date BETWEEN p_from AND p_to
$$;

GRANT USAGE ON SCHEMA mrp TO mrp_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA mrp TO mrp_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA mrp TO mrp_app;
REVOKE ALL ON mrp.workspaces, mrp.provider_invoice_lines FROM mrp_app;
REVOKE ALL ON FUNCTION mrp.authenticate(text), mrp.unsettled_calls(interval), mrp.daily_actuals(date, date), mrp.request_actuals(date, date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mrp.authenticate(text) TO mrp_app;
