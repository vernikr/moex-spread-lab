"""Declarative ``StrategyPlugin`` for implied-rate convergence to RUONIA."""

from __future__ import annotations

import math
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

from ..base import PRICE_FILLS, validate_parameter_map
from .engine import compute_ruonia_entry_bands, convergence_trades

_SCHEMA = (ParameterSpec("threshold_pp", "Порог · п.п. от RUONIA", "float", 1.0, 0.1, 10.0, 0.1),)
_CONVERGENCE_ASSETS = frozenset({"SR", "GZ"})


class CalendarRuoniaPlugin:
    strategy_id = "convergence_to_ruonia"
    family_id = "convergence_to_ruonia"
    label_ru = "Сходимость к справедливой цене RUONIA"
    short_label_ru = "сходимость к RUONIA"
    supported_universes = frozenset({CALENDAR_UNIVERSE})
    supported_fills = ("next_open", "next_close", "reference_same_bar")
    requires_daily_bars = False

    def parameter_schema(
        self, context: InstrumentContext | None = None
    ) -> tuple[ParameterSpec, ...]:
        return _SCHEMA

    def validate_parameters(
        self, raw: Mapping[str, Any], context: InstrumentContext | None = None
    ) -> dict[str, Any]:
        return validate_parameter_map(raw, self.parameter_schema(context))

    def check_compatibility(
        self, window: WindowSnapshot, settings: ResearchSettings | None = None
    ) -> CompatibilityVerdict:
        if window.universe not in self.supported_universes:
            return CompatibilityVerdict(False, "Выбранная стратегия неприменима к этому типу пары.")
        convergence_flag = window.extra.get("convergence")
        if convergence_flag is False or (
            convergence_flag is None and window.asset not in _CONVERGENCE_ASSETS
        ):
            return CompatibilityVerdict(
                False, "Для этого актива не предусмотрена стратегия RUONIA."
            )
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
        if not any(math.isfinite(float(r.get("dev_pp", math.nan))) for r in eligible):
            return CompatibilityVerdict(False, "RUONIA deviation series is unavailable")
        return CompatibilityVerdict(True)

    def evaluate_intents(
        self, window: WindowSnapshot, params: Mapping[str, Any], fill: str
    ) -> StrategyIntentRun:
        if fill not in PRICE_FILLS:
            raise W0Error("Выбранный режим исполнения не поддерживается этой стратегией.")
        ctx = InstrumentContext(
            universe=window.universe,
            pair=window.pair,
            asset=window.asset,
            timeframe=window.timeframe,
        )
        validated = self.validate_parameters(params, ctx)
        verdict = self.check_compatibility(window)
        if not verdict.compatible:
            raise W0Error(verdict.reason or "incompatible RUONIA window")
        delay, fill_quote = PRICE_FILLS[fill]
        analysis_rows = (
            list(window.rows)
            if window.timeframe == "1h"
            else [r for r in window.rows if (r.get("min_volume") or 0) > 0]
        )
        trades = convergence_trades(
            analysis_rows,
            threshold=float(validated["threshold_pp"]),
            delay=delay,
            fill=fill_quote,
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
        relevant = {k: params[k] for k in ("threshold_pp",) if k in params}
        validated = self.validate_parameters(relevant)
        return compute_ruonia_entry_bands(
            series, validated, fill, far_point_value_rub=far_point_value_rub
        )


PLUGIN = CalendarRuoniaPlugin()
