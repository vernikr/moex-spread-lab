"""Independent re-computation and data/benchmark audit of the price-only screener control."""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any

from .analytics import get_cost_scenario, run_mean_reversion_baseline
from .screener import _normalized_series
from .screener_app import PAIRS, ScreenerInputs
from .w0 import W0Error

THIN_VOLUME = 100
INITIAL_CAPITAL = 30_000.0


def _candles(path: Path) -> dict[str, dict[str, Any]]:
    return {row["begin"]: row for row in json.loads(path.read_text(encoding="utf-8"))["rows"]}


def independent_gross(near: dict, far: dict, near_mult: float, far_mult: float, *, lookback: int = 20,
                      entry_z: float = 2.0, exit_z: float = 0.5, delay: int = 1) -> tuple[float, int]:
    """Re-implement the zero-cost mean-reversion control from raw candle closes."""
    stamps = sorted(set(near) & set(far))
    spread = [near[t]["close"] * near_mult - far[t]["close"] * far_mult for t in stamps]
    position, entry, gross, trades = 0, 0.0, 0.0, 0
    for index, value in enumerate(spread):
        signal = index - delay
        if signal < lookback:
            continue
        window = spread[signal - lookback:signal]
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
    contracts = {row["secid"]: row for row in json.loads(inputs.contracts.read_text(encoding="utf-8"))["instruments"]}
    rows = []
    for pair in PAIRS:
        near_id, far_id = pair["near"], pair["far"]
        near_path, far_path = inputs.candles(pair["pair_id"], near_id), inputs.candles(pair["pair_id"], far_id)
        if not (near_path.is_file() and far_path.is_file()):
            continue
        near, far = _candles(near_path), _candles(far_path)
        mult = {s: contracts[s]["tick_value"] / contracts[s]["tick_size"] for s in (near_id, far_id)}
        item: dict[str, Any] = {
            "pair": pair["pair_id"],
            "rows": len(set(near) & set(far)),
            "far_thin_day_share": round(sum(r["volume"] < THIN_VOLUME for r in far.values()) / len(far), 3),
            "near_thin_day_share": round(sum(r["volume"] < THIN_VOLUME for r in near.values()) / len(near), 3),
            "weekend_rows": sum(date.fromisoformat(t[:10]).weekday() >= 5 for t in set(near) & set(far)),
            "close_outside_range": sum(not r["low"] <= r["close"] <= r["high"] for r in (*near.values(), *far.values())),
        }
        try:
            series = _normalized_series(near_path, far_path, inputs.benchmark, inputs.contracts, near=near_id, far=far_id)
            runs = {name: run_mean_reversion_baseline(series, analysis_role="diagnostic_only", cost_profile=get_cost_scenario(name))
                    for name in ("stress", "low")}
        except W0Error as exc:
            rows.append({**item, "status": "blocked", "reason": str(exc)})
            continue
        stress = runs["stress"]
        summary, daily = stress["summary"], stress["daily"]
        gross, trades = independent_gross(near, far, mult[near_id], mult[far_id])
        margin = stress["parameters"]["margin_per_pair"]
        benchmark = [d["benchmark_value"] for d in daily]
        lqdt_on_margin = sum(
            (benchmark[i] / benchmark[i - 1] - 1) * margin for i in range(1, len(daily)) if daily[i - 1]["position"]
        )
        rows.append({
            **item,
            "status": "ok",
            "trades": summary["trade_count"],
            "engine_gross_rub": summary["gross_pnl"],
            "independent_gross_rub": round(gross, 2),
            "independent_trades": trades,
            "engine_matches_independent": abs(gross - summary["gross_pnl"]) < 1e-6 and trades == summary["trade_count"],
            "costs_stress_rub": summary["modeled_costs"],
            "costs_low_rub": runs["low"]["summary"]["modeled_costs"],
            "lqdt_full_capital_hurdle_rub": round(summary["final_benchmark_value"] - INITIAL_CAPITAL, 2),
            "final_X_rub": summary["final_X"],
            "margin_rub": margin,
            "days_in_position": sum(bool(d["position"]) for d in daily),
            "lqdt_on_margin_while_open_rub": round(lqdt_on_margin, 2),
            "overlay_excess_stress_rub": round(summary["gross_pnl"] - summary["modeled_costs"] - lqdt_on_margin, 2),
            "overlay_excess_zero_cost_rub": round(summary["gross_pnl"] - lqdt_on_margin, 2),
        })
    ok = [r for r in rows if r["status"] == "ok"]
    return {
        "schema_version": "pair-audit.v1",
        "method": "mean_reversion_default_parameters; overlay = net P&L minus LQDT return on pair margin while a position is open",
        "pairs": rows,
        "summary": {
            "audited": len(ok),
            "engine_matches_independent": sum(r["engine_matches_independent"] for r in ok),
            "positive_final_X": sum(r["final_X_rub"] > 0 for r in ok),
            "positive_overlay_excess_stress": sum(r["overlay_excess_stress_rub"] > 0 for r in ok),
            "positive_overlay_excess_zero_cost": sum(r["overlay_excess_zero_cost_rub"] > 0 for r in ok),
        },
    }
