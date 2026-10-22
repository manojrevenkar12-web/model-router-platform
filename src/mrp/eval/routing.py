"""Does cost-aware routing save money without missing the quality target?

Uses the simulated model pool (providers/sim.py): eight models with hidden true pass rates per task.
The router only ever sees what calibration measured.

1. Calibration: each model answers n items per task; pass/fail and the cheap check's verdict are
   recorded in a quality table.
2. Test: 6,000 new requests (1,000 per task). Each policy picks models; correctness and cost come
   from the simulator. Policies: always the frontier model, always the cheapest, a table read by
   point estimate, a table read by the lower confidence bound, and the lower bound plus cheap-first
   cascades.
3. Small calibration: the same with only 30 items per cell, where reading point estimates is
   dangerous.
4. Drift: halfway through, the cheap fine-tuned extraction model degrades; the health monitor must
   take it out of rotation and quality must recover.

The numbers measure the routing logic against a known truth. Real savings depend on real models'
pass rates on a customer's tasks, which is why the table is measured, never configured.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from decimal import Decimal

from mrp import router
from mrp.catalog import Catalog, load
from mrp.health import Health
from mrp.providers import sim

PROMPT_TOKENS = {
    "extraction": (300, 1500),
    "classification": (60, 400),
    "code": (200, 1200),
    "summarization": (800, 4000),
    "reasoning": (150, 900),
    "chat": (40, 600),
}


def calibrate(n: int, seed: int = 1, catalog: Catalog | None = None) -> router.QualityTable:
    catalog = catalog or load()
    table = router.QualityTable()
    for task in sim.TASKS:
        for i in range(n):
            item = f"cal{seed}-{task}-{i}"
            for m in catalog.models:
                correct, passes = sim.outcome(m, task, item)
                table.record(task, m, correct, passes if router.check(task, "") is not None else None)
    return table


@dataclass
class Result:
    policy: str
    requests: int = 0
    correct: int = 0
    cost: Decimal = Decimal(0)
    escalations: int = 0
    latency: list[float] = field(default_factory=list)
    per_task: dict[str, list[int]] = field(default_factory=dict)  # task -> [correct, n]
    model_use: dict[str, int] = field(default_factory=dict)

    def summary(self, target: float, baseline_cost: Decimal | None = None) -> dict:
        lat = sorted(self.latency)
        tasks = {t: round(c / n, 4) for t, (c, n) in self.per_task.items()}
        below = [t for t, (c, n) in self.per_task.items() if wilson_upper(c, n) < target]
        out = {
            "policy": self.policy,
            "quality": round(self.correct / self.requests, 4),
            "quality_by_task": tasks,
            "tasks_clearly_below_target": below,
            "cost_eur": float(round(self.cost, 4)),
            "cost_per_1k_requests_eur": float(round(self.cost / self.requests * 1000, 4)),
            "p50_latency_ms": lat[len(lat) // 2],
            "p95_latency_ms": lat[int(len(lat) * 0.95)],
            "escalation_rate": round(self.escalations / self.requests, 4),
            "model_use": dict(sorted(self.model_use.items(), key=lambda kv: -kv[1])),
        }
        if baseline_cost:
            out["saving_vs_frontier"] = round(float(1 - self.cost / baseline_cost), 4)
        return out


def wilson_upper(c: int, n: int, z: float = 1.645) -> float:
    if n == 0:
        return 1.0
    p = c / n
    d = 1 + z * z / n
    return min(1.0, (p + z * z / (2 * n) + z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d)


def requests(n_per_task: int, seed: int) -> list[tuple[str, str, int, int]]:
    rng = random.Random(seed)
    out = []
    for task in sim.TASKS:
        lo, hi = PROMPT_TOKENS[task]
        for i in range(n_per_task):
            out.append((task, f"test{seed}-{task}-{i}", rng.randint(lo, hi), sim.OUT_TOKENS[task]))
    rng.shuffle(out)
    return out


def run_policy(
    name: str,
    reqs,
    table: router.QualityTable,
    target: float,
    catalog: Catalog,
    health: Health | None = None,
    drift_after: int | None = None,
) -> Result:
    res = Result(name)
    original = sim.TRUE_QUALITY["open-8b-extract"][0]
    try:
        for idx, (task, item, tin, tout) in enumerate(reqs):
            if drift_after is not None and idx == drift_after:
                sim.TRUE_QUALITY["open-8b-extract"][0] = 0.60  # the fine-tuned model regresses
            if name == "frontier-large":
                route = router.Route(
                    task, target, catalog.model("frontier-large"), [], None, Decimal(0), "", 0
                )
            elif name == "cheapest":
                route = router.Route(task, target, catalog.model("open-8b"), [], None, Decimal(0), "", 0)
            else:
                allowed = set(catalog.models) - (health.excluded(task) if health else set())
                route = router.decide(
                    catalog, table, task, target, tin, tout, Decimal("10"), allowed,
                    cascade=name.endswith("cascade"), use_lower_bound=not name.startswith("point"),
                )  # fmt: skip
            res.requests += 1
            latency = 0.0
            chosen = route.primary
            correct = False
            if route.first_try is not None:
                m = route.first_try
                c, passes = sim.outcome(m.id, task, item)
                res.cost += m.cost(tin, tout)
                latency += sim.LATENCY_MS[m.id]
                if health:
                    health.observe(task, m.id, passes)
                if passes:
                    chosen, correct = m, c
                else:
                    res.escalations += 1
                    chosen = route.primary
            if chosen is route.primary:
                c, passes = sim.outcome(chosen.id, task, item)
                correct = c
                res.cost += chosen.cost(tin, tout)
                latency += sim.LATENCY_MS[chosen.id]
                has_check = router.check(task, "") is not None
                if health and has_check:
                    health.observe(task, chosen.id, passes)
                if (
                    has_check and not passes and route.fallbacks
                ):  # visibly wrong: escalate once more, as the gateway does
                    res.escalations += 1
                    fb = route.fallbacks[0]
                    correct, _ = sim.outcome(fb.id, task, item)
                    res.cost += fb.cost(tin, tout)
                    latency += sim.LATENCY_MS[fb.id]
                    chosen = fb
            res.correct += correct
            pt = res.per_task.setdefault(task, [0, 0])
            pt[0] += correct
            pt[1] += 1
            res.latency.append(latency)
            res.model_use[chosen.id] = res.model_use.get(chosen.id, 0) + 1
    finally:
        sim.TRUE_QUALITY["open-8b-extract"][0] = original
    return res


def run(n_cal: int = 300, n_test_per_task: int = 1000) -> dict:
    catalog = load()
    out: dict = {
        "calibration_items_per_cell": n_cal,
        "test_requests": n_test_per_task * len(sim.TASKS),
        "targets": {},
    }
    table = calibrate(n_cal)
    reqs = requests(n_test_per_task, seed=2)
    for quality in ("standard", "high"):
        target = catalog.quality_targets[quality]
        results = {
            p: run_policy(p, reqs, table, target, catalog)
            for p in ("frontier-large", "cheapest", "point-estimate", "lower-bound", "lower-bound+cascade")
        }
        base = results["frontier-large"].cost
        out["targets"][quality] = {
            "target": target,
            "policies": [r.summary(target, base) for r in results.values()],
        }

    # small calibration: 30 items per cell
    small = calibrate(30, seed=7)
    target = catalog.quality_targets["standard"]
    out["small_calibration"] = {
        "items_per_cell": 30,
        "target": target,
        "policies": [
            run_policy(p, reqs, small, target, catalog).summary(target)
            for p in ("point-estimate", "lower-bound", "lower-bound+cascade")
        ],
    }

    # drift: extraction requests only; the cheap fine-tuned model degrades after 1,500 of 4,000
    ext = [r for r in requests(4000, seed=11) if r[0] == "extraction"]
    target = catalog.quality_targets["standard"]
    table_d = calibrate(n_cal, seed=3)
    windows = {}
    for label, use_health in (("without_monitor", False), ("with_monitor", True)):
        health = Health(table_d) if use_health else None
        res = Result(label)
        chunks = []
        for start in range(0, len(ext), 500):
            part = run_policy(
                "lower-bound+cascade",
                ext[start : start + 500],
                table_d,
                target,
                catalog,
                health,
                drift_after=0 if start >= 1500 else None,
            )
            chunks.append(round(part.correct / part.requests, 4))
            res.cost += part.cost
        windows[label] = {
            "quality_per_500_requests": chunks,
            "excluded": sorted(health.degraded) if health else [],
        }
    # steady state: the monitor watching 6,000 mixed requests with nothing wrong must not exclude anything
    steady = Health(table)
    run_policy(
        "lower-bound+cascade",
        requests(1000, seed=13),
        table,
        catalog.quality_targets["standard"],
        catalog,
        steady,
    )
    out["drift"] = {
        "requests": len(ext),
        "degrades_after": 1500,
        "target": target,
        **windows,
        "false_exclusions_in_steady_state": sorted(steady.degraded),
    }
    return out
