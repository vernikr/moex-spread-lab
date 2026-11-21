"""Signal-date entry boundaries in the raw spread's own price units.

Delegates per-strategy causal band calculation to the registered ``StrategyPlugin``
so adding new strategy folders never requires editing shared signal attachment logic.
"""

from __future__ import annotations

from typing import Any

from moex_spread_lab.core.w0 import W0Error
from .base import (
    ENTRY_BAND_NOTE_RU,
    NON_PRICE_SIGNAL_NOTE_RU,
    PRICE_FILLS,
    REFERENCE_WARNING,
    discover_strategies,
)


def pair_fill(request: dict[str, Any], strategy: str, *, default_price_fill: str = "reference_same_bar") -> str:
    registry = discover_strategies()
    plugin = registry.get(strategy) if strategy in registry else None
    if plugin is not None and plugin.supported_fills == ("same_day_settlement",):
        default = "same_day_settlement"
        allowed: tuple[str, ...] | dict[str, Any] = plugin.supported_fills
    elif strategy == "funding_only":
        default = "same_day_settlement"
        allowed = ("same_day_settlement",)
    else:
        default = default_price_fill
        allowed = plugin.supported_fills if plugin is not None else PRICE_FILLS
    fill = request.get("fill", default)
    if not isinstance(fill, str) or fill not in allowed:
        raise W0Error("Выбранный режим исполнения не поддерживается этой стратегией.")
    return fill


def attach_entry_levels(proof: dict[str, Any]) -> None:
    """Attach two causal entry bands without changing any signal, trade or indicator."""
    selection = proof["selection"]
    strategy = selection["strategy"]
    native = proof.get("universe") == "calendar_native"
    params = selection["parameters"]
    fill = selection.get("fill", "reference_same_bar")
    reference = fill == "reference_same_bar"
    proof["execution_reference"] = selection["execution_reference"] = reference

    registry = discover_strategies()
    plugin = registry.get(strategy)
    if plugin.supported_fills == ("same_day_settlement",):
        proof["signal_levels"] = {
            "kind": "not_price_based",
            "note": NON_PRICE_SIGNAL_NOTE_RU,
        }
        return

    proof["signal_levels"] = {
        "kind": "entry_bands",
        "basis": "signal_bar_close",
        "note": ENTRY_BAND_NOTE_RU,
        "fill_delay_bars": 0 if reference else 1,
    }
    far_point = 1.0 if native else float(proof["instrument"]["far_point_value_rub"])
    bands = plugin.compute_entry_bands(
        proof["series"],
        params,
        fill,
        far_point_value_rub=far_point,
    )
    for row, band in zip(proof["series"], bands, strict=True):
        row["buy_level"] = band.buy_level
        row["sell_level"] = band.sell_level
        row["buy_level_rel"] = band.buy_level_rel
        row["sell_level_rel"] = band.sell_level_rel


__all__ = [
    "PRICE_FILLS",
    "REFERENCE_WARNING",
    "attach_entry_levels",
    "pair_fill",
]
