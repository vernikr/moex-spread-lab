"""Generic universe identifiers and strategy parameter defaults (Layer 0).

The concrete MOEX instrument catalogs live in ``moex_spread_lab.data.universe``
(Layer 1); this module keeps only the layer-neutral identifiers and settings so
``msl-core`` stays installable and importable in isolation. The Layer-1 names are
still importable from here through a documented deprecation accessor for the
migration window — see ``__getattr__``.
"""

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

# Layer-1 catalog names that historically lived here. The bare annotations keep
# the historical star-import surface visible to static tools without creating
# module attributes, and ``__getattr__`` resolves the values lazily so importing
# ``msl-core`` never pulls in ``msl-data``. Use ``moex_spread_lab.data.universe``
# for new code. Removal window: after the Phase 7 waves have soaked.
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

_DEPRECATED_CATALOG_NAMES = frozenset(
    {
        "BASE_PAIRS",
        "EXTRA_PAIRS",
        "GLD_HISTORICAL_FAR",
        "GLD_HISTORICAL_PAIRS",
        "HISTORICAL_FAR_BY_NEAR",
        "HISTORICAL_PERPETUAL_PAIRS",
        "HISTORY_WINDOWS",
        "PAIRS",
        "PERPETUAL_HISTORY_STARTS",
        "PERPETUAL_HISTORY_TILL",
        "SCREEN_PAIRS",
    }
)


def __getattr__(name: str) -> object:
    """Deprecated Layer-1 catalog access; import from ``moex_spread_lab.data.universe``."""
    if name in _DEPRECATED_CATALOG_NAMES:
        import importlib

        return getattr(importlib.import_module("moex_spread_lab.data.universe"), name)
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
