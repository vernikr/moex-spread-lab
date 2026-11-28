"""Generic universe identifiers.

The concrete MOEX catalogs live in ``moex_spread_lab.data.universe``; callers must
import them there (the migration is complete and the legacy ``core.universe`` shim
is gone). Keep this module dependency-free so it stays safe to import from Layer 0.
"""

from __future__ import annotations

from .settings import (
    DEFAULT_INITIAL_CAPITAL_RUB,
    INITIAL_CAPITAL,
    MAX_QUANTITY,
    MIN_DAILY_VOLUME,
)

PAIR_UNIVERSE = "pair_screen"
CALENDAR_UNIVERSE = "calendar_native"

__all__ = [
    "CALENDAR_UNIVERSE",
    "DEFAULT_INITIAL_CAPITAL_RUB",
    "INITIAL_CAPITAL",
    "MAX_QUANTITY",
    "MIN_DAILY_VOLUME",
    "PAIR_UNIVERSE",
]
