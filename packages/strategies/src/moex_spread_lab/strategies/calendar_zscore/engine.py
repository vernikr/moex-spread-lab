"""Calendar spread rolling sample Z-score mean-reversion engine and causal entry bands."""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from moex_spread_lab.core._fastcore_loader import load_fastcore
from moex_spread_lab.core.cache_metrics import record_cache_lookup
from moex_spread_lab.core.contracts import EntryBandPoint

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
    s = str(raw)
    if "T" in s:
        return datetime.fromisoformat(s.replace("Z", ""))
    return date.fromisoformat(s[:10])


def _get_cached_obs_dt(
    rows: list[dict[str, Any]], parsed_dts: list[date | datetime | None], index: int
) -> date | datetime:
    cached = parsed_dts[index]
    if cached is None:
        cached = _parse_obs_dt(rows[index]["date"])
        parsed_dts[index] = cached
    return cached


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
    n = len(rows)
    z = [math.nan] * n
    spreads_py = [float(r["spread"]) for r in rows]
    elig = [bool(r.get("signal_eligible", True)) for r in rows]
    elig_pref_py = [0] * (n + 1)
    for idx, flag in enumerate(elig):
        elig_pref_py[idx + 1] = elig_pref_py[idx] + flag
    offset = 1 if include_current else 0
    for index in range(window - offset, n):
        if not elig[index]:
            continue
        start = index - window + offset
        end = index + offset
        if elig_pref_py[end] - elig_pref_py[start] != window:
            continue
        sample = spreads_py[start:end]
        deviation = statistics.stdev(sample)
        z[index] = (
            (spreads_py[index] - statistics.mean(sample)) / deviation if deviation > 0 else math.nan
        )
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
    sig_idx = index - cfg.delay
    if sig_idx < 0:
        return None
    z_val = z[sig_idx]
    if (
        rows[sig_idx].get("signal_eligible", True)
        and rows[index].get("entry_eligible", True)
        and not math.isnan(z_val)
        and int(rows[index]["days_to_near_expiry"]) > cfg.close_before
        and abs(z_val) >= cfg.entry_z
    ):
        return sig_idx
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
    trades: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.trades = []


def _try_exit_zscore_py(st: _ZscorePyState, index: int, row: dict[str, Any]) -> None:
    if not st.position or st.entry is None or st.entry_dt is None:
        return
    if not row.get("exit_eligible", True):
        return
    exit_sig = st.z[index - st.cfg.delay] if index >= st.cfg.delay else math.nan
    held = _held_days(st.entry, row, st.entry_dt, _get_cached_obs_dt(st.rows, st.parsed_dts, index))
    reason = _exit_reason_py(row, exit_sig, held, st.cfg)
    if reason:
        st.trades.append(
            make_calendar_trade(
                st.entry,
                row,
                st.position,
                st.cfg.fill,
                reason,
                ("z", st.rows[st.signal_index], st.z[st.signal_index]),
            )
        )
        st.position, st.entry, st.entry_dt = 0, None, None


def _try_enter_zscore_py(st: _ZscorePyState, index: int, row: dict[str, Any]) -> None:
    if st.position:
        return
    sig_idx = _can_enter_zscore(st.rows, st.z, index, st.cfg)
    if sig_idx is None:
        return
    st.position = -1 if st.z[sig_idx] > 0 else 1
    st.entry = row
    st.signal_index = sig_idx
    st.entry_dt = _get_cached_obs_dt(st.rows, st.parsed_dts, index)


def _zscore_scan_py(
    rows: list[dict[str, Any]], z: list[float], cfg: _ZscoreParams
) -> list[dict[str, Any]]:
    st = _ZscorePyState(rows=rows, z=z, cfg=cfg, parsed_dts=[None] * len(rows))
    for index, row in enumerate(rows):
        _try_exit_zscore_py(st, index, row)
        _try_enter_zscore_py(st, index, row)
    if st.position and cfg.close_at_end and st.entry is not None:
        st.trades.append(
            make_calendar_trade(
                st.entry,
                rows[-1],
                st.position,
                cfg.fill,
                "end_of_window",
                ("z", rows[st.signal_index], z[st.signal_index]),
            )
        )
    return st.trades


