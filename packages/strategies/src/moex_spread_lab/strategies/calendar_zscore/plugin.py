"""Declarative ``StrategyPlugin`` for native/H1 calendar spread Z-score mean reversion."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from moex_spread_lab.core.contracts import (
    CompatibilityVerdict,
    EntryBandPoint,
    InstrumentContext,
    ParameterSpec,
    StrategyIntentRun,
    WindowSnapshot,
)
from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error

from ..base import (
    PRICE_FILLS,
    ZSCORE_CALENDAR_OPTIONS,
    check_calendar_liquidity,
    validate_parameter_map,
)
from .engine import compute_calendar_zscore_entry_bands, zscore_trades


class CalendarZscorePlugin:
    strategy_id = "zscore_mean_reversion"
    family_id = "mean_reversion"
    label_ru = "Возврат к среднему · z-скор"
    short_label_ru = "возврат к среднему"
    supported_universes = frozenset({CALENDAR_UNIVERSE})
    supported_fills = ("next_open", "next_close", "reference_same_bar")
    requires_daily_bars = False

    # Declarative adapter metadata (consumed via moex_spread_lab.core.contracts helpers).
    proof_adapter: str | None = "calendar_price_intents.v1"
    family_parameter_aliases: Mapping[str, str] = {"lookback": "window"}
    ui_field_order: tuple[str, ...] = ("entry_z", "exit_z", "window")
    trust_tier: Literal["builtin", "plugin"] = "builtin"

    def parameter_schema(
        self, context: InstrumentContext | None = None
    ) -> tuple[ParameterSpec, ...]:
        monthly = bool(context and context.monthly)
        default_win = ZSCORE_CALENDAR_OPTIONS["monthly" if monthly else "quarterly"]["window"]
        label = "Окно · баров" if (context and context.timeframe == "1h") else "Окно · дней"
        return (
            ParameterSpec("window", label, "int", default_win, 3, 60, 1),
            ParameterSpec("entry_z", "Вход · z", "float", 1.5, 0.5, 5.0, 0.1),
            ParameterSpec("exit_z", "Выход · z", "float", 0.5, 0.1, 4.0, 0.1),
        )

    def validate_parameters(
        self, raw: Mapping[str, Any], context: InstrumentContext | None = None
    ) -> dict[str, Any]:
        return validate_parameter_map(raw, self.parameter_schema(context))

    def check_compatibility(
        self, window: WindowSnapshot, settings: ResearchSettings | None = None
    ) -> CompatibilityVerdict:
        if window.universe not in self.supported_universes:
            return CompatibilityVerdict(False, "Выбранная стратегия неприменима к этому типу пары.")
        verdict, _ = check_calendar_liquidity(window, settings)
        return verdict if verdict is not None else CompatibilityVerdict(True)

    def evaluate_intents(
        self, window: WindowSnapshot, params: Mapping[str, Any], fill: str
    ) -> StrategyIntentRun:
        if fill not in PRICE_FILLS:
            raise W0Error("Выбранный режим исполнения не поддерживается этой стратегией.")
        monthly = bool(window.extra.get("monthly", False))
        ctx = InstrumentContext(
            universe=window.universe,
            pair=window.pair,
            asset=window.asset,
            timeframe=window.timeframe,
            monthly=monthly,
        )
        validated = self.validate_parameters(params, ctx)
        verdict = self.check_compatibility(window)
        if not verdict.compatible:
            raise W0Error(verdict.reason or "incompatible calendar window")
        delay, fill_quote = PRICE_FILLS[fill]
        freq = "monthly" if monthly else "quarterly"
        opts = {**ZSCORE_CALENDAR_OPTIONS[freq], **validated}
        analysis_rows = (
            list(window.rows)
            if window.timeframe == "1h"
            else [r for r in window.rows if (r.get("min_volume") or 0) > 0]
        )
        # The shared Z cache keeps the request-scoped cache telemetry and the repeated-trial
        # behaviour identical to the historical runner.
        _extra = window.extra if isinstance(window.extra, Mapping) else {}
        trades = zscore_trades(
            analysis_rows,
            delay=delay,
            fill=fill_quote,
            z_cache=_extra.get("z_cache"),
            z_cache_key=_extra.get("z_cache_key"),
            entry_z=float(opts["entry_z"]),
            exit_z=float(opts["exit_z"]),
            window=int(opts["window"]),
            max_hold_days=int(opts["max_hold_days"]),
            close_before=int(opts["close_before"]),
            skip_dividend_windows=False,
            close_at_end=True,
        )
        last_day = window.rows[-1]["date"] if window.rows else None
        intents = tuple(
            {
                "side": "short_spread" if item["direction"] < 0 else "long_spread",
                "entry_date": item["entry"]["date"],
                "exit_date": item["exit"]["date"],
                "entry_price": item["entry_price"],
                "exit_price": item["exit_price"],
                "signal": item["signal"],
                # ``open_at_end`` items come from the terminal mark without a closed reason.
                "reason": item.get("reason") or "end_of_window",
                # A terminal mark on the very bar the position opened is reported as an open
                # position rather than a zero-holding round trip the verifier must reject.
                **({"open_at_end": True} if item["entry"]["date"] == last_day else {}),
            }
            for item in trades
        )
        return StrategyIntentRun(
            strategy_id=self.strategy_id,
            fill=fill,
            parameters=validated,
            intents=intents,
        )

    def compute_entry_bands(
        self,
        series: list[dict[str, Any]],
        params: Mapping[str, Any],
        fill: str,
        *,
        far_point_value_rub: float = 1.0,
    ) -> list[EntryBandPoint]:
        if fill not in PRICE_FILLS:
            raise W0Error("Выбранный режим исполнения не поддерживается этой стратегией.")
        relevant = {k: params[k] for k in ("window", "entry_z", "exit_z") if k in params}
        validated = self.validate_parameters(relevant)
        return compute_calendar_zscore_entry_bands(
            series, validated, fill, far_point_value_rub=far_point_value_rub
        )


PLUGIN = CalendarZscorePlugin()
