"""Persistent research preferences and per-window evaluation periods."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from .w0 import W0Error

DEFAULT_VALIDATION_PERCENT = 0
DEFAULT_INITIAL_CAPITAL_RUB = 30_000.0
MAX_RESEARCH_CAPITAL_RUB = 1_000_000_000.0


def validation_percent(value: object) -> float:
    if type(value) not in (int, float) or not 0 <= value <= 50:
        raise W0Error("Доля проверки должна быть числом от 0 до 50%.")
    return int(value) if int(value) == value else value


@dataclass(frozen=True)
class ResearchSettings:
    """One immutable snapshot for all search members, probes and result-period replays."""

    validation_percent: float = DEFAULT_VALIDATION_PERCENT
    allow_failed_volume: bool = True
    allow_failed_capital: bool = True
    slippage_ticks: float = 1.0
    initial_capital_rub: float = DEFAULT_INITIAL_CAPITAL_RUB

    def metadata(self) -> dict[str, float | bool]:
        return asdict(self)

    @classmethod
    def load(cls, root: Path) -> ResearchSettings:
        return cls(**read_settings(root))


# Direct, non-tuning proof consumers do not implicitly opt into the research exceptions.
STRICT_SCREENS = ResearchSettings(allow_failed_volume=False, allow_failed_capital=False)


def _validated_patch(value: object) -> dict:
    if (
        not isinstance(value, dict)
        or not value
        or value.keys() - ResearchSettings().metadata().keys()
    ):
        raise W0Error("Неизвестные или пустые настройки расчёта.")
    patch = dict(value)
    if "validation_percent" in patch:
        patch["validation_percent"] = validation_percent(patch["validation_percent"])
    if "initial_capital_rub" in patch:
        value = patch["initial_capital_rub"]
        if (
            type(value) not in (int, float)
            or not 0.01 <= value <= MAX_RESEARCH_CAPITAL_RUB
            or round(value, 2) != value
        ):
            raise W0Error(
                "Капитал должен быть числом от 0,01 до 1 000 000 000 ₽, не более двух знаков после запятой."
            )
        patch["initial_capital_rub"] = float(value)
    if "slippage_ticks" in patch:
        value = patch["slippage_ticks"]
        if type(value) not in (int, float) or not 0 <= value <= 10:
            raise W0Error("Проскальзывание должно быть числом от 0 до 10 тиков.")
        patch["slippage_ticks"] = float(value)
    for k in ("allow_failed_volume", "allow_failed_capital"):
        if k in patch and type(patch[k]) is not bool:
            raise W0Error(f"{k}: укажите true или false.")
    return patch


def settings_path(root: Path) -> Path:
    return root / "data/analytics/research-settings.json"


def read_settings(root: Path) -> dict[str, float | bool]:
    try:
        stored = _validated_patch(
            json.loads(settings_path(root).read_text(encoding="utf-8"))
        )
        if "validation_percent" not in stored:
            raise W0Error("В настройках отсутствует доля проверки.")
        # Older files omit newer preferences. GET supplies defaults without rewriting them.
        return ResearchSettings(**stored).metadata()
    except FileNotFoundError:
        return ResearchSettings().metadata()
    except (ValueError, UnicodeError) as exc:
        raise W0Error(
            "Не удалось прочитать настройки расчёта. Сохраните их заново."
        ) from exc


def save_settings(root: Path, body: object) -> dict[str, float | bool]:
    patch = _validated_patch(body)
    # A full valid submission can also repair corrupt settings. Legacy split-only
    # clients and partial updates must preserve every other saved preference.
    settings = (
        patch
        if patch.keys() == ResearchSettings().metadata().keys()
        else read_settings(root) | patch
    )
    path = settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Readers (including a second local process) see either complete version.
    with TemporaryDirectory(dir=path.parent) as staging:
        temporary = Path(staging) / path.name
        temporary.write_text(json.dumps(settings) + "\n", encoding="utf-8")
        temporary.replace(path)
    return settings


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

    def metadata(self) -> dict:
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
