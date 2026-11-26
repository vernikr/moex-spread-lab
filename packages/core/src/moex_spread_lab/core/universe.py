"""Generic universe identifiers and strategy defaults."""

from __future__ import annotations

from typing import Any

from .settings import (
    DEFAULT_INITIAL_CAPITAL_RUB,
    INITIAL_CAPITAL,
    MAX_QUANTITY,
    MIN_DAILY_VOLUME,
)

PAIR_UNIVERSE = "pair_screen"
CALENDAR_UNIVERSE = "calendar_native"

STRATEGIES = {
    "mean_reversion": {
        "label": "Возврат спреда к среднему",
        "parameters": {"lookback": 20, "entry_z": 2.0, "exit_z": 0.5},
    },
    "funding_only": {
        "label": "Фондирование перпетуального свопа (carry)",
        "parameters": {"lookback": 20},
    },
}


BASE_PAIRS: Any
EXTRA_PAIRS: Any
GLD_HISTORICAL_FAR: Any
GLD_HISTORICAL_PAIRS: Any
HISTORICAL_FAR_BY_NEAR: Any
HISTORICAL_PERPETUAL_PAIRS: Any
HISTORY_WINDOWS: Any
PAIRS: Any
PERPETUAL_HISTORY_STARTS: Any
PERPETUAL_HISTORY_TILL: Any
SCREEN_PAIRS: Any


def __getattr__(name: str) -> object:
    if name in __all__ and name not in globals():
        from importlib import import_module

        return getattr(import_module("moex_spread_lab.data.universe"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BASE_PAIRS",
    "CALENDAR_UNIVERSE",
    "DEFAULT_INITIAL_CAPITAL_RUB",
    "EXTRA_PAIRS",
    "GLD_HISTORICAL_FAR",
    "GLD_HISTORICAL_PAIRS",
    "HISTORICAL_FAR_BY_NEAR",
    "HISTORICAL_PERPETUAL_PAIRS",
    "HISTORY_WINDOWS",
    "INITIAL_CAPITAL",
    "MAX_QUANTITY",
    "MIN_DAILY_VOLUME",
    "PAIRS",
    "PAIR_UNIVERSE",
    "PERPETUAL_HISTORY_STARTS",
    "PERPETUAL_HISTORY_TILL",
    "SCREEN_PAIRS",
    "STRATEGIES",
]
