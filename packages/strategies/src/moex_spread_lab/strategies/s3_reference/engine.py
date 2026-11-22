"""S3: reproduce the reference SBRF calendar-spread backtest, then re-run it with this project's fills and costs."""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

from moex_spread_lab.accounting.costs import tariff_profile
from moex_spread_lab.core.settings import MIN_DAILY_VOLUME
from moex_spread_lab.core.w0 import W0Error

from ..base import (
    CALENDAR_EXIT_REASONS,
    CALENDAR_FILLS,
    calendar_price,
    calendar_row_buffers,
    elapsed_calendar_days,
    make_calendar_trade,
)
from ..calendar_eval import (
    FILLS,
    REFERENCE_ROUND_TRIP_COST,
    SPREAD_MULTIPLIER,
    account_trades,
    convergence_trades,
    evaluate_variants,
    read_benchmark,
    strategy_runner,
    trade_costs,
    zscore_trades,
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
        legs = {
            secid: _legs(processed / f"futures_{secid}.csv")
            for secid in {r[k] for r in source for k in ("near", "far")}
        }
    except (OSError, KeyError, ValueError) as exc:
        raise W0Error(f"cannot read reference SBRF inputs: {exc}") from exc
    pairs: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(source, key=lambda r: r["date"]):
        near, far = legs[row["near"]][row["date"]], legs[row["far"]][row["date"]]
        pairs.setdefault(f"{row['near']}/{row['far']}", []).append(
            {
                "date": row["date"],
                "spread": float(row["spread"]),
                "open_spread": far["open"] - near["open"],
                "near_close": near["close"],
                "far_close": far["close"],
                "dev_pp": float(row["dev_ruonia"])
                if row["dev_ruonia"] not in ("", "nan")
                else math.nan,
                "days_to_near_expiry": int(row["days_to_near_exp"]),
                "dividend_in_window": float(row["div_window"]) > 0,
                "margin": float(row["go_sum"]),
                "min_volume": min(near["volume"], far["volume"]),
            }
        )
    return pairs


def run_s3(
    reference_dir: Path, benchmark_path: Path, *, slippage_ticks: float = 1.0
) -> dict[str, Any]:
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


__all__ = [
    "CALENDAR_EXIT_REASONS",
    "CALENDAR_FILLS",
    "FILLS",
    "REFERENCE_ROUND_TRIP_COST",
    "SPREAD_MULTIPLIER",
    "account_trades",
    "calendar_price",
    "calendar_row_buffers",
    "convergence_trades",
    "elapsed_calendar_days",
    "evaluate_variants",
    "load_reference",
    "make_calendar_trade",
    "read_benchmark",
    "run_s3",
    "strategy_runner",
    "trade_costs",
    "zscore_trades",
]
