"""Compatibility re-exports for MOEX EOD history and funding-carry evaluation."""

from __future__ import annotations

from moex_spread_lab.core.w0 import load_contracts
from moex_spread_lab.data.iss.catalog import HISTORY_FIELDS, _history_url

from .engine import build_carry_strategy
from .engine import transaction_cost_profile as _transaction_cost_profile
from .study import (
    CARRY_SCHEMA,
    FUNDING_REGIMES,
    STRATEGY_SCHEMA,
    CarryError,
    CarryStrategyError,
    _funding_regime,
    _normalize_history,
    _round,
    build_carry_study,
    fetch_history,
)

__all__ = [
    "CARRY_SCHEMA",
    "FUNDING_REGIMES",
    "HISTORY_FIELDS",
    "STRATEGY_SCHEMA",
    "CarryError",
    "CarryStrategyError",
    "_funding_regime",
    "_history_url",
    "_normalize_history",
    "_round",
    "_transaction_cost_profile",
    "build_carry_strategy",
    "build_carry_study",
    "fetch_history",
    "load_contracts",
]
