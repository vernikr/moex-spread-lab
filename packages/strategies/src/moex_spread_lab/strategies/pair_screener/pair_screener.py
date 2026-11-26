"""Offline pair-screener domain logic, input reader, and catalog builder."""

from __future__ import annotations

from moex_spread_lab.core.settings import INITIAL_CAPITAL, MAX_QUANTITY, MIN_DAILY_VOLUME
from moex_spread_lab.data.universe import HISTORY_WINDOWS, PAIRS

from ..funding_carry.carry import fetch_history
from ..funding_carry.proof_funding import FundingWindow, ProofFunding
from .catalog import catalog
from .inputs import (
    _CANDLE_SUMMARY_CACHE,
    ScreenerError,
    ScreenerInputs,
    _candle_summary,
    _history_source,
    _leg_volumes,
    _make_proof_funding,
    _offline_only,
    _pair,
    _pair_history_span,
    _positive_leg_volume_dates,
    _session_volumes,
    pair_price_inputs,
)
from .runner import STRATEGIES, _parameters, build_result, execution_settings

__all__ = [
    "HISTORY_WINDOWS",
    "INITIAL_CAPITAL",
    "MAX_QUANTITY",
    "MIN_DAILY_VOLUME",
    "PAIRS",
    "STRATEGIES",
    "_CANDLE_SUMMARY_CACHE",
    "FundingWindow",
    "ProofFunding",
    "ScreenerError",
    "ScreenerInputs",
    "_candle_summary",
    "_history_source",
    "_leg_volumes",
    "_make_proof_funding",
    "_offline_only",
    "_pair",
    "_pair_history_span",
    "_parameters",
    "_positive_leg_volume_dates",
    "_session_volumes",
    "build_result",
    "catalog",
    "execution_settings",
    "fetch_history",
    "pair_price_inputs",
]