def _zscore_fastcore(
    fc: Any, rows: list[dict[str, Any]], cfg: _ZscoreParams
) -> list[dict[str, Any]]:
    include_current = cfg.delay == 0
    spreads, _, ts_days, dte, sig_el, ent_el, ext_el, elig_pref = calendar_row_buffers(
        rows, z_cache=cfg.z_cache, z_cache_key=cfg.z_cache_key
    )
    cache_id = (cfg.z_cache_key, cfg.window, include_current) if cfg.z_cache is not None else None
    z = cfg.z_cache.get(cache_id) if (cache_id is not None and cfg.z_cache is not None) else None
    if cache_id is not None:
        record_cache_lookup("zscore", z is not None)
    if z is None:
        z = fc.calendar_sample_z(spreads, sig_el, elig_pref, cfg.window, include_current)
        if cache_id is not None and cfg.z_cache is not None:
            cfg.z_cache[cache_id] = z
    specs = fc.calendar_zscore_scan(
        z,
        ts_days,
        dte,
        sig_el,
        ent_el,
        ext_el,
        cfg.delay,
        cfg.entry_z,
        cfg.exit_z,
        float(cfg.max_hold_days),
        cfg.close_before,
        cfg.close_at_end,
    )
    return [
        make_calendar_trade(
            rows[e_i],
            rows[x_i],
            direction,
            cfg.fill,
            CALENDAR_EXIT_REASONS[r_code],
            ("z", rows[s_i], s_val),
        )
        for e_i, x_i, direction, r_code, s_i, s_val in specs
    ]


def zscore_trades(
    rows: list[dict[str, Any]],
    *,
    delay: int,
    fill: str,
    **options: Any,
) -> list[dict[str, Any]]:
    """Evaluate sample-stdev calendar Z-score mean reversion (C99 fastcore + Python fallback)."""
    unknown = set(options) - _ZSCORE_EXTRA_OPTS
    if unknown:
        raise TypeError(f"unexpected keyword argument(s): {sorted(unknown)}")
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
    include_current = cfg.delay == 0
    cache_id = (cfg.z_cache_key, cfg.window, include_current) if cfg.z_cache is not None else None
    z = cfg.z_cache.get(cache_id) if (cache_id is not None and cfg.z_cache is not None) else None
    if cache_id is not None:
        record_cache_lookup("zscore", z is not None)
    if z is None:
        z = _compute_sample_z_py(rows, cfg.window, include_current)
        if cache_id is not None and cfg.z_cache is not None:
            cfg.z_cache[cache_id] = z
    return _zscore_scan_py(rows, z, cfg)


def compute_calendar_zscore_entry_bands(
    series: list[dict[str, Any]],
    params: Mapping[str, Any],
    fill: str,
    *,
    far_point_value_rub: float = 1.0,
) -> list[EntryBandPoint]:
    """Compute causal sample-stdev entry bands for native/H1 calendar spreads."""
    del far_point_value_rub
    window = int(params["window"])
    entry_z = float(params["entry_z"])
    reference = fill == "reference_same_bar"
    history: list[float] = []
    points: list[EntryBandPoint] = []
    for row in series:
        date_str = str(row.get("date", ""))
        if not row.get("volume_eligible", True):
            points.append(EntryBandPoint(date=date_str))
            continue
        point = float(row.get("point_value_rub", 1.0))
        value = float(row["spread"]) * point
        sample = [*history, value][-window:] if reference else history[-window:]
        buy_level = sell_level = buy_rel = sell_rel = None
        if len(sample) == window:
            mean, std = statistics.mean(sample), statistics.stdev(sample)
            if std > 0:
                width = entry_z * std
                buy_level = (mean - width) / point
                sell_level = (mean + width) / point
                far_val = float(row["far"])
                if far_val:
                    buy_rel = buy_level / far_val * 100
                    sell_rel = sell_level / far_val * 100
        history.append(value)
        points.append(
            EntryBandPoint(
                date=date_str,
                buy_level=buy_level,
                sell_level=sell_level,
                buy_level_rel=buy_rel,
                sell_level_rel=sell_rel,
            )
        )
    return points


__all__ = [
    "compute_calendar_zscore_entry_bands",
    "zscore_trades",
]
