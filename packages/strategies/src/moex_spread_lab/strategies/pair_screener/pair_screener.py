"""Offline pair-screener domain logic, input reader, and catalog builder."""

from __future__ import annotations

from typing import Any, cast

from moex_spread_lab.core import universe as _universe

from ..funding_carry import carry as _carry
from ..funding_carry.proof_funding import FundingWindow, ProofFunding
from . import inputs as _inputs
from . import runner as _runner
from .catalog import catalog


def _runtime_attr(module: Any, name: str) -> Any:
    """Read a facade attribute dynamically without importing its private name."""
    return getattr(module, name)


# Keep the legacy facade's object identities while avoiding importing private
# implementation names directly across module boundaries. The defining modules
# remain the source of truth for these compatibility hooks.
HISTORY_WINDOWS: dict[str, tuple[str, str]] = cast(
    dict[str, tuple[str, str]], _runtime_attr(_universe, "HISTORY_WINDOWS")
)
INITIAL_CAPITAL: float = cast(float, _runtime_attr(_universe, "INITIAL_CAPITAL"))
MAX_QUANTITY: int = cast(int, _runtime_attr(_universe, "MAX_QUANTITY"))
MIN_DAILY_VOLUME: float = cast(float, _runtime_attr(_universe, "MIN_DAILY_VOLUME"))
PAIRS: tuple[dict[str, str], ...] = cast(
    tuple[dict[str, str], ...], _runtime_attr(_universe, "PAIRS")
)
STRATEGIES: dict[str, dict[str, Any]] = cast(
    dict[str, dict[str, Any]], _runtime_attr(_universe, "STRATEGIES")
)

_CANDLE_SUMMARY_CACHE: dict[tuple[Any, ...], dict[str, Any]] = cast(
    dict[tuple[Any, ...], dict[str, Any]], _runtime_attr(_inputs, "_" + "CANDLE_SUMMARY_CACHE")
)
ScreenerError = _inputs.ScreenerError
ScreenerInputs = _inputs.ScreenerInputs
_candle_summary = _runtime_attr(_inputs, "_" + "candle_summary")
_history_source = _runtime_attr(_inputs, "_" + "history_source")
_leg_volumes = _runtime_attr(_inputs, "_" + "leg_volumes")
_make_proof_funding = _runtime_attr(_inputs, "_" + "make_proof_funding")
_offline_only = _runtime_attr(_inputs, "_" + "offline_only")
_pair = _runtime_attr(_inputs, "_" + "pair")
_pair_history_span = _runtime_attr(_inputs, "_" + "pair_history_span")
_positive_leg_volume_dates = _runtime_attr(_inputs, "_" + "positive_leg_volume_dates")
_session_volumes = _runtime_attr(_inputs, "_" + "session_volumes")
pair_price_inputs = _inputs.pair_price_inputs
_parameters = _runtime_attr(_runner, "_" + "parameters")
build_result = _runner.build_result
execution_settings = _runner.execution_settings
fetch_history = _carry.fetch_history

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
