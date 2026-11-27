from __future__ import annotations

from collections.abc import Mapping
from importlib import import_module
from typing import Any

from .engine import compute_calendar_zscore_entry_bands, zscore_trades

ce: Any = import_module("moex_spread_lab.strategies.calendar_eval")


def _schema(self: Any, context: Any = None) -> Any:
    del self
    a = bool(context and context.monthly)
    b = ce.ZSCORE_CALENDAR_OPTIONS["monthly" if a else "quarterly"]["window"]
    c = "Окно · баров" if context and context.timeframe == "1h" else "Окно · дней"
    return (
        ce.ParameterSpec("window", c, "int", b, 3, 60, 1),
        ce.ParameterSpec("entry_z", "Вход · z", "float", 1.5, 0.5, 5.0, 0.1),
        ce.ParameterSpec("exit_z", "Выход · z", "float", 0.5, 0.1, 4.0, 0.1),
    )


def _scan(scan: Any) -> list[dict[str, Any]]:
    b = "monthly" if scan.monthly else "quarterly"
    a = {**ce.ZSCORE_CALENDAR_OPTIONS[b], **scan.params}
    return zscore_trades(
        scan.rows,
        delay=scan.delay,
        fill=scan.quote,
        z_cache=scan.window.extra.get("z_cache"),
        z_cache_key=scan.window.extra.get("z_cache_key"),
        entry_z=float(a["entry_z"]),
        exit_z=float(a["exit_z"]),
        window=int(a["window"]),
        max_hold_days=int(a["max_hold_days"]),
        close_before=int(a["close_before"]),
        skip_dividend_windows=False,
        close_at_end=True,
    )


def _validate(self: Any, raw: Any, context: Any = None) -> Any:
    return ce.calendar_parameters(self, raw, context)


def _check(self: Any, window: Any, settings: Any = None) -> Any:
    del self
    return ce.calendar_compatibility(window, settings)


def _evaluate(self: Any, window: Any, params: Any, fill: str) -> Any:
    return ce.calendar_evaluate_intents(self, window, params, fill)


def _bands(self: Any, *args: Any, **kwargs: Any) -> Any:
    return ce.calendar_entry_bands(self, *args, **kwargs)


class CalendarZscorePlugin:
    strategy_id = "zscore_mean_reversion"
    family_id = "mean_reversion"
    label_ru = "Возврат к среднему · z-скор"
    short_label_ru = "возврат к среднему"
    supported_universes = frozenset({ce.CALENDAR_UNIVERSE})
    supported_fills = "next_open", "next_close", "reference_same_bar"
    requires_daily_bars = False
    family_parameter_aliases: Mapping[str, str] = {"lookback": "window"}
    ui_field_order: tuple[str, ...] = ("entry_z", "exit_z", "window")
    proof_adapter: str | None = "calendar_price_intents.v1"
    trust_tier = "builtin"
    monthly_defaults = True
    band_keys = "window", "entry_z", "exit_z"
    parameter_schema = _schema
    evaluate_intents = _evaluate
    validate_parameters = _validate
    compute_entry_bands = _bands
    check_compatibility = _check
    scan_trades = staticmethod(_scan)
    band_engine = staticmethod(compute_calendar_zscore_entry_bands)


PLUGIN = CalendarZscorePlugin()
