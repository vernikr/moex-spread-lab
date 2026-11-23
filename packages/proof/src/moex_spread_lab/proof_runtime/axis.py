"""Inclusive numeric search domain for one tunable strategy or execution parameter."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


class _SuggestTrial(Protocol):
    def suggest_int(self, name: str, low: int, high: int) -> int: ...

    def suggest_float(
        self, name: str, low: float, high: float, *, step: float | None = None
    ) -> float: ...


@dataclass(frozen=True)
class Axis:
    low: float
    high: float
    step: float

    def contains(self, value: object) -> bool:
        if type(value) not in (int, float):
            return False
        assert isinstance(value, (int, float))
        return (
            self.low <= value <= self.high
            and math.isfinite(value)
            and (self.step != 1 or type(value) is int)
        )

    def suggest(self, trial: _SuggestTrial, name: str, high: float) -> int | float:
        if self.step == 1:
            return trial.suggest_int(name, int(self.low), int(high))
        return round(trial.suggest_float(name, self.low, high, step=self.step), 6)


def _ordered(values: Mapping[str, float]) -> bool:
    return "exit_z" not in values or "entry_z" not in values or values["exit_z"] < values["entry_z"]
