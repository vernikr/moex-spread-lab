"""Inclusive numeric search domain for one tunable strategy or execution parameter."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Axis:
    low: float
    high: float
    step: float

    def contains(self, value):
        return (
            type(value) in (int, float)
            and self.low <= value <= self.high
            and math.isfinite(value)
            and (self.step != 1 or type(value) is int)
        )

    def suggest(self, trial, name, high):
        if self.step == 1:
            return trial.suggest_int(name, self.low, high)
        return round(trial.suggest_float(name, self.low, high, step=self.step), 6)


def _ordered(values):
    return "exit_z" not in values or "entry_z" not in values or values["exit_z"] < values["entry_z"]
