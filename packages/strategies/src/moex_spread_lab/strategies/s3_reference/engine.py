"""S3: reproduce the reference SBRF calendar-spread backtest, then re-run it with this project's fills and costs."""

from __future__ import annotations

import csv
import json
import math
from bisect import bisect_left, bisect_right
from pathlib import Path
from typing import Any

from moex_spread_lab.accounting.costs import side_cost, tariff_profile
from moex_spread_lab.core._fastcore_loader import load_fastcore
from moex_spread_lab.core.settings import MIN_DAILY_VOLUME
from moex_spread_lab.core.w0 import W0Error
from ..base import (
    CALENDAR_EXIT_REASONS,
    CALENDAR_FILLS,
    calendar_row_buffers,
    elapsed_calendar_days,
    make_calendar_trade,
)

REFERENCE_ROUND_TRIP_COST = 4 * 5.5 + 2 * 2.0
SPREAD_MULTIPLIER = {"secid": "SR", "tariff_group": "stock", "tick_value": 1.0, "tick_size": 1.0}
FILLS = CALENDAR_FILLS


def _legs(path: Path) -> dict[str, dict[str, float]]:
    with path.open(encoding="utf-8") as handle:
        return {
            row["begin"][:10]: {k: float(row[k]) for k in ("open", "close", "volume")}
            for row in csv.DictReader(handle)
        }


