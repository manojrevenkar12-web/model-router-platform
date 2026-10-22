"""Choosing a model per request: the cheapest one that meets the quality target, with evidence.

The router never assumes a model is good at a task. It reads a quality table of measured pass
rates (successes / trials per task and model, from the evaluation suite and from verified
production outcomes) and uses the lower end of a 95% confidence interval, so a model that looked
good on 20 examples does not win against one proven on 2,000.

For tasks with a cheap check (JSON that parses with the required fields, a label from the allowed
set, code that compiles) it can try a cheaper model first and escalate when the check fails. It does
so only when the measured numbers say the answers that pass the check will still meet the target:

    P(correct | passed) = q·(1-a) / (q·(1-a) + (1-q)·(1-c))

with q the cheap model's pass rate, c the share of wrong answers the check catches and a the share
of right answers it rejects, all measured.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from mrp.catalog import Catalog, Model

TASKS = ("extraction", "classification", "code", "summarization", "reasoning", "chat")
Z = 1.645  # one-sided 95%


def classify(messages: list[dict], hint: str | None = None, response_format: dict | None = None) -> str:
    """Rule-based task classification; a caller's explicit hint wins when it names a known task."""
    if hint in TASKS:
        return hint
    if response_format and response_format.get("type") in ("json_object", "json_schema"):
        return "extraction"
    last = str(messages[-1].get("content", "")) if messages else ""
    low = last.lower()
    if re.search(r"\b(extract|pull out|parse|fill (in )?the fields|as json)\b", low):
        return "extraction"
    if re.search(r"\b(classify|categori[sz]e|label|sentiment|which category)\b", low):
        return "classification"
    if "```" in last or re.search(
        r"\b(write|fix|refactor|implement) (a |the )?(function|code|class|test|script|query)\b", low
    ):
        return "code"
    if re.search(r"\b(summari[sz]e|tl;dr|summary of|key points)\b", low):
        return "summarization"
    if re.search(r"\b(prove|step by step|reason|plan the|calculate|why does|derive)\b", low):
        return "reasoning"
    return "chat"


# ---- cheap checks --------------------------------------------------------------------------------------

LABELS = {"positive", "negative", "neutral"}


def check(task: str, text: str) -> bool | None:
    """True/False for tasks with a cheap check, None where no cheap check exists."""
    if task == "extraction":
        try:
            obj = json.loads(text)
        except ValueError:
            return False
        return isinstance(obj, dict) and bool(obj)
    if task == "classification":
        return text.strip().lower() in LABELS
    if task == "code":
        src = text.strip().removeprefix("```python").removeprefix("```").removesuffix("```")
        try:
            compile(src, "<answer>", "exec")
        except SyntaxError:
            return False
        return True
    return None


# ---- the quality table ------------------------------------------------------------------------------------


def wilson_lower(successes: float, trials: float, z: float = Z) -> float:
    if trials <= 0:
        return 0.0
    p = successes / trials
    d = 1 + z * z / trials
    centre = p + z * z / (2 * trials)
    margin = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    return max(0.0, (centre - margin) / d)


@dataclass
class Cell:
    passes: float = 0.0
    trials: float = 0.0
    caught: float = 0.0  # wrong answers the cheap check rejected
    wrong: float = 0.0
    false_alarms: float = 0.0  # right answers the check rejected

    @property
    def rate(self) -> float:
        return self.passes / self.trials if self.trials else 0.0

    @property
    def lower(self) -> float:
        return wilson_lower(self.passes, self.trials)


