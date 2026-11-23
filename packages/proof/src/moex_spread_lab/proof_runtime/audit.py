"""Independent re-computation and data audit of the screener's price-only control."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import date
from typing import Any, cast

from moex_spread_lab.accounting.costs import tariff_profile
from moex_spread_lab.core import universe as _universe
from moex_spread_lab.core.universe import MIN_DAILY_VOLUME
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.data.lake.data_lake import BarSeries
from moex_spread_lab.strategies.pair_mean_reversion.analytics import run_mean_reversion_baseline
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs
from moex_spread_lab.strategies.pair_screener.screener import _normalized_series

PAIRS: tuple[dict[str, str], ...] = cast(tuple[dict[str, str], ...], _universe.PAIRS)
SLIPPAGE_GRID = (0.0, 1.0, 2.0)
_INDEPENDENT_GROSS_OPTS = frozenset({"lookback", "entry_z", "exit_z", "delay", "fill"})


def _candles(series: BarSeries) -> dict[str, dict[str, Any]]:
    return {row["begin"]: row for row in series.rows if isinstance(row.get("begin"), str)}


def _parse_independent_gross_options(
    options: dict[str, Any],
) -> tuple[int, float, float, int, str]:
    unknown = set(options) - _INDEPENDENT_GROSS_OPTS
    if unknown:
        raise TypeError(f"unexpected keyword argument(s): {sorted(unknown)}")
    return (
        options.get("lookback", 20),
        options.get("entry_z", 2.0),
        options.get("exit_z", 0.5),
        options.get("delay", 1),
        options.get("fill", "close"),
    )


def independent_gross(
    near: Mapping[str, Mapping[str, Any]],
    far: Mapping[str, Mapping[str, Any]],
    near_mult: float,
    far_mult: float,
    **options: Any,
) -> tuple[float, int]:
    """Re-implement the zero-cost mean-reversion control from raw candles (signal on closes)."""
    lookback, entry_z, exit_z, delay, fill = _parse_independent_gross_options(options)
    stamps = sorted(set(near) & set(far))
    price = "open" if fill == "open" else "close"
    spread: list[float] = [
        near[t]["close"] * near_mult - far[t]["close"] * far_mult for t in stamps
    ]
    filled: list[float] = [near[t][price] * near_mult - far[t][price] * far_mult for t in stamps]
    position, entry, gross, trades = 0, 0.0, 0.0, 0
    for index, value in enumerate(filled):
        signal = index - delay
        if signal < lookback:
            continue
        window = spread[signal - lookback : signal]
        mean = sum(window) / lookback
        sd = math.sqrt(sum((x - mean) ** 2 for x in window) / lookback)
        z = (spread[signal] - mean) / sd if sd > 0 else 0.0
        if position and abs(z) <= exit_z:
            gross += position * (value - entry)
            position, trades = 0, trades + 1
        elif not position and index < len(spread) - 1 and abs(z) >= entry_z:
            position, entry = (1 if z < 0 else -1), value
    if position:
        gross += position * (spread[-1] - entry)
        trades += 1
    return gross, trades


def audit_pairs(inputs: ScreenerInputs) -> dict[str, Any]:
    contracts: dict[str, dict[str, Any]] = {
        row["secid"]: row
        for row in json.loads(inputs.contracts.read_text(encoding="utf-8"))["instruments"]
    }
    rows: list[dict[str, Any]] = []
    for pair in PAIRS:
        near_id, far_id = pair["near"], pair["far"]
        near_path, far_path = (
            inputs.candles(pair["pair_id"], near_id),
            inputs.candles(pair["pair_id"], far_id),
        )
        if not (
            inputs.bar_store.source_available(near_path)
            and inputs.bar_store.source_available(far_path)
        ):
            continue
        try:
            near_source = inputs.bars(
                pair["pair_id"],
                near_id,
                columns=("begin", "open", "high", "low", "close", "volume"),
            )
            far_source = inputs.bars(
                pair["pair_id"], far_id, columns=("begin", "open", "high", "low", "close", "volume")
            )
        except W0Error:
            continue
        near, far = _candles(near_source), _candles(far_source)
        common = set(near) & set(far)
        mult: dict[str, float] = {
            s: contracts[s]["tick_value"] / contracts[s]["tick_size"] for s in (near_id, far_id)
        }
        min_volume: dict[str, float] = {
            s: min(r["volume"] for t, r in c.items() if t in common)
            for s, c in ((near_id, near), (far_id, far))
        }
        item: dict[str, Any] = {
            "pair": pair["pair_id"],
            "rows": len(common),
            "min_daily_volume": min_volume,
            "tradable_every_day": min(min_volume.values()) >= MIN_DAILY_VOLUME,
            "weekend_rows": sum(date.fromisoformat(t[:10]).weekday() >= 5 for t in common),
            "close_outside_range": sum(
                not r["low"] <= r["close"] <= r["high"] for r in (*near.values(), *far.values())
            ),
            "open_outside_range": sum(
                not r["low"] <= r["open"] <= r["high"] for r in (*near.values(), *far.values())
            ),
        }
        try:
            series = _normalized_series(
                near_source,
                far_source,
                inputs.benchmark,
                inputs.contracts,
                near=near_id,
                far=far_id,
            )
            runs = {
                slippage: run_mean_reversion_baseline(
                    series,
                    analysis_role="diagnostic_only",
                    cost_profile=tariff_profile(slippage),
                    fill="open",
                    cash_in_lqdt=True,
                )
                for slippage in SLIPPAGE_GRID
            }
            idle = run_mean_reversion_baseline(
                series,
                analysis_role="diagnostic_only",
                cost_profile=tariff_profile(1.0),
                fill="open",
            )
        except W0Error as exc:
            rows.append({**item, "status": "blocked", "reason": str(exc)})
            continue
        summary = runs[1.0]["summary"]
        gross, trades = independent_gross(near, far, mult[near_id], mult[far_id], fill="open")
        rows.append(
            {
                **item,
                "status": "ok",
                "trades": summary["trade_count"],
                "engine_gross_rub": summary["gross_pnl"],
                "independent_gross_rub": round(gross, 2),
                "independent_trades": trades,
                "engine_matches_independent": abs(gross - summary["gross_pnl"]) < 1e-6
                and trades == summary["trade_count"],
                "tariff_costs_rub": {
                    str(s): runs[s]["summary"]["modeled_costs"] for s in SLIPPAGE_GRID
                },
                "final_X_rub_by_slippage": {
                    str(s): runs[s]["summary"]["final_X"] for s in SLIPPAGE_GRID
                },
                "final_X_rub_idle_cash_slippage_1": idle["summary"]["final_X"],
                "days_in_position": sum(bool(d["position"]) for d in runs[1.0]["daily"]),
                "cash_shortfall_rows": summary["cash_shortfall_rows"],
            }
        )
    ok = [r for r in rows if r["status"] == "ok"]
    return {
        "schema_version": "pair-audit.v2",
        "method": "mean_reversion defaults, one contract per leg, next-open fill, published taker tariff; X = free-cash-in-LQDT wealth minus all-LQDT wealth",
        "pairs": rows,
        "summary": {
            "audited": len(ok),
            "tradable_every_day": sum(r["tradable_every_day"] for r in ok),
            "engine_matches_independent": sum(r["engine_matches_independent"] for r in ok),
            "positive_final_X_slippage_1": sum(r["final_X_rub_by_slippage"]["1.0"] > 0 for r in ok),
            "positive_final_X_slippage_1_tradable": sum(
                r["final_X_rub_by_slippage"]["1.0"] > 0 for r in ok if r["tradable_every_day"]
            ),
        },
    }
