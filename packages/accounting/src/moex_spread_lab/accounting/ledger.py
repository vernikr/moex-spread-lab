"""W4/W5 analytical execution attempts and rebuildable portfolio projection."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from moex_spread_lab.core.w0 import W0Error

from .run_artifacts import load_report

LEDGER_SCHEMA = "execution-ledger.v1"


class LedgerError(W0Error):
    """Raised when an analytical execution ledger cannot be rebuilt safely."""


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) < 10:
        raise LedgerError(f"{field} must contain an ISO date or timestamp")
    candidate = value if len(value) >= 19 else f"{value}T00:00:00"
    try:
        datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerError(f"{field} is not an ISO timestamp: {value}") from exc
    return value


def _round(value: float) -> float:
    return round(float(value), 8)


def _event(event_id: str, event_time: str, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_time": event_time,
        "kind": kind,
        "payload": payload,
    }


def _positions(row: dict[str, Any], near: str, far: str, quantity: int) -> dict[str, int]:
    position = int(row.get("position", 0))
    return {near: position * quantity, far: -position * quantity}


def _attempts(baseline: dict[str, Any]) -> list[dict[str, Any]]:
    rows = baseline.get("daily")
    if not isinstance(rows, list) or not rows:
        raise LedgerError("baseline daily projection is required")
    row_by_time = {row.get("timestamp", row.get("date")): row for row in rows}
    timestamps = list(row_by_time)
    timestamp_index = {timestamp: index for index, timestamp in enumerate(timestamps)}
    delay = int(baseline.get("parameters", {}).get("decision_delay_bars", 1))
    if delay < 1:
        raise LedgerError("decision delay must be at least one bar")
    attempts = []
    for number, trade in enumerate(baseline.get("trades", []), start=1):
        for lifecycle, field in (("entry", "entry_date"), ("exit", "exit_date")):
            decision_at = trade.get(field)
            if decision_at not in row_by_time:
                raise LedgerError(
                    f"trade {field} is absent from baseline daily rows: {decision_at}"
                )
            decision_index = timestamp_index[decision_at]
            signal_index = max(0, decision_index - delay)
            signal_at = timestamps[signal_index]
            attempts.append(
                {
                    "attempt_id": f"ATT-{number:05d}-{lifecycle}",
                    "lifecycle": lifecycle,
                    "scenario": "analytical_close_proxy",
                    "signal_at": _timestamp(signal_at, "signal_at"),
                    "decision_at": _timestamp(decision_at, "decision_at"),
                    "requested_qty": int(trade.get("quantity", 1)),
                    "filled_near_qty": 0,
                    "filled_far_qty": 0,
                    "outcome": "unresolved_no_executable_quote",
                    "reason_code": "historical_bid_ask_unavailable",
                    "proxy_price_used_for_valuation": True,
                    "execution_evidence_status": baseline.get(
                        "execution_evidence_status", "unknown"
                    ),
                    "direction": trade.get("direction"),
                }
            )
    return attempts


def _validate_daily(rows: list[dict[str, Any]]) -> None:
    previous = None
    for index, row in enumerate(rows):
        timestamp = row.get("timestamp", row.get("date"))
        _timestamp(timestamp, f"baseline daily[{index}].timestamp")
        if previous is not None and timestamp <= previous:
            raise LedgerError("baseline daily rows must be strictly chronological")
        previous = timestamp
        for field in ("strategy_value", "benchmark_value", "X", "H", "D", "position"):
            if field not in row:
                raise LedgerError(f"baseline daily row is missing {field}")


def rebuild_projection(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events or events[0].get("kind") != "run_open":
        raise LedgerError("event stream must start with run_open")
    marks = []
    attempts = []
    previous_time = None
    for index, event in enumerate(events):
        if not isinstance(event, dict) or not event.get("event_id") or not event.get("kind"):
            raise LedgerError(f"malformed event at index {index}")
        event_time = event.get("event_time")
        _timestamp(event_time, f"event {index}.event_time")
        if previous_time is not None and event_time < previous_time:
            raise LedgerError("events must be chronological")
        previous_time = event_time
        if event["kind"] == "execution_attempt":
            attempts.append(event["payload"])
        elif event["kind"] == "portfolio_mark":
            marks.append(event["payload"])
    if not marks:
        raise LedgerError("event stream contains no portfolio marks")
    return {
        "marks": marks,
        "attempts": attempts,
        "event_count": len(events),
    }


def build_ledger(baseline_path: Path) -> dict[str, Any]:
    baseline, baseline_hash = load_report(baseline_path, error=LedgerError)
    if baseline.get("schema_version") != "analytical-baseline.v1":
        raise LedgerError("unsupported analytical baseline schema")
    rows = baseline.get("daily")
    if not isinstance(rows, list) or not rows:
        raise LedgerError("analytical baseline has no daily rows")
    _validate_daily(rows)
    near = baseline.get("near")
    far = baseline.get("far")
    if not isinstance(near, str) or not isinstance(far, str):
        raise LedgerError("baseline contract identities are required")
    parameters = baseline.get("parameters", {})
    economics = baseline.get("economics", {})
    margin = float(economics.get("pair_initial_margin_rub", parameters.get("margin_per_pair", 0)))
    quantity = int(parameters.get("quantity", 1))
    if quantity <= 0:
        raise LedgerError("baseline quantity must be positive")
    if margin <= 0:
        raise LedgerError("positive pair margin is required for the portfolio projection")

    attempts = _attempts(baseline)
    events = [
        _event(
            "EVT-000000",
            rows[0].get("timestamp", rows[0]["date"]),
            "run_open",
            {
                "source": "analytical_baseline",
                "execution_evidence_status": baseline.get("execution_evidence_status"),
                "actual_execution_enabled": False,
            },
        )
    ]
    attempts_by_time: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        attempts_by_time.setdefault(attempt["decision_at"], []).append(attempt)
    sequence = 1
    for row in rows:
        timestamp = row.get("timestamp", row["date"])
        for attempt in attempts_by_time.get(timestamp, []):
            events.append(_event(f"EVT-{sequence:06d}", timestamp, "execution_attempt", attempt))
            sequence += 1
        position = int(row["position"])
        reserved_margin = margin * abs(position)
        strategy_value = float(row["strategy_value"])
        benchmark_value = float(row["benchmark_value"])
        modeled_costs = row.get("modeled_costs")
        realized_gross = float(row.get("gross_realized_pnl", 0))
        unrealized_gross = (
            strategy_value - float(parameters.get("initial_capital", 30000)) - realized_gross
        )
        if modeled_costs is not None:
            unrealized_gross += float(modeled_costs)
        events.append(
            _event(
                f"EVT-{sequence:06d}",
                timestamp,
                "portfolio_mark",
                {
                    "event_time": timestamp,
                    "positions": _positions(row, near, far, quantity),
                    "cash_proxy": _round(strategy_value - reserved_margin),
                    "reserved_margin_proxy": _round(reserved_margin),
                    "variation_margin_proxy": _round(unrealized_gross),
                    "strategy_value_proxy": _round(strategy_value),
                    "V_proxy": _round(strategy_value),
                    "benchmark_value": _round(benchmark_value),
                    "B": _round(benchmark_value),
                    "X": _round(float(row["X"])),
                    "H": _round(float(row["H"])),
                    "D": _round(float(row["D"])),
                    "modeled_costs": modeled_costs,
                    "funding_event": None,
                    "valuation_status": "candle_close_proxy_not_execution_mark",
                },
            )
        )
        sequence += 1
    events.append(
        _event(
            f"EVT-{sequence:06d}",
            rows[-1].get("timestamp", rows[-1]["date"]),
            "run_close",
            {
                "release_state": "exploratory_inconclusive",
            },
        )
    )
    rebuilt = rebuild_projection(events)
    final = rebuilt["marks"][-1]
    result = {
        "schema_version": LEDGER_SCHEMA,
        "release_state": "exploratory_inconclusive",
        "analysis_role": baseline.get("analysis_role", "primary_baseline"),
        "near": near,
        "far": far,
        "timeframe": baseline.get("timeframe"),
        "execution": {
            "actual_execution_enabled": False,
            "execution_evidence_status": baseline.get("execution_evidence_status"),
            "outcome_counts": {"unresolved_no_executable_quote": len(attempts)},
            "nonexecution_reason_counts": {"historical_bid_ask_unavailable": len(attempts)},
            "partial_fills_preserved": True,
            "proxy_valuation_is_not_fill": True,
        },
        "portfolio": {
            "initial_capital": float(parameters.get("initial_capital", 30000)),
            "pair_initial_margin_proxy": _round(margin),
            "costs": baseline.get("costs", {}),
            "lifetime_topup_usage": 0.0,
            "external_cash_flows": 0.0,
            "actual_account_reconciliation": False,
            "attribution_status": "unresolved_account_and_execution_evidence",
        },
        "summary": {
            "event_count": len(events),
            "portfolio_mark_count": len(rebuilt["marks"]),
            "execution_attempt_count": len(attempts),
            "final_strategy_value_proxy": final["strategy_value_proxy"],
            "final_benchmark_value": final["benchmark_value"],
            "final_V_proxy": final["V_proxy"],
            "final_B": final["B"],
            "final_X": final["X"],
            "final_H": final["H"],
            "final_D": final["D"],
            "executable_value_available": False,
        },
        "gates": {
            "W4_delayed_execution": False,
            "W4_nonexecution_reasons_recorded": True,
            "W5_event_replay": True,
            "W5_proxy_ledger_rebuilt": True,
            "W5_actual_account_reconciliation": False,
        },
        "events": events,
        "projection": rebuilt["marks"],
        "input_provenance": {
            "baseline_path": str(baseline_path),
            "baseline_sha256": baseline_hash,
            "baseline_input_provenance": baseline.get("input_provenance", {}),
        },
    }
    return result