@dataclass
class QualityTable:
    cells: dict[str, dict[str, Cell]] = field(default_factory=dict)
    # Recent production outcomes count, older ones fade: each new verified outcome multiplies the
    # cell's counts by `decay` first, so a model that gets worse is noticed within a few hundred calls.
    decay: float = 0.997

    def cell(self, task: str, model: str) -> Cell:
        return self.cells.setdefault(task, {}).setdefault(model, Cell())

    def record(
        self, task: str, model: str, correct: bool, check_passed: bool | None = None, online: bool = False
    ) -> None:
        c = self.cell(task, model)
        if online:
            for attr in ("passes", "trials", "caught", "wrong", "false_alarms"):
                setattr(c, attr, getattr(c, attr) * self.decay)
        c.trials += 1
        c.passes += correct
        if check_passed is not None:
            if correct:
                c.false_alarms += not check_passed
            else:
                c.wrong += 1
                c.caught += not check_passed

    def check_rates(self, task: str) -> tuple[float, float]:
        """(catch rate, false-alarm rate) of the task's cheap check, pooled over models, conservatively."""
        wrong = sum(c.wrong for c in self.cells.get(task, {}).values())
        caught = sum(c.caught for c in self.cells.get(task, {}).values())
        right = sum(c.trials - c.wrong for c in self.cells.get(task, {}).values())
        alarms = sum(c.false_alarms for c in self.cells.get(task, {}).values())
        catch = wilson_lower(caught, wrong) if wrong else 0.0
        alarm = 1 - wilson_lower(right - alarms, right) if right else 1.0
        return catch, alarm

    def to_json(self) -> dict:
        return {t: {m: c.__dict__ for m, c in ms.items()} for t, ms in self.cells.items()}

    @classmethod
    def from_json(cls, data: dict) -> QualityTable:
        t = cls()
        for task, ms in data.items():
            for model, c in ms.items():
                t.cells.setdefault(task, {})[model] = Cell(**c)
        return t

    @classmethod
    def load(cls, path: Path) -> QualityTable:
        return cls.from_json(json.loads(path.read_text())) if path.exists() else cls()

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_json(), indent=1, sort_keys=True))


# ---- decisions ---------------------------------------------------------------------------------------------


@dataclass
class Route:
    task: str
    target: float
    primary: Model
    fallbacks: list[Model]
    first_try: Model | None  # a cheaper model to try before the primary, when the check makes it safe
    estimated_cost: Decimal  # expected cost of the plan (cheap try + escalations), an estimate
    reason: str
    expected_quality: float


class NoRoute(Exception):
    pass


def passed_quality(q: float, catch: float, alarm: float) -> float:
    """P(correct | the answer passed the cheap check)."""
    ok = q * (1 - alarm)
    bad = (1 - q) * (1 - catch)
    return ok / (ok + bad) if ok + bad else 0.0


def decide(
    catalog: Catalog,
    table: QualityTable,
    task: str,
    target: float,
    tokens_in: int,
    tokens_out: int,
    max_cost: Decimal,
    allowed: set[str] | None = None,
    cascade: bool = True,
    use_lower_bound: bool = True,
) -> Route:
    cands = [
        m
        for m in catalog.models.values()
        if (allowed is None or m.id in allowed) and m.context >= tokens_in + tokens_out
    ]
    if not cands:
        raise NoRoute("no model can take a request this long")

    def q(m: Model) -> float:
        c = table.cell(task, m.id)
        return c.lower if use_lower_bound else c.rate

    def cost(m: Model) -> Decimal:
        return m.cost(tokens_in, tokens_out)

    affordable = [m for m in cands if cost(m) <= max_cost]
    if not affordable:
        raise NoRoute(f"the cheapest model costs more than the {max_cost} limit")
    meeting = sorted((m for m in affordable if q(m) >= target), key=lambda m: (cost(m), -q(m)))
    if meeting:
        primary, reason = meeting[0], f"cheapest model proven at >= {target:.0%} on {task}"
        fallbacks = meeting[1:3]
    else:  # nobody is proven at the target within budget: take the best evidence available
        ranked = sorted(affordable, key=lambda m: (-q(m), cost(m)))
        primary, reason = (
            ranked[0],
            f"no model proven at {target:.0%} on {task} within budget; best available",
        )
        fallbacks = ranked[1:3]
    first_try: Model | None = None
    est = cost(primary)
    expected = q(primary)
    if cascade and check(task, "") is not None:
        catch, alarm = table.check_rates(task)
        best: tuple[Decimal, Model, float] | None = None
        for m in affordable:
            if cost(m) >= cost(primary):
                continue
            qm = q(m)
            if passed_quality(qm, catch, alarm) < target:
                continue
            p_reject = qm * alarm + (1 - qm) * catch  # upper estimate of how often we escalate
            expected_cost = cost(m) + Decimal(str(p_reject)) * cost(primary)
            if expected_cost < cost(primary) and (best is None or expected_cost < best[0]):
                best = (expected_cost, m, qm)
        if best is not None:
            est, first_try = best[0], best[1]
            reason += f"; try {first_try.id} first, escalate if the check fails"
    return Route(task, target, primary, fallbacks, first_try, est, reason, expected)
