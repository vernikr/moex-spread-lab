"""Slimmed execution-ledger.v1 adapter retained while live CLI callers remain (#242)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, cast

from moex_spread_lab.core.w0 import W0Error, read_hashed_json

LEDGER_SCHEMA = "execution-ledger.v1"
_REQUIRED_DAILY = ("strategy_value", "benchmark_value", "X", "H", "D", "position")


class LedgerError(W0Error):
    pass


def _ts(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) < 10:
        raise LedgerError(f"{field} must contain an ISO date or timestamp")
    try:
        datetime.fromisoformat(
            (value if len(value) >= 19 else f"{value}T00:00:00").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise LedgerError(f"{field} is not an ISO timestamp: {value}") from exc
    return value


def _daily_timestamps(rows: list[dict[str, Any]]) -> list[str]:
    timestamps: list[str] = []
    previous: str | None = None
    for idx, row in enumerate(rows):
        ts = _ts(row.get("timestamp", row.get("date")), f"baseline daily[{idx}].timestamp")
        if previous is not None and ts <= previous:
            raise LedgerError("baseline daily rows must be strictly chronological")
        for field in _REQUIRED_DAILY:
            if field not in row:
                raise LedgerError(f"baseline daily row is missing {field}")
        previous = ts
        timestamps.append(ts)
    return timestamps


def _attempts(baseline: dict[str, Any], timestamps: list[str]) -> list[tuple[str, str]]:
    delay = int(baseline.get("parameters", {}).get("decision_delay_bars", 1))
    if delay < 1:
        raise LedgerError("decision delay must be at least one bar")
    ts_index = {ts: idx for idx, ts in enumerate(timestamps)}
    attempts: list[tuple[str, str]] = []
    for trade in baseline.get("trades", []):
        for field in ("entry_date", "exit_date"):
            decision_at = trade.get(field)
            if decision_at not in ts_index:
                raise LedgerError(f"trade {field} is absent from daily rows: {decision_at}")
            attempts.append((timestamps[max(0, ts_index[decision_at] - delay)], str(decision_at)))
    return attempts


def build_ledger(baseline_path: Path) -> dict[str, Any]:
    baseline, baseline_hash = read_hashed_json(
        baseline_path, error=LedgerError, label="analytical report"
    )
    if baseline.get("schema_version") != "analytical-baseline.v1":
        raise LedgerError("unsupported analytical baseline schema")
    raw_rows = baseline.get("daily")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise LedgerError("analytical baseline has no daily rows")
    rows = cast(list[dict[str, Any]], raw_rows)
    timestamps = _daily_timestamps(rows)
    near, far = baseline.get("near"), baseline.get("far")
    if not isinstance(near, str) or not isinstance(far, str):
        raise LedgerError("baseline contract identities are required")
    params, econ = baseline.get("parameters", {}), baseline.get("economics", {})
    margin = float(econ.get("pair_initial_margin_rub", params.get("margin_per_pair", 0)))
    if int(params.get("quantity", 1)) <= 0 or margin <= 0:
        raise LedgerError("positive quantity and pair margin are required")

    attempts = _attempts(baseline, timestamps)
    marks = [
        {
            "t": ts,
            "p": int(r["position"]),
            "v": round(float(r["strategy_value"]), 8),
            "X": round(float(r["X"]), 8),
        }
        for r, ts in zip(rows, timestamps, strict=True)
    ]
    return {
        "schema_version": LEDGER_SCHEMA,
        "release_state": "exploratory_inconclusive",
        "near": near,
        "far": far,
        "timeframe": baseline.get("timeframe"),
        "execution": {"outcome_counts": {"unresolved_no_executable_quote": len(attempts)}},
        "portfolio": {"actual_account_reconciliation": False},
        "summary": {
            "execution_attempt_count": len(attempts),
            "final_X": marks[-1]["X"],
            "executable_value_available": False,
        },
        "gates": {"W5_event_replay": True},
        "events": attempts,
        "projection": marks,
        "input_provenance": {"baseline_path": str(baseline_path), "baseline_sha256": baseline_hash},
    }
