"""Calendar spread rolling sample Z-score mean-reversion engine and causal entry bands."""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime
from typing import Any, Mapping

from moex_spread_lab.core._fastcore_loader import load_fastcore
from moex_spread_lab.core.contracts import EntryBandPoint
from ..base import (
    CALENDAR_EXIT_REASONS,
    calendar_row_buffers,
    elapsed_calendar_days,
    make_calendar_trade,
)


def zscore_trades(
    rows: list[dict[str, Any]],
    *,
    delay: int,
    fill: str,
    entry_z: float = 1.5,
    exit_z: float = 0.5,
    window: int = 20,
    max_hold_days: int = 30,
    close_before: int = 7,
    skip_dividend_windows: bool = True,
    close_at_end: bool = False,
    z_cache: dict | None = None,
    z_cache_key: Any = None,
) -> list[dict[str, Any]]:
    """Evaluate sample-stdev calendar Z-score mean reversion (C99 fastcore + Python fallback)."""
    if not rows or (skip_dividend_windows and rows[0]["dividend_in_window"]):
        return []
    include_current = delay == 0
    n = len(rows)
    fc = load_fastcore()
    if fc is not None and window >= 2 and delay >= 0:
        spreads, _, ts_days, dte, sig_el, ent_el, ext_el, elig_pref = calendar_row_buffers(
            rows, z_cache=z_cache, z_cache_key=z_cache_key
        )
        cache_id = (z_cache_key, window, include_current) if z_cache is not None else None
        z = z_cache.get(cache_id) if cache_id is not None else None
        if z is None:
            z = fc.calendar_sample_z(spreads, sig_el, elig_pref, int(window), include_current)
            if cache_id is not None:
                z_cache[cache_id] = z
        specs = fc.calendar_zscore_scan(
            z,
            ts_days,
            dte,
            sig_el,
            ent_el,
            ext_el,
            int(delay),
            float(entry_z),
            float(exit_z),
            float(max_hold_days),
            int(close_before),
            bool(close_at_end),
        )
        return [
            make_calendar_trade(
                rows[e_i], rows[x_i], direction, fill, CALENDAR_EXIT_REASONS[r_code], "z", rows[s_i], s_val
            )
            for e_i, x_i, direction, r_code, s_i, s_val in specs
        ]
    cache_id = (z_cache_key, window, include_current) if z_cache is not None else None
    z = z_cache.get(cache_id) if cache_id is not None else None
    if z is None:
        z = [math.nan] * n
        spreads_py = [float(r["spread"]) for r in rows]
        elig = [bool(r.get("signal_eligible", True)) for r in rows]
        elig_pref_py = [0] * (n + 1)
        for idx, flag in enumerate(elig):
            elig_pref_py[idx + 1] = elig_pref_py[idx] + flag
        for index in range(window - include_current, n):
            if not elig[index]:
                continue
            start = index - window + 1 if include_current else index - window
            end = index + 1 if include_current else index
            if elig_pref_py[end] - elig_pref_py[start] != window:
                continue
            sample = spreads_py[start:end]
            deviation = statistics.stdev(sample)
            z[index] = (spreads_py[index] - statistics.mean(sample)) / deviation if deviation > 0 else math.nan
        if cache_id is not None:
            z_cache[cache_id] = z
    position, entry, trades, signal_index = 0, None, [], 0
    entry_dt = None
    parsed_dts: list[Any] | None = None
    for index, row in enumerate(rows):
        if position and row.get("exit_eligible", True):
            exit_signal = z[index - delay] if index >= delay else math.nan
            if abs(exit_signal) <= exit_z:
                reason = "z_reverted"
            else:
                if parsed_dts is None:
                    parsed_dts = [None] * n
                row_dt = parsed_dts[index]
                if row_dt is None:
                    s = str(row["date"])
                    if "T" in s:
                        row_dt = datetime.fromisoformat(s.replace("Z", ""))
                    else:
                        row_dt = date.fromisoformat(s[:10])
                    parsed_dts[index] = row_dt
                if type(entry_dt) is type(row_dt):
                    held = (
                        (row_dt - entry_dt).total_seconds() / 86400
                        if isinstance(row_dt, datetime)
                        else (row_dt - entry_dt).days
                    )
                else:
                    held = elapsed_calendar_days(entry["date"], row["date"])
                if held >= max_hold_days:
                    reason = "max_hold"
                elif row["days_to_near_expiry"] <= close_before:
                    reason = "near_expiry_buffer"
                else:
                    reason = None
            if reason:
                trades.append(
                    make_calendar_trade(entry, row, position, fill, reason, "z", rows[signal_index], z[signal_index])
                )
                position = 0
                entry_dt = None
        signal = index - delay
        if (
            not position
            and signal >= 0
            and rows[signal].get("signal_eligible", True)
            and row.get("entry_eligible", True)
            and not math.isnan(z[signal])
            and row["days_to_near_expiry"] > close_before
            and abs(z[signal]) >= entry_z
        ):
            position, entry, signal_index = (-1 if z[signal] > 0 else 1), row, signal
            if parsed_dts is None:
                parsed_dts = [None] * n
            entry_dt = parsed_dts[index]
            if entry_dt is None:
                s = str(row["date"])
                entry_dt = datetime.fromisoformat(s.replace("Z", "")) if "T" in s else date.fromisoformat(s[:10])
                parsed_dts[index] = entry_dt
    if position and close_at_end:
        trades.append(
            make_calendar_trade(
                entry, rows[-1], position, fill, "end_of_window", "z", rows[signal_index], z[signal_index]
            )
        )
    return trades


def compute_calendar_zscore_entry_bands(
    series: list[dict[str, Any]],
    params: Mapping[str, Any],
    fill: str,
    *,
    far_point_value_rub: float = 1.0,
) -> list[EntryBandPoint]:
    """Compute causal sample-stdev entry bands for native/H1 calendar spreads."""
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
        sample = (history + [value])[-window:] if reference else history[-window:]
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
