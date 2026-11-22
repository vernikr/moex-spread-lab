"""Backward-compatible shim for ``moex_spread_lab.strategies.pair_screener.pair_screener``."""

from moex_spread_lab.strategies.pair_screener.pair_screener import *  # noqa: F403
from moex_spread_lab.strategies.pair_screener.pair_screener import (  # noqa: F401
    _CANDLE_SUMMARY_CACHE,
    _candle_summary,
    _history_source,
    _leg_volumes,
    _offline_only,
    _pair,
    _pair_history_span,
    _parameters,
    _positive_leg_volume_dates,
    _session_volumes,
)
