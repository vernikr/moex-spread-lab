"""Offline futures-pair screener domain logic, input reader, and catalog builder."""

from .catalog import catalog
from .inputs import ScreenerError, ScreenerInputs, _pair, _session_volumes, pair_price_inputs
from .runner import STRATEGIES, _parameters, build_result, execution_settings

__all__ = [
    "STRATEGIES",
    "ScreenerError",
    "ScreenerInputs",
    "_pair",
    "_parameters",
    "_session_volumes",
    "build_result",
    "catalog",
    "execution_settings",
    "pair_price_inputs",
]
