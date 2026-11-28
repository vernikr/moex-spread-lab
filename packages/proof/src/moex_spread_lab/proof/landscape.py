"""Cached search measurements, one-axis sensitivity profiles, and the inclusive
numeric search domain for one tunable strategy or execution parameter."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from moex_spread_lab.core.w0 import W0Error

from .worker_metrics import measure_phase

GREEN = 70
AMBER = 40

type _Point = Mapping[str, float]
type _Payload = dict[str, Any]
type _Evaluate = Callable[[_Payload], _Payload]
type _Progress = Callable[[int, int], None]


class _SuggestTrial(Protocol):
    def suggest_int(self, name: str, low: int, high: int, *, step: int = 1) -> int: ...

    def suggest_float(
        self, name: str, low: float, high: float, *, step: float | None = None
    ) -> float: ...


@dataclass(frozen=True)
class Axis:
    low: float
    high: float
    step: float
    kind: Literal["int", "float"] = "float"

    def __post_init__(self) -> None:
        if self.step <= 0:
            raise ValueError(f"Axis step must be > 0, got {self.step!r}")
        if self.low > self.high:
            raise ValueError(f"Axis low ({self.low!r}) must be <= high ({self.high!r})")

    @property
    def is_integer(self) -> bool:
        """Integer-valued axis: explicit int kind, or a float axis with unit step."""
        return self.kind == "int" or (self.kind == "float" and self.step == 1)

    def contains(self, value: object) -> bool:
        if type(value) not in (int, float):
            return False
        assert isinstance(value, (int, float))
        if not math.isfinite(value):
            return False
        if not self.low <= value <= self.high:
            return False
        if self.kind == "int" and type(value) is not int:
            return False
        grid = (value - self.low) / self.step
        return math.isclose(grid, round(grid), rel_tol=0.0, abs_tol=1e-9)

    def suggest(self, trial: _SuggestTrial, name: str, high: float) -> int | float:
        if self.is_integer:
            return trial.suggest_int(name, int(self.low), int(high), step=int(self.step))
        return round(trial.suggest_float(name, self.low, high, step=self.step), 6)


def ordered(values: Mapping[str, float]) -> bool:
    """Threshold values keep the exit level strictly below the entry level."""
    return "exit_z" not in values or "entry_z" not in values or values["exit_z"] < values["entry_z"]


class Landscape:
    """Per-request cache of small measurements, never a cache of candidate chart payloads."""

    def __init__(self, axes: Mapping[str, Axis], evaluate: _Evaluate) -> None:
        self.axes = axes
        self.evaluate = evaluate
        self.cache: dict[tuple[float, ...], _Payload] = {}

    def measure(self, values: _Point) -> _Payload:
        key = tuple(values[name] for name in self.axes)
        if key not in self.cache:
            try:
                self.cache[key] = self.evaluate(cast(_Payload, values))
            except W0Error as exc:  # e.g. infeasible adjacent size / insufficient carry warm-up
                self.cache[key] = {"utility": None, "reason": str(exc)}
        return self.cache[key]

    def profile(self, values: _Point, name: str) -> _Payload:
        axis, value = self.axes[name], values[name]
        base = self.measure(values)
        readings: list[_Payload] = []
        for offset in (-2, -1, 1, 2):
            neighbor = round(value + offset * axis.step, 6)
            other: dict[str, float] = {**values, name: neighbor}
            if axis.contains(neighbor) and ordered(other):
                readings.append({"value": neighbor, **self.measure(other)})
        valid: list[float] = [
            cast(float, r["utility"]) for r in readings if r["utility"] is not None
        ]
        # No evidence is not a red zero; a measured losing center cannot be a positive plateau.
        score: float | None = None
        reason: Any = base.get("reason")
        worst: float | None = None
        utility = base["utility"]
        if utility is not None and valid:
            worst = min(valid)
            center = cast(float, utility)
            if center <= 0:
                score, reason = (
                    0.0,
                    "Результат текущего значения на обучении неположителен.",
                )
            elif len(valid) != len(readings):
                score, reason = 0.0, "Есть сосед без допустимого результата."
            else:
                score = round(100 * min(1, max(0, worst / center)), 2)
        elif not reason:
            reason = "Нет допустимых соседних значений для сравнения."
        return {
            "value": value,
            "score": score,
            "center": base,
            "neighbors": readings,
            "worst": worst,
            "reason": reason,
            "step": axis.step,
        }


def probe_points(land: Landscape, point: _Point) -> list[_Point]:
    # Count deterministic neighbor probes upfront for monotonic progress updates.
    probe_points: list[_Point] = []
    seen: set[tuple[float, ...]] = set()
    center_key = tuple(point[n] for n in land.axes)
    if center_key not in land.cache:
        seen.add(center_key)
        probe_points.append(point)
    for name, axis in land.axes.items():
        value = point[name]
        for offset in (-2, -1, 1, 2):
            neighbor = round(value + offset * axis.step, 6)
            other: dict[str, float] = {**point, name: neighbor}
            if axis.contains(neighbor) and ordered(other):
                key = tuple(other[n] for n in land.axes)
                if key not in land.cache and key not in seen:
                    seen.add(key)
                    probe_points.append(other)
    return probe_points


@measure_phase("plateau")
def _lamps(
    land: Landscape,
    point: _Point,
    reason: str | None = None,
    *,
    on_progress: _Progress | None = None,
) -> dict[str, Any]:
    fields: dict[str, _Payload]
    if reason:
        fields = {
            k: {"value": point[k], "score": None, "reason": reason, "neighbors": []}
            for k in land.axes
        }
    elif on_progress is not None:
        probes = probe_points(land, point)
        total_probes = len(probes)
        for idx, probe in enumerate(probes, start=1):
            land.measure(probe)
            on_progress(idx, total_probes)
        fields = {k: land.profile(point, k) for k in land.axes}
    else:
        fields = {k: land.profile(point, k) for k in land.axes}
    return {
        "basis": "training",
        "unit": "annualized_excess_pp",
        "fields": fields,
        "green": GREEN,
        "amber": AMBER,
    }


lamps = _lamps
