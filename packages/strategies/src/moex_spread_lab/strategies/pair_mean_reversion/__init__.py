"""Ordinary futures-pair rolling Z-score mean-reversion strategy subpackage."""

from .engine import (
    ANALYTICS_SCHEMA,
    AnalyticsError,
    COST_SCENARIOS,
    REQUIRED_COST_COMPONENTS,
    build_daily_series,
    build_series,
    compute_pair_entry_bands,
    compute_rolling_z,
    get_cost_scenario,
    load_contracts,
    run_mean_reversion_baseline,
)
from .plugin import PLUGIN, PairMeanReversionPlugin

__all__ = [
    "ANALYTICS_SCHEMA",
    "AnalyticsError",
    "COST_SCENARIOS",
    "PLUGIN",
    "PairMeanReversionPlugin",
    "REQUIRED_COST_COMPONENTS",
    "build_daily_series",
    "build_series",
    "compute_pair_entry_bands",
    "compute_rolling_z",
    "get_cost_scenario",
    "load_contracts",
    "run_mean_reversion_baseline",
]
