"""Calendar and candle-file builders shared by the synthetic proof/strategy histories."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path


def weekdays(start: date, *, count: int | None = None, end: date | None = None) -> list[date]:
    """Mon-Fri dates from ``start``: the first ``count`` of them, or all up to ``end``."""
    if (count is None) == (end is None):
        raise ValueError("pass exactly one of count or end")
    out: list[date] = []
    day = start
    while (count is not None and len(out) < count) or (end is not None and day <= end):
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def iso_weekdays_until(start: date, end: date) -> list[str]:
    """ISO Mon-Fri dates in ``[start, end]``."""
    return [day.isoformat() for day in weekdays(start, end=end)]


def iso_weekdays(start: date, count: int) -> list[str]:
    """The first ``count`` ISO Mon-Fri dates from ``start``."""
    return [day.isoformat() for day in weekdays(start, count=count)]


def write_h1_candles(root: Path, subdir: str, secid: str, rows: list[dict]) -> Path:
    """Write an ``iss-candles.v1`` 1h file under ``root/data/w1/<subdir>``."""
    path = root / "data/w1" / subdir / f"{secid}_1h.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"schema_version": "iss-candles.v1", "secid": secid, "interval": 60, "rows": rows}
    path.write_text(json.dumps(document), encoding="utf-8")
    return path
