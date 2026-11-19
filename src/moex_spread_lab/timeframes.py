"""Registry for analysis bar intervals exposed as global research preferences."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TimeframeSpec:
    """Stable setting ID plus the data and engine capabilities for one bar interval."""

    id: str
    label: str
    iss_interval: int
    file_suffix: str
    intraday: bool
    supports_pair_proof: bool
    supports_calendar: bool
    supports_funding_strategy: bool


# Adding a timeframe requires a registry entry and data for that exact ISS interval.
# Intraday price proofs share the timestamp/session handling; unsupported adapters are
# deliberately gated instead of silently falling back to daily prices.
TIMEFRAMES: tuple[TimeframeSpec, ...] = (
    TimeframeSpec("1d", "1 день", 24, "1d", False, True, True, True),
    TimeframeSpec("1h", "1 час", 60, "1h", True, True, False, False),
)
DEFAULT_TIMEFRAME = "1d"
_BY_ID = {spec.id: spec for spec in TIMEFRAMES}
_BY_INTERVAL = {spec.iss_interval: spec for spec in TIMEFRAMES}


def get_timeframe(value: object) -> TimeframeSpec:
    if not isinstance(value, str) or value not in _BY_ID:
        raise ValueError("Неизвестный таймфрейм расчёта.")
    return _BY_ID[value]


def timeframe_for_interval(value: object) -> TimeframeSpec:
    if type(value) is not int or value not in _BY_INTERVAL:
        raise ValueError(f"Неподдерживаемый интервал свечи ISS: {value}.")
    return _BY_INTERVAL[value]


def timeframe_options() -> list[dict[str, str | bool]]:
    return [
        {
            "id": spec.id,
            "label": spec.label,
            "intraday": spec.intraday,
            "supports_pair_proof": spec.supports_pair_proof,
            "supports_calendar": spec.supports_calendar,
            "supports_funding_strategy": spec.supports_funding_strategy,
        }
        for spec in TIMEFRAMES
    ]
