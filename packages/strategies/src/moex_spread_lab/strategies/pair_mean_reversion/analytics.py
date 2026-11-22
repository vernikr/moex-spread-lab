"""Offline analytical spread baseline over ISS candle-close proxies."""

from __future__ import annotations

from ..base import population_rolling_stats as _rolling_stats
from .baseline_runner import compute_rolling_z, run_mean_reversion_baseline
from .series_builder import (
    ANALYTICS_SCHEMA,
    COST_SCENARIOS,
    REQUIRED_COST_COMPONENTS,
    AnalyticsError,
    _calendar_days,
    _date_key,
    _load_benchmark,
    _load_candles,
    _number,
    _round,
    _trade_cost_breakdown,
    _validate_cost_profile,
    build_daily_series,
    build_series,
    get_cost_scenario,
    load_contracts,
)

__all__ = [
    "ANALYTICS_SCHEMA",
    "COST_SCENARIOS",
    "REQUIRED_COST_COMPONENTS",
    "AnalyticsError",
    "_calendar_days",
    "_date_key",
    "_load_benchmark",
    "_load_candles",
    "_number",
    "_rolling_stats",
    "_round",
    "_trade_cost_breakdown",
    "_validate_cost_profile",
    "build_daily_series",
    "build_series",
    "compute_rolling_z",
    "get_cost_scenario",
    "load_contracts",
    "run_mean_reversion_baseline",
]
