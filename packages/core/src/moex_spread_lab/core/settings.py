"""Persistent research preferences and per-window evaluation periods."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, cast

from .timeframes import DEFAULT_TIMEFRAME, get_timeframe
from .w0 import W0Error, load_object

DEFAULT_VALIDATION_PERCENT = 0
DEFAULT_INITIAL_CAPITAL_RUB = 30_000.0
INITIAL_CAPITAL = DEFAULT_INITIAL_CAPITAL_RUB
MIN_DAILY_VOLUME = 100
MAX_QUANTITY = 50
MAX_RESEARCH_CAPITAL_RUB = 1_000_000_000.0
DEFAULT_PRICE_FILL = "reference_same_bar"
VALID_PRICE_FILLS = ("next_open", "next_close", "reference_same_bar")
# 0 means "draw a fresh seed for every run"; any other value makes every random
# choice of the project reproducible.
DEFAULT_RANDOM_SEED = 0
MAX_RANDOM_SEED = 2_147_483_647


def validation_percent(value: object) -> float:
    if type(value) not in (int, float):
        raise W0Error("Доля проверки должна быть числом от 0 до 50%.")
    numeric = cast(int | float, value)
    if not 0 <= numeric <= 50 or not math.isfinite(numeric):
        raise W0Error("Доля проверки должна быть числом от 0 до 50%.")
    return int(numeric) if int(numeric) == numeric else numeric


def random_seed(value: object) -> int:
    """A non-negative integer seed; 0 asks for a fresh random seed per run."""
    if type(value) is not int or not 0 <= value <= MAX_RANDOM_SEED:
        raise W0Error(
            f"Сид должен быть целым числом от 0 до {MAX_RANDOM_SEED}; 0 — новый сид при каждом запуске."
        )
    return value


@dataclass(frozen=True)
class ResearchSettings:
    """One immutable snapshot for all search members, probes and result-period replays."""

    validation_percent: float = DEFAULT_VALIDATION_PERCENT
    allow_failed_volume: bool = True
    allow_failed_capital: bool = True
    slippage_ticks: float = 1.0
    initial_capital_rub: float = DEFAULT_INITIAL_CAPITAL_RUB
    timeframe: str = DEFAULT_TIMEFRAME
    price_fill: str = DEFAULT_PRICE_FILL
    random_seed: int = DEFAULT_RANDOM_SEED

    def metadata(self) -> dict[str, float | bool | str]:
        return asdict(self)

    @classmethod
    def load(cls, root: Path) -> ResearchSettings:
        return cls(**cast(dict[str, Any], read_settings(root)))


# Direct, non-tuning proof consumers do not implicitly opt into the research exceptions.
STRICT_SCREENS = ResearchSettings(allow_failed_volume=False, allow_failed_capital=False)


def _validated_initial_capital(value: object) -> float:
    if type(value) not in (int, float):
        raise W0Error(
            "Капитал должен быть числом от 0,01 до 1 000 000 000 ₽, не более двух знаков после запятой."
        )
    numeric = cast(int | float, value)
    if (
        not 0.01 <= numeric <= MAX_RESEARCH_CAPITAL_RUB
        or not math.isfinite(numeric)
        or round(numeric, 2) != numeric
    ):
        raise W0Error(
            "Капитал должен быть числом от 0,01 до 1 000 000 000 ₽, не более двух знаков после запятой."
        )
    return float(numeric)


def _validated_slippage(value: object) -> float:
    if type(value) not in (int, float):
        raise W0Error("Проскальзывание должно быть числом от 0 до 10 тиков.")
    numeric = cast(int | float, value)
    if not 0 <= numeric <= 10 or not math.isfinite(numeric):
        raise W0Error("Проскальзывание должно быть числом от 0 до 10 тиков.")
    return float(numeric)


def _validated_timeframe(value: object) -> str:
    try:
        return get_timeframe(value).id
    except ValueError as exc:
        raise W0Error(str(exc)) from exc


def _validated_price_fill(value: object) -> str:
    if not isinstance(value, str) or value not in VALID_PRICE_FILLS:
        raise W0Error(
            "Режим исполнения должен быть одним из: next_open, next_close, reference_same_bar."
        )
    return value


def _validated_bool(key: str, value: object) -> bool:
    if type(value) is not bool:
        raise W0Error(f"{key}: укажите true или false.")
    return value


_VALIDATORS: dict[str, Callable[[object], object]] = {
    "validation_percent": validation_percent,
    "initial_capital_rub": _validated_initial_capital,
    "timeframe": _validated_timeframe,
    "price_fill": _validated_price_fill,
    "slippage_ticks": _validated_slippage,
    "allow_failed_volume": lambda value: _validated_bool("allow_failed_volume", value),
    "allow_failed_capital": lambda value: _validated_bool("allow_failed_capital", value),
    "random_seed": random_seed,
}


def _validated_patch(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise W0Error("Неизвестные или пустые настройки расчёта.")
    mapping = cast(dict[str, object], value)
    if not mapping or mapping.keys() - _VALIDATORS.keys():
        raise W0Error("Неизвестные или пустые настройки расчёта.")
    return {key: _VALIDATORS[key](item) for key, item in mapping.items()}


def settings_with_override(
    root: Path, override: dict[str, object] | None = None
) -> ResearchSettings:
    """Build a validated in-memory snapshot without persisting research preferences."""
    settings = read_settings(root)
    if override:
        settings = cast(dict[str, float | bool | str], settings | _validated_patch(override))
    return ResearchSettings(**cast(dict[str, Any], settings))


def settings_path(root: Path) -> Path:
    return root / "data/analytics/research-settings.json"


def read_settings(root: Path) -> dict[str, float | bool | str]:
    try:
        stored = _validated_patch(
            load_object(settings_path(root), label="research settings", error=W0Error)
        )
        if "validation_percent" not in stored:
            raise W0Error("В настройках отсутствует доля проверки.")
        # Older files omit newer preferences. GET supplies defaults without rewriting them.
        return ResearchSettings(**cast(dict[str, Any], stored)).metadata()
    except FileNotFoundError:
        return ResearchSettings().metadata()
    except (ValueError, UnicodeError) as exc:
        raise W0Error("Не удалось прочитать настройки расчёта. Сохраните их заново.") from exc


def save_settings(root: Path, body: object) -> dict[str, float | bool | str]:
    patch = _validated_patch(body)
    # A full valid submission can also repair corrupt settings. Legacy split-only
    # clients and partial updates must preserve every other saved preference.
    settings = cast(
        dict[str, object],
        patch
        if patch.keys() == ResearchSettings().metadata().keys()
        else read_settings(root) | patch,
    )
    path = settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A sibling temporary file keeps replacement atomic for concurrent readers.
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(json.dumps(settings) + "\n")
            handle.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return cast(dict[str, float | bool | str], settings)


@dataclass(frozen=True)
class EvaluationSplit:
    percent: float
    training: tuple[str, ...]
    validation: tuple[str, ...]

    @classmethod
    def from_dates(cls, dates: Iterable[str], percent: float) -> EvaluationSplit:
        percent = validation_percent(percent)
        dates = tuple(dates)
        if not dates or (percent and len(dates) < 2):
            raise W0Error("Недостаточно наблюдений для выбранного разбиения.")
        if any(dates[index] >= dates[index + 1] for index in range(len(dates) - 1)):
            raise W0Error("Даты должны быть строго возрастающими и уникальными.")
        # Round down to complete observations, but never silently disable a requested check.
        count = max(1, int(len(dates) * Decimal(str(percent)) / 100)) if percent else 0
        cut = len(dates) - count
        return cls(percent, dates[:cut], dates[cut:])

    @property
    def basis(self) -> str:
        return "validation" if self.validation else "training"

    def metadata(self) -> dict[str, object]:
        total = len(self.training) + len(self.validation)
        effective = (len(self.validation) / total * 100) if total else 0.0
        return {
            "validation_percent": self.percent,
            "effective_validation_percent": effective,
            "result_basis": self.basis,
            "periods": {
                "training": [self.training[0], self.training[-1]],
                "validation": [self.validation[0], self.validation[-1]]
                if self.validation
                else None,
            },
            "observations": {
                "training": len(self.training),
                "validation": len(self.validation),
            },
        }
