"""Typed contracts and plugin protocols shared across MOEX Spread Lab workspace packages."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from .settings import ResearchSettings


def _empty_mapping() -> dict[str, Any]:
    return {}


@dataclass(frozen=True)
class ParameterSpec:
    """Declarative numeric parameter specification for a registered strategy plugin."""

    name: str
    label_ru: str
    kind: Literal["int", "float"]
    default: int | float
    min_value: int | float
    max_value: int | float
    step: int | float
    tunable: bool = True
    lamp_steps: tuple[int, ...] = (-2, -1, 1, 2)

    def contains(self, value: Any) -> bool:
        if type(value) not in (int, float) or not math.isfinite(value):
            return False
        if self.kind == "int" and type(value) is not int:
            return False
        return self.min_value <= value <= self.max_value

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label_ru": self.label_ru,
            "kind": self.kind,
            "default": self.default,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "step": self.step,
            "tunable": self.tunable,
            "lamp_steps": list(self.lamp_steps),
        }


@dataclass(frozen=True)
class InstrumentContext:
    """Instrument and timeframe metadata supplied when querying strategy schemas or defaults."""

    universe: str
    pair: str = ""
    asset: str | None = None
    timeframe: str = "1d"
    monthly: bool = False
    convergence: bool = False


@dataclass(frozen=True)
class CompatibilityVerdict:
    """Result of checking whether a strategy can evaluate a given window snapshot."""

    compatible: bool
    reason: str | None = None


@dataclass(frozen=True)
class EntryBandPoint:
    """Causal signal-bar entry thresholds in raw spread price units and % of far leg."""

    date: str
    buy_level: float | None = None
    sell_level: float | None = None
    buy_level_rel: float | None = None
    sell_level_rel: float | None = None


@dataclass(frozen=True)
class WindowSnapshot:
    """Normalized offline market window passed into ``StrategyPlugin`` evaluation."""

    universe: str
    pair: str
    timeframe: str = "1d"
    asset: str | None = None
    rows: tuple[dict[str, Any], ...] | list[dict[str, Any]] = ()
    contracts: Mapping[str, Any] = field(default_factory=_empty_mapping)
    near: str = ""
    far: str = ""
    funding_swaprates: Mapping[str, float] | None = None
    carry_study: Mapping[str, Any] | None = None
    benchmark: Any = None
    cost_profile: Mapping[str, Any] | None = None
    initial_capital_rub: float = 30_000.0
    slippage_ticks: float = 1.0
    native_spread_order: bool = True
    far_point_value_rub: float = 1.0
    extra: Mapping[str, Any] = field(default_factory=_empty_mapping)

    def truncated(self, count: int) -> WindowSnapshot:
        """Return a causal prefix copy containing only the first ``count`` rows."""
        prefix_rows = tuple(self.rows[:count])
        carry_study = self.carry_study
        if carry_study is not None and isinstance(carry_study.get("rows"), list):
            carry_study = {**carry_study, "rows": list(carry_study["rows"][:count])}
        return WindowSnapshot(
            universe=self.universe,
            pair=self.pair,
            timeframe=self.timeframe,
            asset=self.asset,
            rows=prefix_rows,
            contracts=self.contracts,
            near=self.near,
            far=self.far,
            funding_swaprates=self.funding_swaprates,
            carry_study=carry_study,
            benchmark=self.benchmark,
            cost_profile=self.cost_profile,
            initial_capital_rub=self.initial_capital_rub,
            slippage_ticks=self.slippage_ticks,
            native_spread_order=self.native_spread_order,
            far_point_value_rub=self.far_point_value_rub,
            extra=self.extra,
        )


@dataclass(frozen=True)
class StrategyIntentRun:
    """Unit-quantity trade intents emitted by a strategy before or alongside funded sizing."""

    strategy_id: str
    fill: str
    parameters: dict[str, Any]
    intents: tuple[dict[str, Any], ...]
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class StrategyPlugin(Protocol):
    """Protocol implemented by every isolated strategy subfolder in ``msl-strategies``."""

    strategy_id: str
    family_id: str
    label_ru: str
    short_label_ru: str
    supported_universes: frozenset[str]
    supported_fills: tuple[str, ...]
    requires_daily_bars: bool

    def parameter_schema(
        self, context: InstrumentContext | None = None
    ) -> tuple[ParameterSpec, ...]: ...
    def validate_parameters(
        self, raw: Mapping[str, Any], context: InstrumentContext | None = None
    ) -> dict[str, Any]: ...
    def check_compatibility(
        self, window: WindowSnapshot, settings: ResearchSettings | None = None
    ) -> CompatibilityVerdict: ...
    def evaluate_intents(
        self, window: WindowSnapshot, params: Mapping[str, Any], fill: str
    ) -> StrategyIntentRun: ...
    def compute_entry_bands(
        self,
        series: list[dict[str, Any]],
        params: Mapping[str, Any],
        fill: str,
        *,
        far_point_value_rub: float = 1.0,
    ) -> list[EntryBandPoint]: ...
