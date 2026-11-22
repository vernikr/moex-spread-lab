"""Declarative ``StrategyPlugin`` for native/H1 calendar spread Z-score mean reversion."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from moex_spread_lab.core.contracts import (
    CompatibilityVerdict,
    EntryBandPoint,
    InstrumentContext,
    ParameterSpec,
    StrategyIntentRun,
    WindowSnapshot,
)
from moex_spread_lab.core.settings import MIN_DAILY_VOLUME, ResearchSettings
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error

from ..base import PRICE_FILLS, ZSCORE_CALENDAR_OPTIONS, validate_parameter_map
from .engine import compute_calendar_zscore_entry_bands, zscore_trades


class CalendarZscorePlugin:
    strategy_id = "zscore_mean_reversion"
    family_id = "mean_reversion"
    label_ru = "Возврат к среднему · z-скор"
    short_label_ru = "возврат к среднему"
    supported_universes = frozenset({CALENDAR_UNIVERSE})
    supported_fills = ("next_open", "next_close", "reference_same_bar")
    requires_daily_bars = False

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
        if window.timeframe == "1h":
            eligible = [
                r for r in window.rows if r.get("volume_eligible", (r.get("min_volume") or 0) > 0)
            ]
            if len(eligible) < 2:
                return CompatibilityVerdict(
                    False, "fewer than two causally liquidity-eligible hourly observations"
                )
            if settings is not None and not settings.allow_failed_volume:
                if not window.extra.get("tradable_every_session", True):
                    return CompatibilityVerdict(
                        False, "Не пройдено правило ликвидности на обучении."
                    )
        else:
            eligible = [r for r in window.rows if (r.get("min_volume") or 0) > 0]
            if len(eligible) < 2:
                return CompatibilityVerdict(
                    False, "fewer than two positive-volume calendar-spread observations"
                )
            if settings is not None and not settings.allow_failed_volume:
                min_vol = min(float(r["min_volume"]) for r in eligible)
                if min_vol < MIN_DAILY_VOLUME:
                    return CompatibilityVerdict(
                        False, "Не пройдено правило ликвидности на обучении."
                    )
        return CompatibilityVerdict(True)

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
        trades = zscore_trades(
            analysis_rows,
            delay=delay,
            fill=fill_quote,
            entry_z=float(opts["entry_z"]),
            exit_z=float(opts["exit_z"]),
            window=int(opts["window"]),
            max_hold_days=int(opts["max_hold_days"]),
            close_before=int(opts["close_before"]),
            skip_dividend_windows=False,
            close_at_end=True,
        )
        intents = tuple(
            {
                "side": "short_spread" if item["direction"] < 0 else "long_spread",
                "entry_date": item["entry"]["date"],
                "exit_date": item["exit"]["date"],
                "entry_price": item["entry_price"],
                "exit_price": item["exit_price"],
                "signal": item["signal"],
                "reason": item["reason"],
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
