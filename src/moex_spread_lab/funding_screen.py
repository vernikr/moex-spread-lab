"""Offline funding-aware screening for documented perpetual-to-quarterly pairs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .carry import CarryError, build_carry_strategy, build_carry_study, fetch_history
from .w0 import W0Error


FUNDING_SCREEN_SCHEMA = "funding-screen.v1"
FUNDING_SCREEN_PAIRS = (
    {"pair_id": "GLDRUBF/GLZ6", "near": "GLDRUBF", "far": "GLZ6"},
    {"pair_id": "RGBIF/RBZ6", "near": "RGBIF", "far": "RBZ6"},
    {"pair_id": "GAZPF/GZZ6", "near": "GAZPF", "far": "GZZ6"},
    {"pair_id": "IMOEXF/MMZ6", "near": "IMOEXF", "far": "MMZ6"},
)


class FundingScreenError(W0Error):
    """Raised when the funding screen cannot be completed."""


def _source_dates(source: dict[str, Any]) -> tuple[str, str]:
    rows = source.get("rows", [])
    dates = sorted(row["TRADEDATE"] for row in rows if row.get("TRADEDATE"))
    if not dates:
        raise FundingScreenError(f"empty history source for {source.get('secid')}")
    return dates[0], dates[-1]


def screen_funding_pairs(
    *,
    contracts_path: Path,
    benchmark_path: Path,
    cache_dir: Path,
    from_date: str,
    till_date: str,
    initial_capital: float = 30_000.0,
    cost_scenario: str = "stress",
    delay_seconds: float = 0.2,
    timeout: float = 60.0,
    pairs: tuple[dict[str, str], ...] = FUNDING_SCREEN_PAIRS,
) -> dict[str, Any]:
    """Evaluate one frozen funding-only rule independently for each pair."""
    if initial_capital <= 0 or delay_seconds < 0 or timeout <= 0:
        raise FundingScreenError("invalid funding screen parameters")
    candidates: list[dict[str, Any]] = []
    for pair in pairs:
        try:
            near_source = fetch_history(
                pair["near"], from_date, till_date,
                cache_dir=cache_dir, delay_seconds=delay_seconds, timeout=timeout,
            )
            far_source = fetch_history(
                pair["far"], from_date, till_date,
                cache_dir=cache_dir, delay_seconds=delay_seconds, timeout=timeout,
            )
            study = build_carry_study(
                near_source,
                far_source,
                contracts_path,
                near=pair["near"],
                far=pair["far"],
                funding_regimes=(),
            )
            strategy = build_carry_strategy(
                study,
                benchmark_path,
                contracts_path,
                transaction_cost_scenario=cost_scenario,
                initial_capital=initial_capital,
                strategy_variant="funding_only",
            )
            study_summary = study["summary"]
            strategy_summary = strategy["summary"]
            risk = strategy["account_risk"]
            candidates.append({
                **pair,
                "status": "evaluated",
                "price_role": "EOD_SETTLEPRICE_proxy",
                "funding_role": "near_SWAPRATE_received_by_short_near",
                "history_window": {
                    "near": _source_dates(near_source),
                    "far": _source_dates(far_source),
                },
                "common_row_count": study_summary["common_row_count"],
                "funding": {
                    "total_received_rub": study_summary["total_funding_received"],
                    "mean_daily_received_rub": study_summary["mean_daily_funding_received"],
                    "positive_rows": study_summary["positive_funding_rows"],
                    "negative_rows": study_summary["negative_funding_rows"],
                    "zero_rows": study_summary["zero_funding_rows"],
                    "cap_check": "not_modelled_for_non_CNY_pairs",
                },
                "gross_decomposition": {
                    "basis_pnl_rub": study_summary["total_basis_pnl"],
                    "funding_received_rub": study_summary["total_funding_received"],
                    "gross_carry_pnl_rub": study_summary["total_gross_carry_pnl"],
                },
                "funding_only_hypothesis": {
                    "rule_id": f"H3-F1/{pair['pair_id']}",
                    "entry": "mean_previous_20_daily_funding_received > 0",
                    "exit": [
                        "mean_previous_20_daily_funding_received <= 0",
                        "date >= far_expiry - 5 calendar days",
                        "perpetual_execution_window",
                    ],
                    "decision_delay_observations": 1,
                    "cost_scenario": cost_scenario,
                    "closed_trade_count": strategy_summary["closed_trade_count"],
                    "open_trade_count": strategy_summary["open_trade_count"],
                    "gross_pnl_rub": strategy_summary["gross_pnl"],
                    "transaction_costs_rub": strategy_summary["transaction_costs"],
                    "final_X_rub": strategy_summary["final_X"],
                    "max_drawdown_rub": round(max(float(row["D"]) for row in strategy["daily"]), 8),
                    "assignment_window_status": strategy["parameters"]["assignment_window_status"],
                },
                "risk": {
                    "pair_margin_rub": risk["pair"]["pair_margin_no_offset_rub"],
                    "current_snapshot_passed": risk["summary"]["current_snapshot_passed"],
                    "all_working_scenarios_passed": risk["summary"]["all_scenarios_passed"],
                    "most_severe_remaining_capital_rub": risk["summary"]["most_severe_remaining_capital_rub"],
                },
                "decision": (
                    "candidate_for_separate_hypothesis"
                    if strategy_summary["final_X"] > 0 and risk["summary"]["all_scenarios_passed"]
                    else "not_promoted"
                ),
                "provenance": {
                    "near_history": near_source.get("source"),
                    "far_history": far_source.get("source"),
                    "study_schema": study["schema_version"],
                    "strategy_schema": strategy["schema_version"],
                },
            })
        except (CarryError, FundingScreenError, OSError, ValueError) as exc:
            candidates.append({
                **pair,
                "status": "not_screenable",
                "decision": "not_screenable",
                "error": str(exc),
            })
    candidates.sort(key=lambda item: (
        item.get("decision") != "candidate_for_separate_hypothesis",
        -(item.get("funding_only_hypothesis", {}).get("final_X_rub") or float("-inf")),
    ))
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank
    return {
        "schema_version": FUNDING_SCREEN_SCHEMA,
        "release_state": "exploratory_inconclusive",
        "screen_id": "flagship_perpetual_quarterly_funding_only",
        "method": {
            "hypothesis": "H3-F1_funding_only_applied_separately",
            "lookback_observations": 20,
            "decision_delay_observations": 1,
            "quantity_per_leg": 1,
            "price_proxy": "EOD_SETTLEPRICE",
            "funding_field": "near_SWAPRATE_per_contract_day",
            "funding_sign": "positive_near_swaprate_received_by_short_near",
            "cost_scenario": cost_scenario,
            "historical_executable_fills": False,
            "historical_order_book": False,
        },
        "summary": {
            "candidate_count": len(candidates),
            "evaluated_count": sum(item.get("status") == "evaluated" for item in candidates),
            "stress_pass_count": sum(
                item.get("risk", {}).get("all_working_scenarios_passed", False) for item in candidates
            ),
            "positive_final_X_count": sum(
                (item.get("funding_only_hypothesis", {}).get("final_X_rub") or 0) > 0 for item in candidates
            ),
            "separate_hypothesis_candidates": sum(
                item.get("decision") == "candidate_for_separate_hypothesis" for item in candidates
            ),
        },
        "candidates": candidates,
        "provenance": {
            "contracts_path": str(contracts_path),
            "benchmark_path": str(benchmark_path),
            "history_endpoint": "MOEX_ISS_futures_history",
            "funding_unit_basis": "R06_and_ISS_history_SWAPRATE_times_near_LOTVOLUME",
            "source_window": {"from": from_date, "till": till_date},
        },
    }
