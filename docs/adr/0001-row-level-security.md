# 1. Tenant isolation in PostgreSQL, not in application code

**Decision.** Every workspace table has row-level security keyed on a transaction-local setting;
the request path runs as a role without BYPASSRLS; cross-workspace work (reconciliation, rotation)
goes through the owner role or narrow security-definer functions.

**Why.** A forgotten `WHERE workspace_id = …` is the most likely way one customer sees another's
data. With RLS such a query returns nothing. The cost is discipline about which pool a query uses,
which the review checklist and invariant tests enforce.
