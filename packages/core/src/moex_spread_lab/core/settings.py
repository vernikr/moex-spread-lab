"""Persistent research preferences and per-window evaluation periods."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from .timeframes import DEFAULT_TIMEFRAME, get_timeframe
from .w0 import W0Error

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
    if not 0 <= numeric <= 50:
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
    if not 0.01 <= numeric <= MAX_RESEARCH_CAPITAL_RUB or round(numeric, 2) != numeric:
        raise W0Error(
            "Капитал должен быть числом от 0,01 до 1 000 000 000 ₽, не более двух знаков после запятой."
        )
    return float(numeric)


def _validated_slippage(value: object) -> float:
    if type(value) not in (int, float):
        raise W0Error("Проскальзывание должно быть числом от 0 до 10 тиков.")
    numeric = cast(int | float, value)
    if not 0 <= numeric <= 10:
        raise W0Error("Проскальзывание должно быть числом от 0 до 10 тиков.")
    return float(numeric)


def _validated_patch(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise W0Error("Неизвестные или пустые настройки расчёта.")
    mapping = cast(dict[str, object], value)
    if not mapping or mapping.keys() - ResearchSettings().metadata().keys():
        raise W0Error("Неизвестные или пустые настройки расчёта.")
    patch = dict(mapping)
    if "validation_percent" in patch:
        patch["validation_percent"] = validation_percent(patch["validation_percent"])
    if "initial_capital_rub" in patch:
        patch["initial_capital_rub"] = _validated_initial_capital(patch["initial_capital_rub"])
    if "timeframe" in patch:
        try:
            get_timeframe(cast(str, patch["timeframe"]))
        except ValueError as exc:
            raise W0Error(str(exc)) from exc
    if "price_fill" in patch:
        value = patch["price_fill"]
        if not isinstance(value, str) or value not in VALID_PRICE_FILLS:
            raise W0Error(
                "Режим исполнения должен быть одним из: next_open, next_close, reference_same_bar."
            )
    if "slippage_ticks" in patch:
        patch["slippage_ticks"] = _validated_slippage(patch["slippage_ticks"])
    for k in ("allow_failed_volume", "allow_failed_capital"):
        if k in patch and type(patch[k]) is not bool:
            raise W0Error(f"{k}: укажите true или false.")
    if "random_seed" in patch:
        patch["random_seed"] = random_seed(patch["random_seed"])
    return patch


def settings_path(root: Path) -> Path:
    return root / "data/analytics/research-settings.json"


def read_settings(root: Path) -> dict[str, float | bool | str]:
    try:
        stored = _validated_patch(json.loads(settings_path(root).read_text(encoding="utf-8")))
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
    # Readers (including a second local process) see either complete version.
    with TemporaryDirectory(dir=path.parent) as staging:
        temporary = Path(staging) / path.name
        temporary.write_text(json.dumps(settings) + "\n", encoding="utf-8")
        temporary.replace(path)
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
        # Round down to complete observations, but never silently disable a requested check.
        count = max(1, int(len(dates) * Decimal(str(percent)) / 100)) if percent else 0
        cut = len(dates) - count
        return cls(percent, dates[:cut], dates[cut:])

    @property
    def basis(self) -> str:
        return "validation" if self.validation else "training"

    def metadata(self) -> dict[str, object]:
        return {
            "validation_percent": self.percent,
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
