"""Frozen request objects and shared notes for the mean-reversion pair proof."""

from __future__ import annotations

from array import array
from dataclasses import dataclass, replace
from typing import Any

CARRY_FILL_NOTE = (
    "Фандинг удерживаемой позиции = −position × SWAPRATE × лот × число контрактов. Источник ставки — "
    "итог клиринга MOEX ISS, кроме явно отмеченных оценок. Вход/выход по open — прокси времени "
    "исполнения; лот — из текущего снапшота. Это не пересчёт минутных D/K1/K2 и не брокерская выписка."
)
VOLUME_RULE_NOTE = (
    "Дни, когда объём любой ноги равен нулю или отсутствует, остаются в журнале, но исключены из "
    "индикаторов, гейта объёма и симуляции сделок; позиции на этих датах не переоцениваются."
)
OPEN_POSITION_NOTE = (
    "Открытая на конец выборки позиция отмечается как принудительный прокси-выход, а не как "
    "исполнимая сделка."
)
SPREAD_NOTES = (
    "Спред пары — близкая минус дальняя, нормированный в рублях, а не собственный инструмент; его "
    "объём не наблюдаем.",
    "Спред показан в % от стоимости дальней ноги; абсолютные отметки в рублях — в CSV и во второй "
    "строке таблицы.",
)


@dataclass(frozen=True, slots=True)
class MeanReversionSpec:
    """Fixed-quantity mean-reversion request bound to one prepared window."""

    inputs: Any
    pair: dict[str, Any]
    execution: dict[str, Any]
    params: dict[str, Any]
    terms: dict[str, dict[str, Any]]
    risk: dict[str, Any]
    prepared: tuple[Any, ...]
    through: str | None = None
    cache: dict[str, Any] | None = None
    benchmark_dates: list[str] | None = None
    benchmark_prices: dict[str, float] | None = None

    def with_execution(self, **overrides: Any) -> MeanReversionSpec:
        """Same window, different execution figures (used by the compatibility shim)."""
        return replace(self, execution={**self.execution, **overrides})


@dataclass(frozen=True, slots=True)
class MeanReversionWindow:
    """Cut-off price series, funding evidence and derived multipliers for one replay."""

    spec: MeanReversionSpec
    series: dict[str, Any]
    analysis_rows: list[dict[str, Any]]
    analysis_series: dict[str, Any]
    spread_ticks: array[float]
    evidence: Any
    marks: dict[str, Any]
    funding_swaprates: dict[str, Any]
    volume_map: dict[str, dict[str, Any]]
    funding_window: Any
    near_mult: float
    far_mult: float
    margin_pair: float
    z_scores: list[Any]
    signal_dates: dict[str, str]