def load_reference(reference_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Group the reference's processed SBRF spread rows by near/far pair with leg prices and volumes."""
    processed = reference_dir / "data" / "processed"
    try:
        with (processed / "spreads_sbrf.csv").open(encoding="utf-8") as handle:
            source = list(csv.DictReader(handle))
        legs = {secid: _legs(processed / f"futures_{secid}.csv") for secid in {r[k] for r in source for k in ("near", "far")}}
    except (OSError, KeyError, ValueError) as exc:
        raise W0Error(f"cannot read reference SBRF inputs: {exc}") from exc
    pairs: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(source, key=lambda r: r["date"]):
        near, far = legs[row["near"]][row["date"]], legs[row["far"]][row["date"]]
        pairs.setdefault(f"{row['near']}/{row['far']}", []).append({
            "date": row["date"],
            "spread": float(row["spread"]),
            "open_spread": far["open"] - near["open"],
            "near_close": near["close"],
            "far_close": far["close"],
            "dev_pp": float(row["dev_ruonia"]) if row["dev_ruonia"] not in ("", "nan") else math.nan,
            "days_to_near_expiry": int(row["days_to_near_exp"]),
            "dividend_in_window": float(row["div_window"]) > 0,
            "margin": float(row["go_sum"]),
            "min_volume": min(near["volume"], far["volume"]),
        })
    return pairs


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
    import statistics
    from datetime import date, datetime

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


def trade_costs(profile: dict[str, Any] | None, row: dict[str, Any], quantity: int, native_spread: bool) -> dict[str, float]:
    """Cost of one side at a row's leg values; the reference flat model splits its round trip evenly."""
    if profile is None:
        return {"reference_flat": REFERENCE_ROUND_TRIP_COST / 2 * quantity}
    leg = {**SPREAD_MULTIPLIER, "tariff_group": profile.get("tariff_group", "stock")}
    legs = [({**leg, "secid": "near"}, row["near_close"]), ({**leg, "secid": "far"}, row["far_close"])]
    return side_cost(profile, legs, quantity, native_spread=native_spread)


def account_trades(
    label: str, per_pair: dict[str, list], benchmark: dict[str, float], profile: dict[str, Any] | None,
    *, native_spread: bool = False, quantity: int = 1, benchmark_dates: list[str] | None = None,
) -> dict[str, Any]:
    dates = benchmark_dates if benchmark_dates is not None else sorted(benchmark)

    def price(observation: str | dict[str, Any]) -> float:
        mark_day = observation.get("benchmark_mark_date") if isinstance(observation, dict) else None
        day = observation.get("date") if isinstance(observation, dict) else observation
        target = str(mark_day or day)[:10]
        index = bisect_right(dates, target) - 1 if mark_day is not None or "T" not in str(day) else bisect_left(dates, target) - 1
        return benchmark[dates[max(0, index)]]

    gross = costs = lqdt_on_margin = 0.0
    trades = []
    for pair, items in per_pair.items():
        for item in items:
            entry, exit_row, direction = item["entry"], item["exit"], item["direction"]
            pnl = direction * (item["exit_price"] - item["entry_price"]) * quantity
            entry_cost, exit_cost = (trade_costs(profile, row, quantity, native_spread) for row in (entry, exit_row))
            cost = sum(entry_cost.values()) + sum(exit_cost.values())
            hurdle = entry["margin"] * quantity * (price(exit_row) / price(entry) - 1)
            gross += pnl
            costs += cost
            lqdt_on_margin += hurdle
            trades.append({
                "pair": pair, "entry": entry["date"], "exit": exit_row["date"], "direction": direction, "quantity": quantity,
                "entry_spread": item["entry_price"], "exit_spread": item["exit_price"], "gross_rub": round(pnl, 2),
                "costs_rub": round(cost, 2), "entry_costs": entry_cost, "exit_costs": exit_cost,
                "reason": item["reason"], "signal": item["signal"], "margin_rub": round(entry["margin"] * quantity, 2),
                "lqdt_on_margin_rub": round(hurdle, 2),
                "entry_row": entry, "exit_row": exit_row,
            })
    net = gross - costs
    return {
        "variant": label,
        "trades": len(trades),
        "winners": sum(t["gross_rub"] - t["costs_rub"] > 0 for t in trades),
        "gross_rub": round(gross, 2),
        "costs_rub": round(costs, 2),
        "net_rub": round(net, 2),
        "lqdt_on_pair_margin_while_open_rub": round(lqdt_on_margin, 2),
        "excess_over_lqdt_on_margin_rub": round(net - lqdt_on_margin, 2),
        "trade_list": trades,
    }


def strategy_runner(strategy: str, *, delay: int, fill: str, close_at_end: bool, skip_dividend_windows_in_zscore: bool,
                    threshold: float = 1.0, zscore_options: dict[str, Any] | None = None,
                    z_cache: dict | None = None, z_cache_key: Any = None):
    if strategy == "convergence_to_ruonia":
        return lambda rows: convergence_trades(
            rows, threshold=threshold, delay=delay, fill=fill, close_at_end=close_at_end,
            z_cache=z_cache, z_cache_key=z_cache_key)
    if strategy == "zscore_mean_reversion":
        return lambda rows: zscore_trades(
            rows, delay=delay, fill=fill, skip_dividend_windows=skip_dividend_windows_in_zscore,
            close_at_end=close_at_end, z_cache=z_cache, z_cache_key=z_cache_key, **(zscore_options or {}))
    raise W0Error(f"unknown strategy: {strategy}")


def evaluate_variants(
    pairs: dict[str, list[dict[str, Any]]], benchmark: dict[str, float], profile: dict[str, Any],
    *, native_spread: bool = False, reference_cost: bool = True, skip_dividend_windows_in_zscore: bool = True,
    close_at_end: bool = False, zscore_options: dict[str, Any] | None = None, quantity: int = 1,
) -> list[dict[str, Any]]:
    """Run both strategies under every fill rule on prepared pair rows."""
    variants = []
    for label, delay, fill in FILLS:
        for strategy in ("convergence_to_ruonia", "zscore_mean_reversion"):
            run = strategy_runner(strategy, delay=delay, fill=fill, close_at_end=close_at_end,
                                  skip_dividend_windows_in_zscore=skip_dividend_windows_in_zscore, zscore_options=zscore_options)
            per_pair = {p: run(rows) for p, rows in pairs.items()}
            if reference_cost:
                variants.append({"strategy": strategy, "cost_model": "reference_flat_round_trip", **account_trades(label, per_pair, benchmark, None)})
            variants.append({"strategy": strategy, "cost_model": "published_tariff", **account_trades(
                label, per_pair, benchmark, profile, native_spread=native_spread, quantity=quantity)})
    return variants


def read_benchmark(path: Path) -> dict[str, float]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {row["date"]: float(row["price"]) for row in payload["rows"]}
    except (OSError, ValueError, KeyError) as exc:
        raise W0Error(f"cannot read benchmark: {exc}") from exc


def run_s3(reference_dir: Path, benchmark_path: Path, *, slippage_ticks: float = 1.0) -> dict[str, Any]:
    pairs = load_reference(reference_dir)
    benchmark = read_benchmark(benchmark_path)
    variants = evaluate_variants(pairs, benchmark, tariff_profile(slippage_ticks))
    return {
        "schema_version": "s3-sbrf-reference.v2",
        "release_state": "exploratory_inconclusive",
        "interpretation": (
            "reference_same_bar reproduces the reference rules (signal and fill on the same candle close, rolling window including "
            "the current observation) under its flat RUB 26 round-trip cost; next_close and next_open are this project's fills "
            "(signal at the close, fill one close later or at the next open)"
        ),
        "benchmark": "LQDT lineage; excess over LQDT subtracts LQDT return on pair margin while a position is open",
        "pairs": {
            pair: {
                "rows": len(rows),
                "min_leg_daily_volume": min(r["min_volume"] for r in rows),
                "tradable_every_day": min(r["min_volume"] for r in rows) >= MIN_DAILY_VOLUME,
            }
            for pair, rows in pairs.items()
        },
        "variants": variants,
    }
