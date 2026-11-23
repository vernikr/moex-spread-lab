"""Mean-reversion pair proof engine.

Derives the cut-off window and the reusable per-parameter primitives for the fixed
quantity z-score strategy over one futures pair.
"""

from __future__ import annotations

from array import array
from collections.abc import Callable
from typing import Any

from moex_spread_lab.accounting.costs import tariff_profile
from moex_spread_lab.core.timeframes import get_timeframe
from moex_spread_lab.strategies import PRICE_FILLS
from moex_spread_lab.strategies.pair_mean_reversion.analytics import (
    compute_rolling_z,
    run_mean_reversion_baseline,
)

from .mean_reversion_spec import (
    MeanReversionSpec,
    MeanReversionWindow,
)


def _cached(cache: dict[Any, Any] | None, key: tuple[Any, ...], build: Callable[[], Any]) -> Any:
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


def _base_window(spec: MeanReversionSpec) -> tuple[Any, ...]:
    """Cut the prepared series at `through` and derive the cut-specific primitives."""
    series, analysis_rows, _, _, _ = spec.prepared
    intraday = get_timeframe(series["timeframe"]).intraday
    through = spec.through
    evidence = None
    window = spec.prepared[2]
    if window is not None:
        evidence = window.resolve(through)
    if through is not None:
        series = {
            **series,
            "rows": [row for row in series["rows"] if _inside_cutoff(row, through, intraday)],
        }
        analysis_rows = [row for row in analysis_rows if _inside_cutoff(row, through, intraday)]
    return (
        series,
        analysis_rows,
        {**series, "rows": analysis_rows},
        array("d", (float(item["spread_ticks"]) for item in analysis_rows)),
        evidence,
        evidence.marks if evidence else {},
        evidence.rates if evidence else {},
        {row["date"]: row for row in series["rows"]},
    )


def _resolve_window(spec: MeanReversionSpec) -> MeanReversionWindow:
    """Apply the cut-off and derive everything the engine reuses across parameters."""
    cache = spec.cache
    through_cache = cache["through"] if cache is not None else None
    base = _cached(through_cache, (spec.through,), lambda: _base_window(spec))
    series, analysis_rows, analysis_series, ticks, evidence, marks, rates, volume_map = base
    funding_window = spec.prepared[2]
    near, far = spec.pair["near"], spec.pair["far"]
    near_contract, far_contract = series["contracts"][near], series["contracts"][far]
    delay = PRICE_FILLS[spec.execution["fill"]][0]
    lookback = int(spec.params["lookback"])
    quantity = int(spec.execution["quantity"])
    z_scores = _cached(
        cache["z"] if cache is not None else None,
        (spec.through, lookback, delay),
        lambda: compute_rolling_z(ticks, lookback=lookback, decision_delay_bars=delay),
    )
    signal_dates = _cached(
        cache["signal_dates"] if cache is not None else None,
        (spec.through, delay),
        lambda: {
            row["date"]: analysis_rows[max(0, index - delay)]["date"]
            for index, row in enumerate(analysis_rows)
        },
    )
    return MeanReversionWindow(
        spec=spec,
        series=series,
        analysis_rows=analysis_rows,
        analysis_series=analysis_series,
        spread_ticks=ticks,
        evidence=evidence,
        marks=marks,
        funding_swaprates=rates,
        volume_map=volume_map,
        funding_window=funding_window,
        near_mult=float(near_contract["tick_value"]) / float(near_contract["tick_size"]),
        far_mult=float(far_contract["tick_value"]) / float(far_contract["tick_size"]),
        margin_pair=(float(near_contract["initial_margin"]) + float(far_contract["initial_margin"]))
        * quantity,
        z_scores=z_scores,
        signal_dates=signal_dates,
    )


resolve_window = _resolve_window


def report_for(window: MeanReversionWindow, *, metrics_only: bool) -> dict[str, Any]:
    spec = window.spec
    execution, params = spec.execution, spec.params
    delay, fill_quote = PRICE_FILLS[execution["fill"]]
    return run_mean_reversion_baseline(
        window.analysis_series,
        initial_capital=execution["initial_capital_rub"],
        lookback=int(params["lookback"]),
        entry_z=float(params["entry_z"]),
        exit_z=float(params["exit_z"]),
        quantity=int(execution["quantity"]),
        analysis_role="diagnostic_only",
        cost_profile=tariff_profile(float(execution["slippage_ticks"])),
        fill=fill_quote,
        decision_delay_bars=delay,
        cash_in_lqdt=True,
        funding_swaprates=window.funding_swaprates,
        sizing_intents=bool(execution.get("sizing_intents", False)),
        metrics_only=metrics_only,
        precomputed_z=window.z_scores,
    )
