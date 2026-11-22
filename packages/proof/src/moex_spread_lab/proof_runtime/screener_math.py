"""Screener search numeric validation."""

import math
from datetime import date
from typing import Any

from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerError


def _require_finite(value: float | None) -> float:
    if value is None or not math.isfinite(value):
        raise ScreenerError("non-annualizable training window")
    return value


def _annualized(values: list[dict[str, Any]], start: str, end: str) -> float | None:
    rows = {row["date"]: row for row in values}
    if start not in rows or end not in rows:
        return None
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    first, last = rows[start], rows[end]
    if days < 30 or min(first["V"], first["B"], last["V"], last["B"]) <= 0:
        return None
    strategy = (last["V"] / first["V"]) ** (365 / days) - 1
    benchmark = (last["B"] / first["B"]) ** (365 / days) - 1
    return round((strategy - benchmark) * 100, 4)
