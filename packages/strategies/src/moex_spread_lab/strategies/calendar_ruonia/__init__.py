"""Implied-rate convergence to RUONIA + dividend-window adjustment strategy subpackage."""

from .engine import (
    DIVIDEND_SHARES_PER_CONTRACT,
    QUARTER_DAYS,
    SBER_EX_DATES,
    compute_ruonia_entry_bands,
    convergence_trades,
    implied_ruonia_deviation_pp,
    window_dividend_rub,
)
from .plugin import PLUGIN, CalendarRuoniaPlugin

__all__ = [
    "DIVIDEND_SHARES_PER_CONTRACT",
    "PLUGIN",
    "QUARTER_DAYS",
    "SBER_EX_DATES",
    "CalendarRuoniaPlugin",
    "compute_ruonia_entry_bands",
    "convergence_trades",
    "implied_ruonia_deviation_pp",
    "window_dividend_rub",
]
