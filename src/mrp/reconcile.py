"""Reconciliation: our ledger against what providers say they charged.

Two jobs, run by the worker:

- `settle_unknown_calls`: provider calls whose cost we never learned (the process crashed or the
  connection dropped between the provider's answer and our ledger). Where the provider can report
  usage per request, the cost is filled in; the rest stay open and show up in invoice
  reconciliation.
- `reconcile_invoice`: a provider's invoice lines against our provider calls. With request ids it
  matches line by line; without, per day and model. Every difference is reported: charged but not
  in our ledger, in our ledger but not charged, and amounts that differ beyond rounding.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from mrp import ledger
from mrp.catalog import Catalog
from mrp.db import Database
from mrp.providers.base import Provider

TOLERANCE = Decimal("0.000001")


async def settle_unknown_calls(
    db: Database,
    catalog: Catalog,
    providers: Mapping[str, Provider],
    older_than: timedelta = timedelta(minutes=5),
) -> dict:
    async with db.owner() as conn:
        rows = await conn.fetch("SELECT * FROM mrp.unsettled_calls($1)", older_than)
        plans = {r["id"]: r["plan"] for r in await conn.fetch("SELECT id, plan FROM mrp.workspaces")}
    markups = {w: catalog.plans[p].markup for w, p in plans.items()}
    settled = unknown = 0
    for r in rows:
        provider = providers.get(r["provider"])
        tokens = await provider.usage_for(r["provider_request_id"]) if provider else None
        async with db.tenant(r["workspace_id"]) as conn:
            call = await conn.fetchrow(
                "SELECT model, status FROM mrp.provider_calls WHERE provider_request_id = $1",
                r["provider_request_id"],
            )
            if tokens is None:
                unknown += 1
                continue
            status = "cancelled" if call["status"] == "cancelled" else "abandoned"
            await ledger.call_finished(conn, catalog, r["provider_request_id"], call["model"], status, tokens)
            # keep the request's actual cost equal to the sum of its calls
            await conn.execute(
                "UPDATE mrp.usage u SET actual_cost = (SELECT sum(cost) FROM mrp.provider_calls c WHERE c.usage_id = u.id) "
                "WHERE id = (SELECT usage_id FROM mrp.provider_calls WHERE provider_request_id = $1)",
                r["provider_request_id"],
            )
            # a stream cut off by the client: the delivered part is billed once its cost is known
            delivered = await conn.fetchrow(
                "SELECT c.cost, c.usage_id FROM mrp.provider_calls c WHERE c.provider_request_id = $1 AND c.delivered",
                r["provider_request_id"],
            )
            if delivered is not None:
                await conn.execute(
                    "UPDATE mrp.usage SET billed_amount = round($2::numeric * (1 + $3::numeric), 8) WHERE id = $1",
                    delivered["usage_id"],
                    delivered["cost"],
                    markups[r["workspace_id"]],
                )
            await conn.execute(
                "UPDATE mrp.usage SET status = 'failed', error = 'lost between provider and ledger; cost recorded, not billed', settled_at = now() "
                "WHERE id = (SELECT usage_id FROM mrp.provider_calls WHERE provider_request_id = $1) AND status = 'pending'",
                r["provider_request_id"],
            )
            settled += 1
    return {"checked": len(rows), "settled": settled, "still_unknown": unknown}


@dataclass
class InvoiceLine:
    provider: str
    day: date
    model: str
    amount: Decimal
    request_id: str | None = None


@dataclass
class Report:
    provider: str
    invoice_total: Decimal = Decimal(0)
    ledger_total: Decimal = Decimal(0)
    charged_not_in_ledger: list[dict] = field(default_factory=list)
    in_ledger_not_charged: list[dict] = field(default_factory=list)
    amount_mismatch: list[dict] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.charged_not_in_ledger or self.in_ledger_not_charged or self.amount_mismatch)

    def summary(self) -> dict:
        return {
            "provider": self.provider,
            "invoice_total": str(self.invoice_total),
            "ledger_total": str(self.ledger_total),
            "difference": str(self.invoice_total - self.ledger_total),
            "charged_not_in_ledger": len(self.charged_not_in_ledger),
            "in_ledger_not_charged": len(self.in_ledger_not_charged),
            "amount_mismatch": len(self.amount_mismatch),
            "clean": self.clean,
        }


def parse_invoice_csv(provider: str, text: str) -> list[InvoiceLine]:
    """Columns: day, model, amount, and optionally request_id."""
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        out.append(
            InvoiceLine(
                provider,
                date.fromisoformat(row["day"]),
                row["model"],
                Decimal(row["amount"]),
                row.get("request_id") or None,
            )
        )
    return out


async def reconcile_invoice(
    db: Database, provider: str, lines: list[InvoiceLine], start: date, end: date
) -> Report:
    rep = Report(provider)
    rep.invoice_total = sum((ln.amount for ln in lines), Decimal(0))
    async with db.owner() as conn:
        await conn.executemany(
            "INSERT INTO mrp.provider_invoice_lines (provider, day, model, request_id, amount) VALUES ($1, $2, $3, $4, $5)",
            [(ln.provider, ln.day, ln.model, ln.request_id, ln.amount) for ln in lines],
        )
        if lines and all(ln.request_id for ln in lines):
            ours = {
                r["request_id"]: r
                for r in await conn.fetch(
                    "SELECT * FROM mrp.request_actuals($1, $2) WHERE provider = $3", start, end, provider
                )
            }
            rep.ledger_total = sum((r["amount"] or Decimal(0) for r in ours.values()), Decimal(0))
            seen = set()
            for ln in lines:
                seen.add(ln.request_id)
                mine = ours.get(ln.request_id)
                if mine is None:
                    rep.charged_not_in_ledger.append({"request_id": ln.request_id, "amount": str(ln.amount)})
                elif mine["amount"] is None:
                    rep.charged_not_in_ledger.append(
                        {
                            "request_id": ln.request_id,
                            "amount": str(ln.amount),
                            "note": "call recorded, cost unknown",
                        }
                    )
                elif abs(mine["amount"] - ln.amount) > TOLERANCE:
                    rep.amount_mismatch.append(
                        {
                            "request_id": ln.request_id,
                            "invoice": str(ln.amount),
                            "ledger": str(mine["amount"]),
                        }
                    )
            for rid, r in ours.items():
                if rid not in seen and (r["amount"] or 0) > 0:
                    rep.in_ledger_not_charged.append({"request_id": rid, "amount": str(r["amount"])})
        else:
            ours_daily = {
                (r["day"], r["model"]): r["amount"]
                for r in await conn.fetch(
                    "SELECT * FROM mrp.daily_actuals($1, $2) WHERE provider = $3", start, end, provider
                )
            }
            rep.ledger_total = sum(ours_daily.values(), Decimal(0))
            inv: dict[tuple[date, str], Decimal] = {}
            for ln in lines:
                inv[(ln.day, ln.model)] = inv.get((ln.day, ln.model), Decimal(0)) + ln.amount
            for k in sorted(set(inv) | set(ours_daily)):
                a, b = inv.get(k), ours_daily.get(k)
                item = {"day": k[0].isoformat(), "model": k[1]}
                if b is None:
                    rep.charged_not_in_ledger.append({**item, "amount": str(a)})
                elif a is None:
                    rep.in_ledger_not_charged.append({**item, "amount": str(b)})
                elif abs(a - b) > Decimal("0.01"):
                    rep.amount_mismatch.append({**item, "invoice": str(a), "ledger": str(b)})
    return rep
