# 2. Record every provider call before making it

**Decision.** A provider call row is committed before the call; its cost is filled in afterwards,
or by the reconciler from the provider's records. Customers are billed only for the delivered call.

**Why.** Between "the provider charged us" and "we wrote it down" a process can die. Recording first
turns every such gap into a known row with an unknown cost, which reconciliation can close, instead
of a charge we never learn about.
