"""Declarative ``StrategyPlugin`` for ordinary futures-pair Z-score mean reversion."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from moex_spread_lab.accounting.costs import tariff_profile
from moex_spread_lab.core.contracts import (
    CompatibilityVerdict,
    EntryBandPoint,
    InstrumentContext,
    ParameterSpec,
    StrategyIntentRun,
    WindowSnapshot,
)
from moex_spread_lab.core.settings import MIN_DAILY_VOLUME, ResearchSettings
from moex_spread_lab.core.universe import PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error

from ..base import PRICE_FILLS, validate_parameter_map
from .engine import compute_pair_entry_bands, run_mean_reversion_baseline

_SCHEMA = (
    ParameterSpec("lookback", "Окно · набл.", "int", 20, 5, 80, 1),
    ParameterSpec("entry_z", "Вход · z", "float", 2.0, 0.5, 5.0, 0.1),
    ParameterSpec("exit_z", "Выход · z", "float", 0.5, 0.0, 4.0, 0.1),
)


class PairMeanReversionPlugin:
    strategy_id = "mean_reversion"
    family_id = "mean_reversion"
    label_ru = "Возврат спреда к среднему"
    short_label_ru = "возврат к среднему"
    supported_universes = frozenset({PAIR_UNIVERSE})
    supported_fills = ("next_open", "next_close", "reference_same_bar")
    requires_daily_bars = False

    # Declarative adapter metadata (consumed via moex_spread_lab.core.contracts helpers).
    proof_adapter: Literal["pair_price_intents.v1"] = "pair_price_intents.v1"
    family_parameter_aliases: Mapping[str, str] = {"window": "lookback"}
    ui_field_order: tuple[str, ...] = ("entry_z", "exit_z", "lookback")
    trust_tier: Literal["builtin", "plugin"] = "builtin"

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
        positive = [
            row
            for row in window.rows
            if (row.get("near_volume") or 0) > 0 and (row.get("far_volume") or 0) > 0
        ]
        if len(positive) < 2:
            return CompatibilityVerdict(False, "fewer than two positive-volume common observations")
        if settings is not None and not settings.allow_failed_volume:
            min_vol = min(min(float(r["near_volume"]), float(r["far_volume"])) for r in positive)
            if min_vol < MIN_DAILY_VOLUME:
                return CompatibilityVerdict(False, "Не пройдено правило ликвидности на обучении.")
        return CompatibilityVerdict(True)

    def evaluate_intents(
        self, window: WindowSnapshot, params: Mapping[str, Any], fill: str
    ) -> StrategyIntentRun:
        if fill not in PRICE_FILLS:
            raise W0Error("Выбранный режим исполнения не поддерживается этой стратегией.")
        validated = self.validate_parameters(
            params,
            InstrumentContext(
                universe=window.universe, pair=window.pair, timeframe=window.timeframe
            ),
        )
        verdict = self.check_compatibility(window)
        if not verdict.compatible:
            raise W0Error(verdict.reason or "incompatible window")
        positive_rows = [
            row
            for row in window.rows
            if (row.get("near_volume") or 0) > 0 and (row.get("far_volume") or 0) > 0
        ]
        delay, fill_quote = PRICE_FILLS[fill]
        cost_profile = (
            dict(window.cost_profile)
            if window.cost_profile is not None
            else tariff_profile(float(window.slippage_ticks))
        )
        series = {
            "near": window.near,
            "far": window.far,
            "timeframe": window.timeframe,
            "contracts": dict(window.contracts),
            "rows": positive_rows,
            "provenance": dict(window.extra.get("provenance", {})),
        }
        report = run_mean_reversion_baseline(
            series,
            initial_capital=window.initial_capital_rub,
            lookback=int(validated["lookback"]),
            entry_z=float(validated["entry_z"]),
            exit_z=float(validated["exit_z"]),
            quantity=1,
            decision_delay_bars=delay,
            analysis_role="diagnostic_only",
            cost_profile=cost_profile,
            fill=fill_quote,
            cash_in_lqdt=True,
            funding_swaprates=dict(window.funding_swaprates) if window.funding_swaprates else None,
            sizing_intents=True,
            metrics_only=True,
        )
        days = [row["date"] for row in window.rows]
        delay = 0 if fill == "reference_same_bar" else 1

        def signal_day(entry: str) -> str:
            key = entry[:10] if window.timeframe == "1d" else entry[:19].replace(" ", "T")
            return days[max(0, days.index(key) - delay)]

        intents = tuple(
            {
                "side": "long_spread"
                if row["direction"] == "long_near_short_far"
                else "short_spread",
                "entry_date": row["entry_date"],
                "exit_date": row["exit_date"],
                "entry_near_fill": row["entry_near_fill"],
                "entry_far_fill": row["entry_far_fill"],
                "exit_near_fill": row["exit_near_fill"],
                "exit_far_fill": row["exit_far_fill"],
                "entry_signal_z": row["entry_signal_z"],
                "signal": {
                    "kind": "z",
                    "date": signal_day(row["entry_date"]),
                    "value": row["entry_signal_z"],
                },
                "reason": row["exit_reason"],
            }
            for row in report["trades"]
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
        relevant = {k: params[k] for k in ("lookback", "entry_z", "exit_z") if k in params}
        validated = self.validate_parameters(relevant)
        return compute_pair_entry_bands(
            series, validated, fill, far_point_value_rub=far_point_value_rub
        )


PLUGIN = PairMeanReversionPlugin()
