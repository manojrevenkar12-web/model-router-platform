"""Command line: serve, migrate, calibrate, evaluate, gate."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Objectives the evaluation must meet; `mrp gate` fails CI when one is missed.
SLO = {
    "routing_standard_saving_vs_frontier": (">=", 0.50),
    "routing_standard_tasks_below_target": ("<=", 0),
    "routing_small_calibration_lower_bound_tasks_below_target": ("<=", 1),
    "drift_quality_after_exclusion": (">=", 0.90),
    "drift_false_exclusions": ("<=", 0),
    "billing_charges_missing_from_ledger": ("<=", 0),
    "billing_cost_mismatches": ("<=", 0),
    "billing_double_rows": ("<=", 0),
    "billing_wrong_amounts": ("<=", 0),
    "billing_billed_without_answer": ("<=", 0),
    "invoice_planted_errors_found": (">=", 1),
    "cache_false_hits_with_slot_check": ("<=", 0.0),
    "overhead_p95_ms_concurrency_1": ("<=", 40),
}


def measures(r: dict) -> dict:
    std = {p["policy"]: p for p in r["routing"]["targets"]["standard"]["policies"]}
    small = {p["policy"]: p for p in r["routing"]["small_calibration"]["policies"]}
    drift = r["routing"]["drift"]["with_monitor"]["quality_per_500_requests"]
    b = r["billing"]
    cache = next(x for x in r["cache"]["by_threshold"] if x["threshold"] == 0.85)
    return {
        "routing_standard_saving_vs_frontier": std["lower-bound+cascade"]["saving_vs_frontier"],
        "routing_standard_tasks_below_target": len(std["lower-bound+cascade"]["tasks_clearly_below_target"]),
        "routing_small_calibration_lower_bound_tasks_below_target": len(
            small["lower-bound+cascade"]["tasks_clearly_below_target"]
        ),
        "drift_quality_after_exclusion": min(drift[-3:]),
        "drift_false_exclusions": len(r["routing"]["drift"]["false_exclusions_in_steady_state"]),
        "billing_charges_missing_from_ledger": b["ledger_vs_provider_records"]["charges_missing_from_ledger"],
        "billing_cost_mismatches": b["ledger_vs_provider_records"]["cost_mismatches"],
        "billing_double_rows": b["customer_billing"]["keys_with_more_than_one_row"],
        "billing_wrong_amounts": b["customer_billing"]["billed_amount_wrong"],
        "billing_billed_without_answer": b["customer_billing"]["billed_without_an_answer"],
        "invoice_planted_errors_found": int(b["invoice_reconciliation"]["all_found"]),
        "cache_false_hits_with_slot_check": cache["near_misses_served_with_slot_check"],
        "overhead_p95_ms_concurrency_1": r["load"]["concurrency_1"]["p95_ms"],
    }


def cmd_evaluate(args) -> int:
    from mrp.eval import billing, cache_eval, load, routing
    from mrp.eval.report import write

    db, redis = os.environ["MRP_DATABASE_URL"], os.environ.get("MRP_REDIS_URL", "redis://localhost:6379/0")
    t0 = time.time()
    quick = args.quick
    res = {
        "routing": routing.run(n_cal=300, n_test_per_task=400 if quick else 1000),
        "cache": cache_eval.run(n=300 if quick else 600),
        "billing": asyncio.run(billing.run(db, redis, n=500 if quick else 1500)),
        "load": asyncio.run(load.run(db, redis, n=500 if quick else 2000)),
    }
    res["meta"] = {"quick": quick, "seconds": round(time.time() - t0, 1)}
    res["measures"] = measures(res)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "evaluation.json").write_text(json.dumps(res, indent=2, default=str))
    write(res, out)
    print((out / "EVALUATION.md").read_text())
    return 0


def cmd_gate(args) -> int:
    res = json.loads((Path(args.out) / "evaluation.json").read_text())
    m = measures(res)
    failed = 0
    for k, (op, target) in SLO.items():
        ok = m[k] >= target if op == ">=" else m[k] <= target
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {k}: {m[k]} (target {op} {target})")
    return 1 if failed else 0


def cmd_calibrate(args) -> int:
    from mrp.app import DEFAULT_TABLE
    from mrp.eval.routing import calibrate

    path = Path(args.out) if args.out else DEFAULT_TABLE
    calibrate(args.n).save(path)
    print(f"quality table written to {path}")
    return 0


def cmd_migrate(args) -> int:
    from mrp.db import Database

    async def go():
        await Database(os.environ["MRP_DATABASE_URL"]).migrate()

    asyncio.run(go())
    print("migrated")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("mrp.main:app", host=args.host, port=args.port, proxy_headers=True, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mrp")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    sub.add_parser("migrate")
    p = sub.add_parser(
        "calibrate", help="measure per-task pass rates into the quality table (simulated models)"
    )
    p.add_argument("--n", type=int, default=300)
    p.add_argument("--out", default="")
    p = sub.add_parser("evaluate")
    p.add_argument("--quick", action="store_true")
    p.add_argument("--out", default="reports")
    p = sub.add_parser("gate")
    p.add_argument("--out", default="reports")
    args = ap.parse_args(argv)
    return {
        "serve": cmd_serve,
        "migrate": cmd_migrate,
        "calibrate": cmd_calibrate,
        "evaluate": cmd_evaluate,
        "gate": cmd_gate,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
