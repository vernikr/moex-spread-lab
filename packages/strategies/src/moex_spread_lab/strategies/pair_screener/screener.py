"""Offline screening of documented MOEX futures-spread candidates."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from typing import Any

from moex_spread_lab.accounting.account_risk import assess_vtb_pair
from moex_spread_lab.core.universe import SCREEN_PAIRS
from moex_spread_lab.core.w0 import W0Error

from ..pair_mean_reversion.analytics import get_cost_scenario, run_mean_reversion_baseline
from .normalization import ScreenError, _bar_series, _normalized_series, _read_json, _round

SCREEN_SCHEMA = "pair-screen.v1"


def _basis_daily_pnl(series: dict[str, Any]) -> list[float]:
    rows = series["rows"]
    near = series["contracts"][series["near"]]
    far = series["contracts"][series["far"]]
    near_multiplier = float(near["tick_value"]) / float(near["tick_size"])
    far_multiplier = float(far["tick_value"]) / float(far["tick_size"])
    return [
        (float(cur["far_close"]) - float(prev["far_close"])) * far_multiplier
        - (float(cur["near_close"]) - float(prev["near_close"])) * near_multiplier
        for prev, cur in pairwise(rows)
    ]


def _screen_single_pair(
    pair: dict[str, str],
    candles_dir: Path,
    benchmark_path: Path,
    contracts_path: Path,
    initial_capital: float,
    stress_cost: dict[str, Any],
) -> dict[str, Any]:
    near, far = pair["near"], pair["far"]
    base: dict[str, Any] = {
        **pair,
        "price_role": "1d_candle_close_proxy",
        "candidate_status": "screened",
    }
    try:
        series = _normalized_series(
            candles_dir / f"{near}_1d.json",
            candles_dir / f"{far}_1d.json",
            benchmark_path,
            contracts_path,
            near=near,
            far=far,
        )
        risk = assess_vtb_pair(
            contracts_path,
            near,
            far,
            initial_capital=initial_capital,
            observed_daily_pnl=_basis_daily_pnl(series),
        )
        baseline = run_mean_reversion_baseline(
            series,
            initial_capital=initial_capital,
            analysis_role="diagnostic_only",
            cost_profile=stress_cost,
        )
        summary = baseline["summary"]
        base.update(
            {
                "common_rows": summary["common_row_count"],
                "first_date": summary["first_date"],
                "last_date": summary["last_date"],
                "pair_initial_margin_rub": risk["pair"]["pair_margin_no_offset_rub"],
                "stress_screen_passed": risk["summary"]["all_scenarios_passed"],
                "current_margin_screen_passed": risk["summary"]["current_snapshot_passed"],
                "stress_remaining_capital_rub": risk["summary"][
                    "most_severe_remaining_capital_rub"
                ],
                "h1_control": {
                    "trade_count": summary["trade_count"],
                    "gross_pnl_rub": summary["gross_pnl"],
                    "illustrative_stress_costs_rub": summary["modeled_costs"],
                    "final_X_rub": summary["final_X"],
                    "max_drawdown_rub": summary["max_drawdown_D"],
                },
                "decision": (
                    "candidate_for_separate_hypothesis"
                    if risk["summary"]["all_scenarios_passed"] and summary["final_X"] > 0
                    else "not_promoted"
                ),
                "risk": risk,
                "series_provenance": series["provenance"],
            }
        )
    except (OSError, ScreenError, W0Error) as exc:
        base.update(
            {
                "candidate_status": "rejected_input_or_capital_gate",
                "decision": "not_screenable",
                "error": str(exc),
            }
        )
    return base


def screen_pairs(
    candles_dir: Path,
    benchmark_path: Path,
    contracts_path: Path,
    *,
    initial_capital: float = 30_000.0,
    pairs: tuple[dict[str, str], ...] = SCREEN_PAIRS,
) -> dict[str, Any]:
    """Run the same analytical control and account stress screen for each pair."""
    if initial_capital <= 0:
        raise ScreenError("initial capital must be positive")
    contracts, contracts_hash = _read_json(contracts_path)
    if contracts.get("schema_version") != "iss-intake.v1":
        raise ScreenError("screen requires iss-intake.v1 contracts")
    benchmark, benchmark_hash = _read_json(benchmark_path)
    if benchmark.get("schema_version") != "lqdt-lineage.v1":
        raise ScreenError("screen requires lqdt-lineage.v1 benchmark")
    stress_cost = get_cost_scenario("stress")
    candidates = [
        _screen_single_pair(
            p, candles_dir, benchmark_path, contracts_path, initial_capital, stress_cost
        )
        for p in pairs
    ]
    candidates.sort(
        key=lambda item: (
            item.get("decision") != "candidate_for_separate_hypothesis",
            -(item.get("h1_control", {}).get("final_X_rub") or float("-inf")),
        )
    )
    for index, candidate in enumerate(candidates, start=1):
        candidate["rank"] = index
    return {
        "schema_version": SCREEN_SCHEMA,
        "release_state": "exploratory_inconclusive",
        "screen_id": "documented_flagship_pairs_h1_control",
        "method": {
            "hypothesis": "fixed_mean_reversion_control_only",
            "lookback": 20,
            "entry_z": 2.0,
            "exit_z": 0.5,
            "decision_delay_bars": 1,
            "cost_profile": "illustrative_stress_unverified",
            "risk_profile": "vtb_ebs_kpur_working_profile",
            "price_proxy": "daily_candle_close",
            "historical_order_book": False,
        },
        "summary": {
            "candidate_count": len(candidates),
            "stress_pass_count": sum(
                item.get("stress_screen_passed", False) for item in candidates
            ),
            "positive_h1_control_count": sum(
                (item.get("h1_control", {}).get("final_X_rub") or 0) > 0 for item in candidates
            ),
            "separate_hypothesis_candidates": sum(
                item.get("decision") == "candidate_for_separate_hypothesis" for item in candidates
            ),
        },
        "candidates": candidates,
        "provenance": {
            "contracts_path": str(contracts_path),
            "contracts_sha256": contracts_hash,
            "benchmark_path": str(benchmark_path),
            "benchmark_sha256": benchmark_hash,
            "candidate_source": "R06_documented_flagship_same_asset_pairs",
        },
    }


__all__ = [
    "SCREEN_SCHEMA",
    "ScreenError",
    "_bar_series",
    "_basis_daily_pnl",
    "_normalized_series",
    "_read_json",
    "_round",
    "screen_pairs",
]
