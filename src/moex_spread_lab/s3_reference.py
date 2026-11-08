"""S3: reproduce the reference SBRF calendar-spread backtest, then re-run it with this project's fills and costs."""

from __future__ import annotations

import csv
import json
import math
import statistics
from bisect import bisect_right
from datetime import date
from pathlib import Path
from typing import Any

from .costs import side_cost, tariff_profile
from .screener_app import MIN_DAILY_VOLUME
from .w0 import W0Error

REFERENCE_ROUND_TRIP_COST = 4 * 5.5 + 2 * 2.0
SPREAD_MULTIPLIER = {"secid": "SR", "tariff_group": "stock", "tick_value": 1.0, "tick_size": 1.0}
FILLS = (
    ("reference_same_bar", 0, "close"),
    ("next_close", 1, "close"),
    ("next_open", 1, "open"),
)


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


def _price(row: dict[str, Any], fill: str) -> float:
    return row["open_spread"] if fill == "open" else row["spread"]


def _convergence(rows, *, threshold: float, delay: int, fill: str, exit_days: int = 2):
    position, entry, trades = 0, None, []
    for index, row in enumerate(rows):
        if position and row["days_to_near_expiry"] <= exit_days:
            trades.append((entry, row, position, _price(entry, fill), _price(row, fill)))
            position = 0
        signal = rows[index - delay] if index >= delay else None
        if (not position and signal and row["days_to_near_expiry"] > exit_days and not math.isnan(signal["dev_pp"])
                and not rows[0]["dividend_in_window"] and abs(signal["dev_pp"]) >= threshold):
            position, entry = (-1 if signal["dev_pp"] > 0 else 1), row
    return trades


def _zscore(rows, *, delay: int, fill: str, entry_z: float = 1.5, exit_z: float = 0.5, window: int = 20,
            max_hold_days: int = 30, close_before: int = 7):
    if rows[0]["dividend_in_window"]:
        return []
    include_current = delay == 0
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
                trades.append((entry, row, position, _price(entry, fill), _price(row, fill)))
                position = 0
        signal = index - delay
        if (not position and signal >= 0 and not math.isnan(z[signal]) and row["days_to_near_expiry"] > close_before
                and abs(z[signal]) >= entry_z):
            position, entry = (-1 if z[signal] > 0 else 1), row
    return trades


def _account(label: str, per_pair: dict[str, list], benchmark: dict[str, float], profile: dict[str, Any] | None) -> dict[str, Any]:
    dates = sorted(benchmark)

    def price(day: str) -> float:
        return benchmark[dates[max(0, bisect_right(dates, day) - 1)]]

    def side(row: dict[str, Any]) -> float:
        legs = [({**SPREAD_MULTIPLIER, "secid": "near"}, row["near_close"]), ({**SPREAD_MULTIPLIER, "secid": "far"}, row["far_close"])]
        return sum(side_cost(profile, legs, 1).values())

    gross = costs = lqdt_on_margin = 0.0
    trades = []
    for pair, items in per_pair.items():
        for entry, exit_row, direction, entry_price, exit_price in items:
            pnl = direction * (exit_price - entry_price)
            cost = side(entry) + side(exit_row) if profile else REFERENCE_ROUND_TRIP_COST
            gross += pnl
            costs += cost
            lqdt_on_margin += entry["margin"] * (price(exit_row["date"]) / price(entry["date"]) - 1)
            trades.append({"pair": pair, "entry": entry["date"], "exit": exit_row["date"], "direction": direction,
                           "entry_spread": entry_price, "exit_spread": exit_price, "gross_rub": round(pnl, 2),
                           "costs_rub": round(cost, 2)})
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


def run_s3(reference_dir: Path, benchmark_path: Path, *, slippage_ticks: float = 1.0) -> dict[str, Any]:
    pairs = load_reference(reference_dir)
    try:
        payload = json.loads(benchmark_path.read_text(encoding="utf-8"))
        benchmark = {row["date"]: float(row["price"]) for row in payload["rows"]}
    except (OSError, ValueError, KeyError) as exc:
        raise W0Error(f"cannot read benchmark: {exc}") from exc
    profile = tariff_profile(slippage_ticks)
    variants = []
    for label, delay, fill in FILLS:
        for strategy, run in (
            ("convergence_to_ruonia", lambda rows: _convergence(rows, threshold=1.0, delay=delay, fill=fill)),
            ("zscore_mean_reversion", lambda rows: _zscore(rows, delay=delay, fill=fill)),
        ):
            per_pair = {p: run(rows) for p, rows in pairs.items()}
            variants.append({"strategy": strategy, "cost_model": "reference_flat_round_trip", **_account(label, per_pair, benchmark, None)})
            variants.append({"strategy": strategy, "cost_model": "published_tariff", **_account(label, per_pair, benchmark, profile)})
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
