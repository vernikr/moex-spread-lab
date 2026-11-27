from __future__ import annotations

import math
import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from moex_spread_lab.core._fastcore_loader import load_fastcore
from moex_spread_lab.core.contracts import EntryBandPoint
from moex_spread_lab.core.derived_cache import record_cache_lookup

from ..base import (
    CALENDAR_EXIT_REASONS,
    calendar_row_buffers,
    elapsed_calendar_days,
    make_calendar_trade,
)

_ZSCORE_EXTRA_OPTS = frozenset(
    {
        "entry_z",
        "exit_z",
        "window",
        "max_hold_days",
        "close_before",
        "skip_dividend_windows",
        "close_at_end",
        "z_cache",
        "z_cache_key",
    }
)


@dataclass(frozen=True, slots=True)
class _ZscoreParams:
    delay: int
    fill: str
    entry_z: float
    exit_z: float
    window: int
    max_hold_days: int
    close_before: int
    skip_dividend: bool
    close_at_end: bool
    z_cache: dict[Any, Any] | None
    z_cache_key: Any


def _parse_obs_dt(raw: Any) -> date | datetime:
    a = str(raw)
    if "T" in a:
        return datetime.fromisoformat(a.replace("Z", ""))
    return date.fromisoformat(a[:10])


def _get_cached_obs_dt(
    rows: list[dict[str, Any]], parsed_dts: list[date | datetime | None], index: int
) -> date | datetime:
    a = parsed_dts[index]
    if a is None:
        a = _parse_obs_dt(rows[index]["date"])
        parsed_dts[index] = a
    return a


def _held_days(
    entry_row: Mapping[str, Any],
    row: Mapping[str, Any],
    entry_dt: date | datetime,
    row_dt: date | datetime,
) -> float:
    if isinstance(entry_dt, datetime) and isinstance(row_dt, datetime):
        return (row_dt - entry_dt).total_seconds() / 86400
    if type(entry_dt) is date and type(row_dt) is date:
        return float((row_dt - entry_dt).days)
    return elapsed_calendar_days(str(entry_row["date"]), str(row["date"]))


def _compute_sample_z_py(
    rows: list[dict[str, Any]], window: int, include_current: bool
) -> list[float]:
    b = len(rows)
    z = [math.nan] * b
    d = [float(a["spread"]) for a in rows]
    e = [bool(a.get("signal_eligible", True)) for a in rows]
    a = [0] * (b + 1)
    for f, k in enumerate(e):
        a[f + 1] = a[f] + k
    c = 1 if include_current else 0
    for index in range(window - c, b):
        if not e[index]:
            continue
        g = index - window + c
        h = index + c
        if a[h] - a[g] != window:
            continue
        i = d[g:h]
        j = statistics.stdev(i)
        z[index] = (d[index] - statistics.mean(i)) / j if j > 0 else math.nan
    return z


def _exit_reason_py(
    row: Mapping[str, Any], z_val: float, held: float, cfg: _ZscoreParams
) -> str | None:
    if abs(z_val) <= cfg.exit_z:
        return "z_reverted"
    if held >= cfg.max_hold_days:
        return "max_hold"
    if int(row["days_to_near_expiry"]) <= cfg.close_before:
        return "near_expiry_buffer"
    return None


def _can_enter_zscore(
    rows: list[dict[str, Any]], z: list[float], index: int, cfg: _ZscoreParams
) -> int | None:
    a = index - cfg.delay
    if a < 0:
        return None
    z_val = z[a]
    if (
        rows[a].get("signal_eligible", True)
        and rows[index].get("entry_eligible", True)
        and not math.isnan(z_val)
        and int(rows[index]["days_to_near_expiry"]) > cfg.close_before
        and abs(z_val) >= cfg.entry_z
    ):
        return a
    return None


@dataclass
class _ZscorePyState:
    rows: list[dict[str, Any]]
    z: list[float]
    cfg: _ZscoreParams
    parsed_dts: list[date | datetime | None]
    position: int = 0
    entry: dict[str, Any] | None = None
    entry_dt: date | datetime | None = None
    signal_index: int = 0
    trades: list[dict[str, Any]] = field(default_factory=list)


def _try_exit_zscore_py(st: _ZscorePyState, index: int, row: dict[str, Any]) -> None:
    if not st.position or st.entry is None or st.entry_dt is None:
        return
    if not row.get("exit_eligible", True):
        return
    b = st.z[index - st.cfg.delay] if index >= st.cfg.delay else math.nan
    held = _held_days(st.entry, row, st.entry_dt, _get_cached_obs_dt(st.rows, st.parsed_dts, index))
    a = _exit_reason_py(row, b, held, st.cfg)
    if a:
        st.trades.append(
            make_calendar_trade(
                st.entry,
                row,
                st.position,
                st.cfg.fill,
                a,
                ("z", st.rows[st.signal_index], st.z[st.signal_index]),
            )
        )
        st.position, st.entry, st.entry_dt = 0, None, None


def _try_enter_zscore_py(st: _ZscorePyState, index: int, row: dict[str, Any]) -> None:
    if st.position:
        return
    a = _can_enter_zscore(st.rows, st.z, index, st.cfg)
    if a is None:
        return
    st.position = -1 if st.z[a] > 0 else 1
    st.entry = row
    st.signal_index = a
    st.entry_dt = _get_cached_obs_dt(st.rows, st.parsed_dts, index)


