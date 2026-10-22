"""Noticing when a model gets worse in production.

For tasks with a cheap check, every production answer tells us whether it passed. From the quality
table we know the pass rate to expect for each model and task. When the recent pass rate is clearly
below what we expect, the model is taken out of rotation for that task until it is re-evaluated, and
the router moves to the next cheapest model that meets the target.

"Clearly" is strict on purpose (z = 3.1, about 1 in 1,000 per look): the test runs after every
answer, and repeated looks at a 95% test would take healthy models out of rotation by chance. The
load test found exactly that before the threshold was raised.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from mrp.router import QualityTable


def wilson_upper(successes: float, trials: float, z: float = 1.645) -> float:
    if trials <= 0:
        return 1.0
    p = successes / trials
    d = 1 + z * z / trials
    centre = p + z * z / (2 * trials)
    margin = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    return min(1.0, (centre + margin) / d)


@dataclass
class Health:
    table: QualityTable
    window: int = 200
    min_samples: int = 100
    margin: float = 0.03
    z: float = 3.1
    recent: dict[tuple[str, str], deque] = field(default_factory=dict)
    degraded: set[tuple[str, str]] = field(default_factory=set)

    def expected_pass(self, task: str, model: str) -> float:
        c = self.table.cell(task, model)
        if not c.trials:
            return 0.0
        right = c.trials - c.wrong
        return ((right - c.false_alarms) + (c.wrong - c.caught)) / c.trials

    def observe(self, task: str, model: str, passed: bool) -> bool:
        """Records one checked answer; returns True if the model has just been taken out of rotation."""
        q = self.recent.setdefault((task, model), deque(maxlen=self.window))
        q.append(passed)
        if (task, model) in self.degraded or len(q) < self.min_samples:
            return False
        if wilson_upper(sum(q), len(q), self.z) < self.expected_pass(task, model) - self.margin:
            self.degraded.add((task, model))
            return True
        return False

    def excluded(self, task: str) -> set[str]:
        return {m for t, m in self.degraded if t == task}

    def restore(self, task: str, model: str) -> None:
        self.degraded.discard((task, model))
        self.recent.pop((task, model), None)
