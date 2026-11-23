"""Read-only MOEX EOD history and funding-aware CNY carry decomposition."""

from __future__ import annotations

from moex_spread_lab.core.w0 import load_contracts
from moex_spread_lab.data.iss.catalog import (
    HISTORY_FIELDS,
    HISTORY_SCHEMA,
    IssHistoryError,
    _history_url,
)

from .strategy_costs import (
    BENCHMARK_PARSED_CACHE as _BENCHMARK_PARSED_CACHE,
)
from .strategy_costs import (
    benchmark_rows as _benchmark_rows,
)
from .strategy_costs import (
    transaction_cost_profile as _transaction_cost_profile,
)
from .strategy_runner import build_carry_strategy
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
    "HISTORY_SCHEMA",
    "STRATEGY_SCHEMA",
    "_BENCHMARK_PARSED_CACHE",
    "CarryError",
    "CarryStrategyError",
    "IssHistoryError",
    "_benchmark_rows",
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
