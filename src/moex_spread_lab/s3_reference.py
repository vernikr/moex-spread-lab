"""S3: reproduce the reference SBRF calendar-spread backtest, then stress its fills and liquidity."""

from __future__ import annotations

import csv
import json
import math
import statistics
from bisect import bisect_right
from datetime import date
from pathlib import Path
from typing import Any

from .w0 import W0Error

FEE_PER_CONTRACT = 5.5
SLIPPAGE_POINTS = 2.0
ROUND_TRIP_COST = 4 * FEE_PER_CONTRACT + 2 * SLIPPAGE_POINTS
INITIAL_CAPITAL = 30_000.0


def _volumes(path: Path) -> dict[str, int]:
    with path.open(encoding="utf-8") as handle:
        return {row["begin"][:10]: int(float(row["volume"])) for row in csv.DictReader(handle)}


def load_reference(reference_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Group the reference's processed SBRF spread rows by near/far pair with leg volumes."""
    processed = reference_dir / "data" / "processed"
    try:
        with (processed / "spreads_sbrf.csv").open(encoding="utf-8") as handle:
            source = list(csv.DictReader(handle))
        volumes = {secid: _volumes(processed / f"futures_{secid}.csv") for secid in {r[k] for r in source for k in ("near", "far")}}
    except (OSError, KeyError, ValueError) as exc:
        raise W0Error(f"cannot read reference SBRF inputs: {exc}") from exc
    pairs: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(source, key=lambda r: r["date"]):
        dev = float(row["dev_ruonia"]) if row["dev_ruonia"] not in ("", "nan") else math.nan
        pairs.setdefault(f"{row['near']}/{row['far']}", []).append({
            "date": row["date"],
            "spread": float(row["spread"]),
            "dev_pp": dev,
            "days_to_near_expiry": int(row["days_to_near_exp"]),
            "dividend_in_window": float(row["div_window"]) > 0,
            "margin": float(row["go_sum"]),
            "min_volume": min(volumes[row["near"]].get(row["date"], 0), volumes[row["far"]].get(row["date"], 0)),
        })
    return pairs


def _convergence(rows, *, threshold: float, delay: int, min_volume: int, exit_days: int = 2):
    position, entry, trades = 0, None, []
    for index, row in enumerate(rows):
        if position and row["days_to_near_expiry"] <= exit_days:
            trades.append((entry, row, position))
            position = 0
        signal = rows[index - delay] if index >= delay else None
        if (not position and signal and row["days_to_near_expiry"] > exit_days and not math.isnan(signal["dev_pp"])
                and not rows[0]["dividend_in_window"] and abs(signal["dev_pp"]) >= threshold
                and min(signal["min_volume"], row["min_volume"]) >= min_volume):
            position, entry = (-1 if signal["dev_pp"] > 0 else 1), row
    return trades


def _zscore(rows, *, delay: int, min_volume: int, include_current: bool, entry_z: float = 1.5, exit_z: float = 0.5,
            window: int = 20, max_hold_days: int = 30, close_before: int = 7):
    if rows[0]["dividend_in_window"]:
        return []
    spreads = [r["spread"] for r in rows]
    z = [math.nan] * len(rows)
    for index in range(window - include_current, len(rows)):
        sample = spreads[index - window + 1:index + 1] if include_current else spreads[index - window:index]
        deviation = statistics.stdev(sample)
        z[index] = (spreads[index] - statistics.mean(sample)) / deviation if deviation > 0 else math.nan
    position, entry, trades = 0, None, []
    for index, row in enumerate(rows):
        if position:
            held = (date.fromisoformat(row["date"]) - date.fromisoformat(entry["date"])).days
            exit_signal = z[index - delay] if index >= delay else math.nan
            if abs(exit_signal) <= exit_z or held >= max_hold_days or row["days_to_near_expiry"] <= close_before:
                trades.append((entry, row, position))
                position = 0
        signal = index - delay
        if (not position and signal >= 0 and not math.isnan(z[signal]) and row["days_to_near_expiry"] > close_before
                and abs(z[signal]) >= entry_z and min(rows[signal]["min_volume"], row["min_volume"]) >= min_volume):
            position, entry = (-1 if z[signal] > 0 else 1), row
    return trades


def _account(label: str, per_pair: dict[str, list], benchmark: dict[str, float]) -> dict[str, Any]:
    dates = sorted(benchmark)

    def price(day: str) -> float:
        return benchmark[dates[max(0, bisect_right(dates, day) - 1)]]

    gross = lqdt_on_margin = 0.0
    trades = []
    for pair, items in per_pair.items():
        for entry, exit_row, direction in items:
            pnl = direction * (exit_row["spread"] - entry["spread"])
            hurdle = entry["margin"] * (price(exit_row["date"]) / price(entry["date"]) - 1)
            gross += pnl
            lqdt_on_margin += hurdle
            trades.append({"pair": pair, "entry": entry["date"], "exit": exit_row["date"], "direction": direction,
                           "entry_spread": entry["spread"], "exit_spread": exit_row["spread"], "gross_rub": round(pnl, 2)})
    net = gross - ROUND_TRIP_COST * len(trades)
    return {
        "variant": label,
        "trades": len(trades),
        "winners": sum(t["gross_rub"] - ROUND_TRIP_COST > 0 for t in trades),
        "gross_rub": round(gross, 2),
        "net_rub": round(net, 2),
        "lqdt_on_pair_margin_while_open_rub": round(lqdt_on_margin, 2),
        "overlay_excess_rub": round(net - lqdt_on_margin, 2),
        "trade_list": trades,
    }


def run_s3(reference_dir: Path, benchmark_path: Path) -> dict[str, Any]:
    pairs = load_reference(reference_dir)
    try:
        payload = json.loads(benchmark_path.read_text(encoding="utf-8"))
        benchmark = {row["date"]: float(row["price"]) for row in payload["rows"]}
    except (OSError, ValueError, KeyError) as exc:
        raise W0Error(f"cannot read benchmark: {exc}") from exc
    variants = []
    for label, options in (
        ("reference_same_bar", {"delay": 0, "min_volume": 0}),
        ("delay_1", {"delay": 1, "min_volume": 0}),
        ("delay_1_min_volume_100", {"delay": 1, "min_volume": 100}),
        ("delay_1_min_volume_500", {"delay": 1, "min_volume": 500}),
        ("delay_1_min_volume_1000", {"delay": 1, "min_volume": 1000}),
    ):
        variants.append({"strategy": "convergence_to_ruonia", "threshold_pp": 1.0, **_account(
            label, {p: _convergence(rows, threshold=1.0, **options) for p, rows in pairs.items()}, benchmark)})
        zoptions = {**options, "include_current": options["delay"] == 0}
        variants.append({"strategy": "zscore_mean_reversion", **_account(
            label, {p: _zscore(rows, **zoptions) for p, rows in pairs.items()}, benchmark)})
    return {
        "schema_version": "s3-sbrf-reference.v1",
        "release_state": "exploratory_inconclusive",
        "interpretation": (
            "reference_same_bar reproduces the reference rules (signal and fill on the same candle close, rolling window including "
            "the current observation, no liquidity filter); the delayed variants are this project's accounting"
        ),
        "cost_model": {"round_trip_rub": ROUND_TRIP_COST, "basis": "reference exchange fee and 2-point slippage; broker fee zero"},
        "benchmark": "LQDT lineage; overlay excess subtracts LQDT return on pair margin while a position is open",
        "pairs": {pair: {"rows": len(rows), "median_min_leg_volume": statistics.median(r["min_volume"] for r in rows)}
                  for pair, rows in pairs.items()},
        "variants": variants,
    }
