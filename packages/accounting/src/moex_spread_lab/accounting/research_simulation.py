"""Synthetic research W3-W5 economics, execution, and portfolio waves."""

from __future__ import annotations

from typing import Any

from .research_intake import ResearchError
from .research_intake import positive_number as _positive_number
from .research_intake import require as _require
from .research_intake import timestamp as _timestamp

_COST_COMPONENTS = ("exchange", "clearing", "broker", "slippage", "funding", "exit")
_EXECUTION_FIELDS = (
    "session_open",
    "quote_age_seconds",
    "max_quote_age_seconds",
    "requested_qty",
    "filled_near_qty",
    "filled_far_qty",
    "cost_status",
    "capital_status",
    "margin_status",
)


def _validate_contract_terms(contracts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contract_ids: set[str] = set()
    cost_profiles: list[dict[str, Any]] = []
    for contract in contracts:
        contract_id = str(_require(contract, "stable_id", "contract terms"))
        if contract_id in contract_ids:
            raise ResearchError(f"duplicate contract terms: {contract_id}")
        contract_ids.add(contract_id)
        for field in (
            "lot_size",
            "tick_size",
            "multiplier",
            "settlement_type",
            "price_unit",
            "margin_requirement",
        ):
            _require(contract, field, f"contract terms {contract_id}")
        if not isinstance(contract["lot_size"], int) or contract["lot_size"] <= 0:
            raise ResearchError(f"contract lot must be a positive integer: {contract_id}")
        for field in ("tick_size", "multiplier", "margin_requirement"):
            _positive_number(contract[field], f"{field} for {contract_id}")
        _timestamp(contract["expiry"], f"contract expiry for {contract_id}")
        cost_profiles.append(_require(contract, "cost_profile", f"contract terms {contract_id}"))
    return cost_profiles


def _validate_spread_rows(spreads: list[dict[str, Any]]) -> None:
    if not spreads:
        raise ResearchError("spread series is empty")
    for row in spreads:
        if _require(row, "direction", "spread row") != "near_minus_far":
            raise ResearchError("unsupported spread direction")
        _timestamp(row.get("event_time"), "spread event_time")
        near = float(_require(row, "near_settlement", "spread row"))
        far = float(_require(row, "far_settlement", "spread row"))
        if abs(float(row["spread_value"]) - (near - far)) > 1e-9:
            raise ResearchError("spread sign or value mismatch")
        if not isinstance(row.get("quality_status"), str) or row["quality_status"] == "missing":
            raise ResearchError("spread row has unacceptable quality status")


def run_w3_economics(fixture: dict[str, Any]) -> dict[str, Any]:
    contracts = fixture["contracts"]
    cost_profiles = _validate_contract_terms(contracts)
    if any(
        profile.get(component) is None
        for profile in cost_profiles
        for component in _COST_COMPONENTS
    ):
        raise ResearchError("unknown cost component cannot become zero")
    total_cost = sum(
        float(profile[component]) for profile in cost_profiles for component in _COST_COMPONENTS
    )
    spreads = fixture["spread_series"]
    _validate_spread_rows(spreads)
    return {
        "wave": "W3",
        "status": "passed",
        "contract_count": len(contracts),
        "spread_row_count": len(spreads),
        "direction": "near_minus_far",
        "known_cost_components": list(_COST_COMPONENTS),
        "cost_per_leg": round(total_cost / len(cost_profiles), 8),
        "cost_per_pair": round(total_cost, 8),
        "unknown_cost_is_zero": False,
    }


def _validate_execution_attempt(attempt: dict[str, Any]) -> tuple[str, int]:
    attempt_id = str(_require(attempt, "attempt_id", "execution attempt"))
    signal_at = _timestamp(
        _require(attempt, "signal_at", f"execution {attempt_id}"), f"signal_at {attempt_id}"
    )
    decision_at = _timestamp(
        _require(attempt, "decision_at", f"execution {attempt_id}"), f"decision_at {attempt_id}"
    )
    if decision_at < signal_at:
        raise ResearchError(f"decision precedes signal: {attempt_id}")
    if attempt.get("midpoint") is not None:
        raise ResearchError(f"midpoint cannot be a fill price: {attempt_id}")
    for field in _EXECUTION_FIELDS:
        _require(attempt, field, f"execution {attempt_id}")
    requested = attempt["requested_qty"]
    if not isinstance(requested, int) or requested <= 0:
        raise ResearchError(f"requested quantity must be a positive integer: {attempt_id}")
    for field in ("filled_near_qty", "filled_far_qty"):
        if not isinstance(attempt[field], int) or not 0 <= attempt[field] <= requested:
            raise ResearchError(
                f"{field} must be an integer between zero and requested quantity: {attempt_id}"
            )
    if (
        not isinstance(attempt["quote_age_seconds"], (int, float))
        or attempt["quote_age_seconds"] < 0
    ):
        raise ResearchError(f"quote age must be non-negative: {attempt_id}")
    return attempt_id, requested


def _classify_attempt_outcome(attempt: dict[str, Any], requested: int) -> str:
    if attempt["session_open"] is not True:
        return "rejected_session"
    if attempt["quote_age_seconds"] > attempt["max_quote_age_seconds"]:
        return "rejected_stale"
    if (
        attempt["cost_status"] != "known"
        or attempt["capital_status"] != "passed"
        or attempt["margin_status"] != "passed"
    ):
        return "rejected_gate"
    if attempt["filled_near_qty"] == requested and attempt["filled_far_qty"] == requested:
        return "filled_balanced"
    if attempt["filled_near_qty"] or attempt["filled_far_qty"]:
        return "partial_unbalanced"
    return "unfilled_no_quote"


def run_w4_execution(fixture: dict[str, Any]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for attempt in fixture["execution"]["attempts"]:
        attempt_id, requested = _validate_execution_attempt(attempt)
        outcome = _classify_attempt_outcome(attempt, requested)
        results.append(
            {
                "attempt_id": attempt_id,
                "lifecycle": attempt["lifecycle"],
                "scenario": attempt["scenario"],
                "outcome": outcome,
            }
        )
        counts[outcome] = counts.get(outcome, 0) + 1
    if not {"filled_balanced", "rejected_stale", "partial_unbalanced"}.issubset(counts):
        raise ResearchError("execution fixture must exercise balanced, stale and partial paths")
    return {
        "wave": "W4",
        "status": "passed",
        "attempts": results,
        "outcome_counts": counts,
        "partial_fills_visible": True,
        "delayed_execution_only": True,
    }


def _compute_portfolio_marks(
    marks: list[dict[str, Any]], initial: float, first_index: float
) -> list[dict[str, Any]]:
    cumulative_pnl = 0.0
    cumulative_costs = 0.0
    cumulative_external = 0.0
    peak = 0.0
    rows: list[dict[str, Any]] = []
    for mark in marks:
        _timestamp(_require(mark, "event_time", "portfolio mark"), "portfolio event_time")
        cumulative_pnl += float(_require(mark, "strategy_gross_pnl", "portfolio mark"))
        cumulative_costs += float(_require(mark, "strategy_costs", "portfolio mark"))
        cumulative_external += float(_require(mark, "external_cash_flow", "portfolio mark"))
        benchmark = initial * float(mark["lqdt_index"]) / first_index + cumulative_external
        strategy = initial + cumulative_pnl - cumulative_costs + cumulative_external
        excess = strategy - benchmark
        peak = max(peak, excess)
        rows.append(
            {
                "event_time": mark["event_time"],
                "strategy_value": round(strategy, 8),
                "benchmark_value": round(benchmark, 8),
                "X": round(excess, 8),
                "H": round(peak, 8),
                "D": round(peak - excess, 8),
                "cumulative_costs": round(cumulative_costs, 8),
            }
        )
    if rows[0]["X"] != 0:
        raise ResearchError("X(0) must equal zero")
    return rows


def _summarize_execution_events(attempts: list[dict[str, Any]]) -> tuple[list[str], int, int]:
    balanced_position = 0
    unbalanced_attempts = 0
    full_fills: list[dict[str, Any]] = []
    for item in attempts:
        if item["outcome"] == "partial_unbalanced":
            unbalanced_attempts += 1
        elif item["outcome"] == "filled_balanced":
            full_fills.append(item)
            if item["lifecycle"] == "entry":
                balanced_position += 1
            elif item["lifecycle"] == "exit":
                balanced_position -= 1
    event_types = ["portfolio_open"]
    event_types.extend(f"execution_attempt:{item['outcome']}" for item in attempts)
    event_types.extend(str(item["lifecycle"]) for item in full_fills)
    event_types.append("portfolio_marks")
    return event_types, balanced_position, unbalanced_attempts


def run_w5_portfolio(fixture: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    portfolio = fixture["portfolio"]
    initial = float(_require(portfolio, "initial_capital", "portfolio"))
    if initial != 30000:
        raise ResearchError("fixture must use the delegated RUB 30,000 initial envelope")
    marks = _require(portfolio, "marks", "portfolio")
    if not marks:
        raise ResearchError("portfolio marks are empty")
    first_index = float(_require(marks[0], "lqdt_index", "portfolio mark"))
    if first_index <= 0:
        raise ResearchError("initial LQDT index must be positive")
    rows = _compute_portfolio_marks(marks, initial, first_index)
    additional_funding = sum(float(mark.get("additional_funding", 0)) for mark in marks)
    if additional_funding < 0 or additional_funding > 15000:
        raise ResearchError("lifetime top-up usage violates the provisional policy")
    if any(float(mark.get("core_transfer", 0)) != 0 for mark in marks):
        raise ResearchError("synthetic W5 fixture must not hide internal LQDT transfers")
    event_types, balanced_position, unbalanced_attempts = _summarize_execution_events(
        execution["attempts"]
    )
    return {
        "wave": "W5",
        "status": "passed",
        "initial_capital": initial,
        "rows": rows,
        "event_types": event_types,
        "execution_attempts_recorded": len(execution["attempts"]),
        "balanced_position_qty_after_replay": balanced_position,
        "unbalanced_attempts_preserved": unbalanced_attempts,
        "lifetime_topup_usage": round(additional_funding, 8),
        "external_cash_flows_matched": True,
        "actual_lqdt_account_reconciliation": False,
        "attribution_status": "unresolved_synthetic_benchmark",
    }
