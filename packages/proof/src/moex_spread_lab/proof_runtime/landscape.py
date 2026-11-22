"""Cached search measurements and local one-axis sensitivity profiles."""

from __future__ import annotations

from moex_spread_lab.core.w0 import W0Error

from .axis import _ordered
from .phase_metrics import measure_phase

GREEN = 70
AMBER = 40


class Landscape:
    """Per-request cache of small measurements, never a cache of candidate chart payloads."""

    def __init__(self, axes, evaluate):
        self.axes = axes
        self.evaluate = evaluate
        self.cache = {}

    def measure(self, values):
        key = tuple(values[name] for name in self.axes)
        if key not in self.cache:
            try:
                self.cache[key] = self.evaluate(values)
            except W0Error as exc:  # e.g. infeasible adjacent size / insufficient carry warm-up
                self.cache[key] = {"utility": None, "reason": str(exc)}
        return self.cache[key]

    def profile(self, values, name):
        axis, value = self.axes[name], values[name]
        base = self.measure(values)
        readings = []
        for offset in (-2, -1, 1, 2):
            neighbor = round(value + offset * axis.step, 6)
            other = {**values, name: neighbor}
            if axis.contains(neighbor) and _ordered(other):
                readings.append({"value": neighbor, **self.measure(other)})
        valid = [r["utility"] for r in readings if r["utility"] is not None]
        # No evidence is not a red zero; a measured losing center cannot be a positive plateau.
        score, reason, worst = None, base.get("reason"), None
        if base["utility"] is not None and valid:
            worst = min(valid)
            if base["utility"] <= 0:
                score, reason = (
                    0.0,
                    "Результат текущего значения на обучении неположителен.",
                )
            elif len(valid) != len(readings):
                score, reason = 0.0, "Есть сосед без допустимого результата."
            else:
                score = round(100 * min(1, max(0, worst / base["utility"])), 2)
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


def _probe_points(land, point):
    # Count deterministic neighbor probes upfront for monotonic progress updates.
    probe_points = []
    seen = set()
    center_key = tuple(point[n] for n in land.axes)
    if center_key not in land.cache:
        seen.add(center_key)
        probe_points.append(point)
    for name, axis in land.axes.items():
        value = point[name]
        for offset in (-2, -1, 1, 2):
            neighbor = round(value + offset * axis.step, 6)
            other = {**point, name: neighbor}
            if axis.contains(neighbor) and _ordered(other):
                key = tuple(other[n] for n in land.axes)
                if key not in land.cache and key not in seen:
                    seen.add(key)
                    probe_points.append(other)
    return probe_points


@measure_phase("plateau")
def _lamps(land, point, reason=None, *, on_progress=None):
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
