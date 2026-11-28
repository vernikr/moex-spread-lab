"""Declarative ``StrategyPlugin`` for H3-F1 perpetual SWAPRATE funding carry."""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from moex_spread_lab.core.contracts import (
    CompatibilityVerdict,
    EntryBandPoint,
    InstrumentContext,
    ParameterSpec,
    StrategyIntentRun,
    WindowSnapshot,
)
from moex_spread_lab.core.settings import MIN_DAILY_VOLUME, ResearchSettings
from moex_spread_lab.core.timeframes import get_timeframe
from moex_spread_lab.core.universe import PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error

from ..base import validate_parameter_map
from .engine import build_carry_strategy

_SCHEMA = (ParameterSpec("lookback", "Окно · набл.", "int", 20, 5, 80, 1),)


class FundingCarryPlugin:
    strategy_id: str = "funding_only"
    family_id: str = "funding_only"
    label_ru: str = "Фондирование перпетуального свопа (carry)"
    short_label_ru: str = "фондирование"
    supported_universes: frozenset[str] = frozenset({PAIR_UNIVERSE})
    supported_fills: tuple[str, ...] = ("same_day_settlement",)
    requires_daily_bars: bool = True

    # Declarative adapter metadata (consumed via moex_spread_lab.core.contracts helpers).
    proof_adapter: Literal["carry_settlement_intents.v1"] = "carry_settlement_intents.v1"
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
        tf = get_timeframe(window.timeframe)
        if not tf.supports_funding_strategy:
            return CompatibilityVerdict(
                False, f"{tf.label}: carry требует дневных расчётных ставок."
            )
        study = window.carry_study
        if not study or not isinstance(study.get("rows"), list) or len(study["rows"]) < 2:
            return CompatibilityVerdict(False, "carry study rows are unavailable")
        if (
            settings is not None
            and not settings.allow_failed_volume
            and any(_row_min_volume(r) < MIN_DAILY_VOLUME for r in study["rows"])
        ):
            return CompatibilityVerdict(False, "Не пройдено правило ликвидности на обучении.")
        return CompatibilityVerdict(True)

    def evaluate_intents(
        self, window: WindowSnapshot, params: Mapping[str, Any], fill: str
    ) -> StrategyIntentRun:
        if fill not in self.supported_fills:
            raise W0Error("Выбранный режим исполнения не поддерживается этой стратегией.")
        validated = self.validate_parameters(
            params,
            InstrumentContext(
                universe=window.universe, pair=window.pair, timeframe=window.timeframe
            ),
        )
        verdict = self.check_compatibility(window)
        if not verdict.compatible:
            raise W0Error(verdict.reason or "incompatible carry window")
        contracts_path = window.extra.get("contracts_path")
        benchmark_path = window.benchmark
        if not isinstance(contracts_path, Path) or not isinstance(benchmark_path, Path):
            raise W0Error("carry strategy evaluation requires contracts_path and benchmark Path")
        study = window.carry_study
        assert study is not None
        if len(study["rows"]) <= int(validated["lookback"]):
            return StrategyIntentRun(
                strategy_id=self.strategy_id,
                fill=fill,
                parameters=validated,
                intents=(),
            )
        cost_scenario = str(window.extra.get("transaction_cost_scenario", "tariff"))
        report = build_carry_strategy(
            dict(window.carry_study),  # type: ignore[arg-type]
            benchmark_path,
            contracts_path,
            transaction_cost_scenario=cost_scenario,
            warmup_observations=int(validated["lookback"]),
            strategy_variant="funding_only",
            initial_capital=window.initial_capital_rub,
            quantity=1,
            execution_delay_rows=0,
            cash_in_lqdt=True,
            slippage_ticks=float(window.slippage_ticks),
            metrics_only=True,
        )
        last_date = report["daily"][-1]["date"]
        reasons = {
            (sig["date"], sig["action"]): sig.get("reason")
            for sig in report["signals"]
            if sig.get("execution_available")
        }
        intents = tuple(
            {
                "side": "short_spread",
                "entry_date": row["entry_execution_date"],
                "exit_date": row["exit_execution_date"] or last_date,
                "signal_date": row["entry_decision_date"],
                "reason": reasons.get((row.get("exit_decision_date"), "exit")) or row["status"],
                "entry_reason": reasons.get((row["entry_decision_date"], "entry")),
                "open_at_end": row["status"] == "open_at_data_end_no_executable_exit",
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
        """Funding entries come from the rate history, so only dates are bandable."""
        self.validate_parameters(params)
        return [EntryBandPoint(date=str(row.get("date", ""))) for row in series]


def _row_min_volume(row: Mapping[str, Any]) -> float:
    near, far = row.get("near_volume"), row.get("far_volume")
    if (
        isinstance(near, bool)
        or isinstance(far, bool)
        or not isinstance(near, (int, float))
        or not isinstance(far, (int, float))
    ):
        return 0.0
    nf, ff = float(near), float(far)
    if not (math.isfinite(nf) and math.isfinite(ff)):
        return 0.0
    return min(nf, ff)


PLUGIN = FundingCarryPlugin()
