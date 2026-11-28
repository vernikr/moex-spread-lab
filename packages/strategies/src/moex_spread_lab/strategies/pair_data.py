"""Per-session candle volumes and offline ISS history sources for pair datasets.

The funding evidence and the offline screener resolve the same two legs, so the
volume summation rule and the history-cache location live here once.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from moex_spread_lab.data import universe as _catalog
from moex_spread_lab.data.iss.catalog import fetch_history

HISTORY_WINDOWS: dict[str, tuple[str, str]] = _catalog.HISTORY_WINDOWS
PAIRS: tuple[dict[str, str], ...] = _catalog.PAIRS

VolumeMap = dict[str, float | None]


def _offline_only(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError("local screener does not download data; run the documented intake first")


def daily_volumes(rows: Iterable[Mapping[str, Any]]) -> VolumeMap:
    """Sum candle volume per session day; a non-numeric or negative row voids its day."""
    volumes: VolumeMap = {}
    invalid_days: set[str] = set()
    for row in rows:
        begin = row.get("begin")
        if not isinstance(begin, str) or len(begin) < 10:
            continue
        day = begin[:10]
        value = row.get("volume")
        if value is None or isinstance(value, bool):
            invalid_days.add(day)
            continue
        number = float(value)
        if not math.isfinite(number) or number < 0:
            invalid_days.add(day)
            continue
        volumes[day] = float(volumes.get(day) or 0.0) + number
    for day in invalid_days:
        volumes[day] = None
    return volumes


def positive_leg_days(near: VolumeMap, far: VolumeMap) -> set[str]:
    """Session days where both legs are present with positive volume."""
    return {
        day
        for day in set(near) & set(far)
        if (nv := near.get(day)) is not None
        and float(nv) > 0
        and (fv := far.get(day)) is not None
        and float(fv) > 0
    }


def _values(leg: VolumeMap, days: Iterable[str]) -> list[float]:
    return [float(v) for v in (leg[d] for d in days) if v is not None]


def leg_volumes(near: VolumeMap, far: VolumeMap) -> dict[str, float] | None:
    """Per-leg volume on the thinnest common session day, the binding liquidity limit."""
    days = positive_leg_days(near, far)
    if not days:
        return None
    return {"near": min(_values(near, days)), "far": min(_values(far, days))}


def history_cache_dir(root: Path, pair_id: str) -> Path:
    folder = "carry-history-cache" if pair_id == "CNYRUBF/CRZ6" else "carry-screen-history-cache"
    return root / "data" / folder


def history_source(
    root: Path,
    pair_id: str,
    secid: str,
    *,
    fetch_fn: Callable[..., dict[str, Any]] = fetch_history,
) -> dict[str, Any]:
    """Read one leg's ISS history pages from the pair's offline cache only."""
    start, end = HISTORY_WINDOWS[pair_id]
    return fetch_fn(
        secid,
        start,
        end,
        cache_dir=history_cache_dir(root, pair_id),
        delay_seconds=0,
        cache_only=True,
        opener=_offline_only,
    )


__all__ = [
    "HISTORY_WINDOWS",
    "PAIRS",
    "VolumeMap",
    "daily_volumes",
    "history_cache_dir",
    "history_source",
    "leg_volumes",
    "positive_leg_days",
]
