"""Shared market window for pair proofs: one cut-off, no strategy decisions.

Built-in strategies and drop-in plugins read the same cut price series, funding
evidence, liquidity metadata and multipliers from here. Nothing in this module
evaluates a strategy, so building a chart template never needs a replay.
"""

from __future__ import annotations

from array import array
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from moex_spread_lab.core.settings import MIN_DAILY_VOLUME
from moex_spread_lab.core.timeframes import get_timeframe
from moex_spread_lab.strategies.pair_mean_reversion.engine import compute_rolling_z
from moex_spread_lab.strategies.pair_screener import _session_volumes

from .payload_tools import min_leg_volume


@dataclass(frozen=True, slots=True)
class ReplaySpec:
    """One request-scoped pair replay (price or carry settlement) bound to prepared inputs."""

    inputs: Any
    pair: dict[str, Any]
    execution: dict[str, Any]
    params: dict[str, Any]
    terms: dict[str, dict[str, Any]]
    risk: dict[str, Any]
    prepared: tuple[Any, ...]
    through: str | None = None
    cache: dict[str, Any] | None = None
    benchmark_dates: list[str] | None = None
    benchmark_prices: dict[str, float] | None = None


@dataclass(frozen=True, slots=True)
class PairMarketWindow:
    """Cut-off price series, funding evidence and derived multipliers for one replay."""

    spec: ReplaySpec
    series: dict[str, Any]
    analysis_rows: list[dict[str, Any]]
    spread_ticks: array[float]
    evidence: Any
    marks: dict[str, Any]
    funding_swaprates: dict[str, Any]
    volume_map: dict[str, dict[str, Any]]
    funding_window: Any
    near_mult: float
    far_mult: float
    margin_pair: float


def cached(cache: dict[Any, Any] | None, key: Any, build: Callable[[], Any]) -> Any:
    """Memoise a per-request derivation; a `None` cache means "no caching for this run"."""
    if cache is None:
        return build()
    if key not in cache:
        cache[key] = build()
    return cache[key]


def _inside_cutoff(row: dict[str, Any], through: str, intraday: bool) -> bool:
    intraday_day = len(through) == 10 and intraday
    key = row.get("session_date", row["date"][:10]) if intraday_day else row["date"]
    return key <= through


def _base_window(spec: ReplaySpec) -> tuple[Any, ...]:
    """Cut the prepared series at `through` and derive the cut-specific primitives."""
    series, analysis_rows, window = spec.prepared[0], spec.prepared[1], spec.prepared[2]
    intraday = get_timeframe(series["timeframe"]).intraday
    through = spec.through
    evidence = window.resolve(through) if window is not None else None
    if through is not None:
        series = {
            **series,
            "rows": [row for row in series["rows"] if _inside_cutoff(row, through, intraday)],
        }
        analysis_rows = [row for row in analysis_rows if _inside_cutoff(row, through, intraday)]
    return (
        series,
        analysis_rows,
        array("d", (float(item["spread_ticks"]) for item in analysis_rows)),
        evidence,
        evidence.marks if evidence else {},
        evidence.rates if evidence else {},
        {row["date"]: row for row in series["rows"]},
    )


def resolve_window(spec: ReplaySpec) -> PairMarketWindow:
    """Apply the cut-off and derive everything the replay reuses across parameters."""
    through_cache = spec.cache["through"] if spec.cache is not None else None
    base = cached(through_cache, (spec.through,), lambda: _base_window(spec))
    series, analysis_rows, ticks, evidence, marks, rates, volume_map = base
    near, far = spec.pair["near"], spec.pair["far"]
    near_contract, far_contract = series["contracts"][near], series["contracts"][far]
    quantity = int(spec.execution["quantity"])
    return PairMarketWindow(
        spec=spec,
        series=series,
        analysis_rows=analysis_rows,
        spread_ticks=ticks,
        evidence=evidence,
        marks=marks,
        funding_swaprates=rates,
        volume_map=volume_map,
        funding_window=spec.prepared[2],
        near_mult=float(near_contract["tick_value"]) / float(near_contract["tick_size"]),
        far_mult=float(far_contract["tick_value"]) / float(far_contract["tick_size"]),
        margin_pair=(float(near_contract["initial_margin"]) + float(far_contract["initial_margin"]))
        * quantity,
    )


def sessions_for(window: PairMarketWindow) -> dict[str, Any]:
    """Per-session volumes of both legs, cached for the whole request."""
    spec = window.spec
    near, far = spec.pair["near"], spec.pair["far"]

    def build() -> dict[str, Any]:
        return _session_volumes(spec.inputs, spec.pair, window.series["timeframe"]) or {
            near: {},
            far: {},
        }

    if spec.cache is None:
        return build()
    if "sessions" not in spec.cache:
        spec.cache["sessions"] = build()
    return spec.cache["sessions"]


def _smallest_leg_volume(window: PairMarketWindow) -> float | None:
    sessions = sessions_for(window)
    near, far = window.spec.pair["near"], window.spec.pair["far"]
    near_volumes, far_volumes = sessions.get(near, {}), sessions.get(far, {})

    def leg_volumes(row: dict[str, Any]) -> tuple[Any, Any]:
        session_date = row.get("session_date", row["date"][:10])
        return near_volumes.get(session_date), far_volumes.get(session_date)

    return min_leg_volume(leg_volumes(row) for row in window.series["rows"])


def window_meta(window: PairMarketWindow) -> dict[str, Any]:
    """Liquidity screen for the current cut-off: the smallest min-leg session volume."""
    cache = window.spec.cache
    smallest = cached(
        cache.setdefault("meta", {}) if cache is not None else None,
        window.spec.through,
        lambda: _smallest_leg_volume(window),
    )
    return {
        "min_leg_volume": smallest,
        "tradable_every_day": bool(smallest is not None and smallest >= MIN_DAILY_VOLUME),
        "passes_capital_screen": window.spec.risk["summary"]["all_scenarios_passed"],
    }


def chart_signal_z(window: PairMarketWindow, lookback: int, delay: int) -> dict[str, float | None]:
    """Indicator column for the chart: the delayed rolling z of each analysis bar."""
    cache = window.spec.cache
    z_scores = cached(
        cache["z"] if cache is not None else None,
        (window.spec.through, lookback, delay),
        # The kernel reads any float buffer; the typed array avoids a per-trial list copy.
        lambda: compute_rolling_z(
            cast("list[float]", window.spread_ticks), lookback=lookback, decision_delay_bars=delay
        ),
    )
    return {
        row["date"]: round(float(z_scores[index]), 8)
        if index - delay >= lookback and z_scores[index] is not None
        else None
        for index, row in enumerate(window.analysis_rows)
    }
