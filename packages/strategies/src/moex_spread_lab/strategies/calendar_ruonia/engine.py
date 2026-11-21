"""Implied-rate convergence to RUONIA + dividend-window adjustment engine and entry bands."""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Mapping

from moex_spread_lab.core._fastcore_loader import load_fastcore
from moex_spread_lab.core.contracts import EntryBandPoint
from ..base import (
    CALENDAR_EXIT_REASONS,
    calendar_row_buffers,
    make_calendar_trade,
)

QUARTER_DAYS = 91
SBER_EX_DATES: tuple[tuple[date, float], ...] = (
    (date(2023, 5, 9), 25.0),
    (date(2024, 7, 9), 33.3),
    (date(2025, 7, 16), 34.84),
    (date(2026, 7, 18), 37.64),
)
DIVIDEND_SHARES_PER_CONTRACT = 100


def window_dividend_rub(asset: str, expiry_date: date) -> float:
    """Dividend adjustment in RUB per contract for SBRF quarterly cycles."""
    if asset != "SR":
        return 0.0
    return (
        sum(
            value
            for ex, value in SBER_EX_DATES
            if expiry_date < ex <= expiry_date + timedelta(days=QUARTER_DAYS)
        )
        * DIVIDEND_SHARES_PER_CONTRACT
    )


def implied_ruonia_deviation_pp(
    spread_value: float,
    near_value: float,
    dividend_rub: float,
    ruonia_rate: float | None,
    *,
    convergence_supported: bool = True,
) -> float:
    """Implied annualized rate minus RUONIA in percentage points."""
    if not convergence_supported or ruonia_rate is None or near_value <= 0:
        return math.nan
    implied = (spread_value + dividend_rub) / near_value * 365 / QUARTER_DAYS
    return (implied - ruonia_rate) * 100


def convergence_trades(
    rows: list[dict[str, Any]],
    *,
    threshold: float,
    delay: int,
    fill: str,
    exit_days: int = 2,
    close_at_end: bool = False,
    z_cache: dict | None = None,
    z_cache_key: Any = None,
) -> list[dict[str, Any]]:
    """Evaluate RUONIA convergence trades (C99 fastcore + Python fallback)."""
    if not rows or rows[0]["dividend_in_window"]:
        return []
    fc = load_fastcore()
    if fc is not None and delay >= 0:
        _, dev_pp, _, dte, sig_el, ent_el, ext_el, _ = calendar_row_buffers(
            rows, z_cache=z_cache, z_cache_key=z_cache_key
        )
        specs = fc.calendar_convergence_scan(
            dev_pp, dte, sig_el, ent_el, ext_el, float(threshold), int(delay), int(exit_days), bool(close_at_end)
        )
        return [
            make_calendar_trade(
                rows[e_i], rows[x_i], direction, fill, CALENDAR_EXIT_REASONS[r_code], "dev_pp", rows[s_i], s_val
            )
            for e_i, x_i, direction, r_code, s_i, s_val in specs
        ]
    position, entry, trades, signal_row = 0, None, [], None
    for index, row in enumerate(rows):
        if position and row.get("exit_eligible", True) and row["days_to_near_expiry"] <= exit_days:
            trades.append(
                make_calendar_trade(
                    entry, row, position, fill, "near_expiry_buffer", "dev_pp", signal_row, signal_row["dev_pp"]
                )
            )
            position = 0
        signal = rows[index - delay] if index >= delay else None
        if (
            not position
            and signal
            and signal.get("signal_eligible", True)
            and row.get("entry_eligible", True)
            and row["days_to_near_expiry"] > exit_days
            and not math.isnan(signal["dev_pp"])
            and abs(signal["dev_pp"]) >= threshold
        ):
            position, entry, signal_row = (-1 if signal["dev_pp"] > 0 else 1), row, signal
    if position and close_at_end:
        trades.append(
            make_calendar_trade(
                entry, rows[-1], position, fill, "end_of_window", "dev_pp", signal_row, signal_row["dev_pp"]
            )
        )
    return trades


def compute_ruonia_entry_bands(
    series: list[dict[str, Any]],
    params: Mapping[str, Any],
    fill: str,
    *,
    far_point_value_rub: float = 1.0,
) -> list[EntryBandPoint]:
    """Compute causal RUONIA implied-rate threshold bands in raw spread price units."""
    threshold = float(params["threshold_pp"])
    points: list[EntryBandPoint] = []
    for row in series:
        date_str = str(row.get("date", ""))
        if not row.get("volume_eligible", True):
            points.append(EntryBandPoint(date=date_str))
            continue
        dev = row.get("dev_pp")
        scale = row.get("rate_price_per_pp")
        buy_level = sell_level = buy_rel = sell_rel = None
        if dev is not None and scale is not None and math.isfinite(dev):
            spread = float(row["spread"])
            buy_level = spread + (-threshold - dev) * float(scale)
            sell_level = spread + (threshold - dev) * float(scale)
            far_val = float(row["far"])
            if far_val:
                buy_rel = buy_level / far_val * 100
                sell_rel = sell_level / far_val * 100
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
    "DIVIDEND_SHARES_PER_CONTRACT",
    "QUARTER_DAYS",
    "SBER_EX_DATES",
    "compute_ruonia_entry_bands",
    "convergence_trades",
    "implied_ruonia_deviation_pp",
    "window_dividend_rub",
]
