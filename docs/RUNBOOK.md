# Runbook

| Signal | Meaning | First steps |
|---|---|---|
| Fallback rate up for one provider | the provider is failing or rate-limiting | check the provider's status page; the router already falls back; if sustained, remove the provider's models from `allowed` for affected plans |
| Reconciler `still_unknown` > 0 for a closed provider | calls whose cost we could not learn per request | expected for providers without per-request usage; they are covered by the daily invoice reconciliation |
| Invoice reconciliation not clean | the provider's invoice and our ledger disagree | `charged_not_in_ledger`: look for a crash window or a key used outside the gateway; `amount_mismatch`: check the price table against the provider's; open a billing dispute with the evidence |
| A model taken out of rotation | its check pass rate dropped below its measured rate | re-run its evaluation suite; if it recovered, restore it (`Health.restore`); if not, update the quality table |
| `usage` rows stuck in `pending` | a gateway instance died mid-request | the next retry takes over; the reconciler settles lost calls every five minutes |
| Agent runs stuck in `running` | a worker died holding a lease | another worker resumes after the lease (30 s); check worker restarts |
| Key Manager errors | customers' provider keys cannot be unwrapped | requests that need a customer's own key fail; page on-call (platform-key traffic is unaffected) |

## Rotating the key-encryption key

1. Key Manager rotates the key (90-day policy) or rotate it manually.
2. `POST /admin/kek/rotate` re-wraps every data key with the current version; secrets themselves are
   never decrypted for this.
3. Retire the old version once the response shows nothing left to re-wrap.

## Restoring the database

Restore from the managed backup into a new instance, run `mrp migrate` (idempotent), point the
secret at it, roll the gateway and worker. Then run the reconciler for the gap window: provider calls
made during the outage window show up as `charged_not_in_ledger` in the next invoice reconciliation.
