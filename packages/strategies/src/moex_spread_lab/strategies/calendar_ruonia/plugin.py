from __future__ import annotations

import math
from importlib import import_module
from typing import Any

from .engine import compute_ruonia_entry_bands, convergence_trades

ce: Any = import_module("moex_spread_lab.strategies.calendar_eval")
_SCHEMA = (ce.ParameterSpec("threshold_pp", "Порог · п.п. от RUONIA", "float", 1.0, 0.1, 1e1, 0.1),)
_CONVERGENCE_ASSETS = frozenset({"SR", "GZ"})


def _precheck(window: Any) -> Any:
    a = window.extra.get("convergence")
    if a is False or (a is None and window.asset not in _CONVERGENCE_ASSETS):
        return ce.CompatibilityVerdict(False, "Для этого актива не предусмотрена стратегия RUONIA.")
    return None


def _series_usable(eligible: list[dict[str, Any]]) -> bool:
    return any(math.isfinite(float(a.get("dev_pp", math.nan))) for a in eligible)


def _scan(scan: Any) -> list[dict[str, Any]]:
    return convergence_trades(
        scan.rows,
        threshold=float(scan.params["threshold_pp"]),
        delay=scan.delay,
        fill=scan.quote,
        close_at_end=True,
        z_cache=scan.window.extra.get("z_cache"),
        z_cache_key=scan.window.extra.get("z_cache_key"),
    )


def _schema(self: Any, context: Any = None) -> Any:
    del self, context
    return _SCHEMA


def _check(self: Any, window: Any, settings: Any = None) -> Any:
    del self
    return ce.calendar_compatibility(
        window,
        settings,
        precheck=_precheck,
        series_usable=_series_usable,
        unavailable_reason="RUONIA deviation series is unavailable",
    )


def _validate(self: Any, raw: Any, context: Any = None) -> Any:
    return ce.calendar_parameters(self, raw, context)


def _evaluate(self: Any, window: Any, params: Any, fill: str) -> Any:
    return ce.calendar_evaluate_intents(self, window, params, fill)


def _bands(self: Any, *args: Any, **kwargs: Any) -> Any:
    return ce.calendar_entry_bands(self, *args, **kwargs)


class CalendarRuoniaPlugin:
    strategy_id = "convergence_to_ruonia"
    family_id = "convergence_to_ruonia"
    label_ru = "Сходимость к справедливой цене RUONIA"
    short_label_ru = "сходимость к RUONIA"
    supported_universes = frozenset({ce.CALENDAR_UNIVERSE})
    supported_fills = "next_open", "next_close", "reference_same_bar"
    requires_daily_bars = False
    required_asset_capability: str | None = "convergence"
    proof_adapter: str | None = "calendar_price_intents.v1"
    trust_tier = "builtin"
    incompatible_reason = "incompatible RUONIA window"
    monthly_defaults = False
    band_keys = ("threshold_pp",)
    scan_trades = staticmethod(_scan)
    band_engine = staticmethod(compute_ruonia_entry_bands)
    parameter_schema = _schema
    validate_parameters = _validate
    check_compatibility = _check
    evaluate_intents = _evaluate
    compute_entry_bands = _bands


PLUGIN = CalendarRuoniaPlugin()