def _zscore_scan_py(
    rows: list[dict[str, Any]], z: list[float], cfg: _ZscoreParams
) -> list[dict[str, Any]]:
    st = _ZscorePyState(rows=rows, z=z, cfg=cfg, parsed_dts=[None] * len(rows))
    for index, row in enumerate(rows):
        _try_exit_zscore_py(st, index, row)
        _try_enter_zscore_py(st, index, row)
    if st.position and st.entry is not None:
        a = "z", rows[st.signal_index], z[st.signal_index]
        if cfg.close_at_end and st.entry is not rows[-1]:
            st.trades.append(
                make_calendar_trade(st.entry, rows[-1], st.position, cfg.fill, "end_of_window", a)
            )
        else:
            st.trades.append(
                {"entry": st.entry, "exit": rows[-1], "direction": st.position, "signal": a}
            )
    return st.trades


def _cached_sample_z(
    fc: Any,
    rows: list[dict[str, Any]],
    cfg: _ZscoreParams,
    include_current: bool,
    fastcore_args: tuple[Any, ...] = (),
) -> list[float]:
    a = cfg.z_cache
    b = (cfg.z_cache_key, cfg.window, include_current) if a is not None else None
    z: list[float] | None = a.get(b) if a is not None and b is not None else None
    if b is not None:
        record_cache_lookup("zscore", z is not None)
    if z is None:
        z = (
            _compute_sample_z_py(rows, cfg.window, include_current)
            if fc is None
            else fc.calendar_sample_z(*fastcore_args, cfg.window, include_current)
        )
        if a is not None and b is not None:
            a[b] = z
    assert z is not None
    return z


def _zscore_fastcore(
    fc: Any, rows: list[dict[str, Any]], cfg: _ZscoreParams
) -> list[dict[str, Any]]:
    include_current = cfg.delay == 0
    b, _h, c, d, a, e, f, g = calendar_row_buffers(
        rows, z_cache=cfg.z_cache, z_cache_key=cfg.z_cache_key
    )
    z = _cached_sample_z(fc, rows, cfg, include_current, (b, a, g))
    specs: list[tuple[int, int, int, int, int, float]] = fc.calendar_zscore_scan(
        z,
        c,
        d,
        a,
        e,
        f,
        cfg.delay,
        cfg.entry_z,
        cfg.exit_z,
        float(cfg.max_hold_days),
        cfg.close_before,
        cfg.close_at_end,
    )
    return [
        make_calendar_trade(
            rows[a], rows[b], c, cfg.fill, CALENDAR_EXIT_REASONS[d], ("z", rows[e], f)
        )
        for (a, b, c, d, e, f) in specs
    ]


def zscore_trades(
    rows: list[dict[str, Any]], *, delay: int, fill: str, **options: Any
) -> list[dict[str, Any]]:
    a = set(options) - _ZSCORE_EXTRA_OPTS
    if a:
        raise TypeError(f"unexpected keyword argument(s): {sorted(a)}")
    cfg = _ZscoreParams(
        delay=delay,
        fill=fill,
        entry_z=float(options.get("entry_z", 1.5)),
        exit_z=float(options.get("exit_z", 0.5)),
        window=int(options.get("window", 20)),
        max_hold_days=int(options.get("max_hold_days", 30)),
        close_before=int(options.get("close_before", 7)),
        skip_dividend=bool(options.get("skip_dividend_windows", True)),
        close_at_end=bool(options.get("close_at_end", False)),
        z_cache=options.get("z_cache"),
        z_cache_key=options.get("z_cache_key"),
    )
    if not rows or (cfg.skip_dividend and rows[0]["dividend_in_window"]):
        return []
    fc = load_fastcore()
    if fc is not None and cfg.window >= 2 and cfg.delay >= 0:
        return _zscore_fastcore(fc, rows, cfg)
    z = _cached_sample_z(None, rows, cfg, cfg.delay == 0)
    return _zscore_scan_py(rows, z, cfg)


def compute_calendar_zscore_entry_bands(
    series: list[dict[str, Any]],
    params: Mapping[str, Any],
    fill: str,
    *,
    far_point_value_rub: float = 1.0,
) -> list[EntryBandPoint]:
    del far_point_value_rub
    window = int(params["window"])
    entry_z = float(params["entry_z"])
    n = fill == "reference_same_bar"
    history: list[float] = []
    points: list[EntryBandPoint] = []
    for row in series:
        f = str(row.get("date", ""))
        if not row.get("volume_eligible", True):
            points.append(EntryBandPoint(date=f))
            continue
        a = float(row.get("point_value_rub", 1.0))
        g = float(row["spread"]) * a
        b = [*history, g][-window:] if n else history[-window:]
        c = d = h = i = None
        if len(b) == window:
            j, k = statistics.mean(b), statistics.stdev(b)
            if k > 0:
                m = entry_z * k
                c = (j - m) / a
                d = (j + m) / a
                e = float(row["far"])
                if e:
                    h = c / e * 100
                    i = d / e * 100
        history.append(g)
        points.append(
            EntryBandPoint(date=f, buy_level=c, sell_level=d, buy_level_rel=h, sell_level_rel=i)
        )
    return points


__all__ = ["compute_calendar_zscore_entry_bands", "zscore_trades"]
