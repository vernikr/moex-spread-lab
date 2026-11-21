"""Engine exports and causal entry-band helper for ordinary pair mean reversion."""

from __future__ import annotations

from typing import Any, Mapping

from moex_spread_lab.core.contracts import EntryBandPoint
from ..base import population_rolling_stats
from .analytics import (
    ANALYTICS_SCHEMA,
    COST_SCENARIOS,
    REQUIRED_COST_COMPONENTS,
    AnalyticsError,
    build_daily_series,
    build_series,
    compute_rolling_z,
    get_cost_scenario,
    load_contracts,
    run_mean_reversion_baseline,
)


def compute_pair_entry_bands(
    series: list[dict[str, Any]],
    params: Mapping[str, Any],
    fill: str,
    *,
    far_point_value_rub: float = 1.0,
) -> list[EntryBandPoint]:
    """Compute causal population-std entry bands excluding the current signal bar."""
    window = int(params["lookback"])
    entry_z = float(params["entry_z"])
    history: list[float] = []
    points: list[EntryBandPoint] = []
    for row in series:
        date_str = str(row.get("date", ""))
        if not row.get("volume_eligible", True):
            points.append(EntryBandPoint(date=date_str))
            continue
        value = float(row["spread"])
        sample = history[-window:]
        buy_level = sell_level = buy_rel = sell_rel = None
        if len(sample) == window:
            mean, std = population_rolling_stats(sample)
            if std > 0:
                width = entry_z * std
                buy_level = mean - width
                sell_level = mean + width
                far_val = float(row["far"]) * far_point_value_rub
                if far_val:
                    buy_rel = buy_level / far_val * 100
                    sell_rel = sell_level / far_val * 100
        history.append(value)
        points.append(
            EntryBandPoint(
                date=date_str,
                buy_level=buy_level,
                sell_level=sell_level,
                buy_level_rel=buy_rel,
                sell_level_rel=sell_rel,
            )
        )
    return points


__all__ = [
    "ANALYTICS_SCHEMA",
    "AnalyticsError",
    "COST_SCENARIOS",
    "REQUIRED_COST_COMPONENTS",
    "build_daily_series",
    "build_series",
    "compute_pair_entry_bands",
    "compute_rolling_z",
    "get_cost_scenario",
    "load_contracts",
    "run_mean_reversion_baseline",
]
