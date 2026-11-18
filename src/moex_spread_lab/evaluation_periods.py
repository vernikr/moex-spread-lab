"""One project-wide split policy for interactive strategy research (revised BR-10)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from .w0 import W0Error

DEFAULT_VALIDATION_PERCENT = 0


def validation_percent(value: object) -> float:
    if type(value) not in (int, float) or not 0 <= value <= 50:
        raise W0Error("Доля проверки должна быть числом от 0 до 50%.")
    return int(value) if int(value) == value else value


def _settings(value: object) -> dict[str, float]:
    if not isinstance(value, dict) or value.keys() != {"validation_percent"}:
        raise W0Error("Ожидается настройка validation_percent (0–50%).")
    return {"validation_percent": validation_percent(value["validation_percent"])}


def settings_path(root: Path) -> Path:
    return root / "data/analytics/research-settings.json"


def read_settings(root: Path) -> dict[str, float]:
    try:
        return _settings(json.loads(settings_path(root).read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {"validation_percent": DEFAULT_VALIDATION_PERCENT}
    except (ValueError, UnicodeError) as exc:
        raise W0Error(
            "Не удалось прочитать настройки расчёта. Сохраните их заново."
        ) from exc


def save_settings(root: Path, body: object) -> dict[str, float]:
    settings = _settings(body)
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
