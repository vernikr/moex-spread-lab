"""Cached search measurements and local one-axis sensitivity profiles."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast

from moex_spread_lab.core.w0 import W0Error

from .axis import ordered as _ordered
from .phase_metrics import measure_phase

GREEN = 70
AMBER = 40

# Normalized point/payload shapes shared with the search and cohort facades.
type _Point = Mapping[str, float]
type _Payload = dict[str, Any]
type _Reading = dict[str, Any]
type _Profile = dict[str, Any]
type _Fields = dict[str, _Profile]
type _EvalPoint = dict[str, Any]
type _Evaluate = Callable[[_EvalPoint], _Payload]
type _Progress = Callable[[int, int], None]


class _AxisLike(Protocol):
    """The numeric domain surface used by the one-axis plateau probes."""

    @property
    def step(self) -> float: ...

    def contains(self, value: object) -> bool: ...


# ``ordered`` is the supported public export from #74; it is the very same
# function object as the historical ``axis._ordered``. Binding it under the old
# private name keeps the module-level patch hook and facade identity intact.


class Landscape:
    """Per-request cache of small measurements, never a cache of candidate chart payloads."""

    def __init__(self, axes: Mapping[str, _AxisLike], evaluate: _Evaluate) -> None:
        self.axes = axes
        self.evaluate = evaluate
        self.cache: dict[tuple[float, ...], _Payload] = {}

    def measure(self, values: _Point) -> _Payload:
        key = tuple(values[name] for name in self.axes)
        if key not in self.cache:
            try:
                self.cache[key] = self.evaluate(cast(_EvalPoint, values))
            except W0Error as exc:  # e.g. infeasible adjacent size / insufficient carry warm-up
                self.cache[key] = {"utility": None, "reason": str(exc)}
        return self.cache[key]

    def profile(self, values: _Point, name: str) -> _Profile:
        axis, value = self.axes[name], values[name]
        base = self.measure(values)
        readings: list[_Reading] = []
        for offset in (-2, -1, 1, 2):
            neighbor = round(value + offset * axis.step, 6)
            other: dict[str, float] = {**values, name: neighbor}
            if axis.contains(neighbor) and _ordered(other):
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


def _probe_points(land: Landscape, point: _Point) -> list[_Point]:
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
            if axis.contains(neighbor) and _ordered(other):
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
    fields: _Fields
    if reason:
        fields = {
            k: {"value": point[k], "score": None, "reason": reason, "neighbors": []}
            for k in land.axes
        }
    elif on_progress is not None:
        probe_points = _probe_points(land, point)
        total_probes = len(probe_points)
        for idx, probe in enumerate(probe_points, start=1):
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


# Typed internal exports used by other proof modules.
lamps = _lamps
