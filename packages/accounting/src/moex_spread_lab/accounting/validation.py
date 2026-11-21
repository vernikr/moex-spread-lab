"""W7 chronological validation and falsification report for analytical baselines."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from moex_spread_lab.core.w0 import W0Error, read_hashed_json
from .run_artifacts import load_report


VALIDATION_SCHEMA = "validation-report.v1"


class ValidationError(W0Error):
    """Raised when a W7 report cannot be built from the supplied artifacts."""


def _round(value: float) -> float:
    return round(float(value), 8)


def _segment(rows: list[dict[str, Any]], start: int, end: int) -> dict[str, Any]:
    selected = rows[start:end]
    if not selected:
        raise ValidationError("validation segment is empty")
    first = selected[0]
    last = selected[-1]
    x_values = [float(row["X"]) for row in selected]
    return {
        "first_timestamp": first.get("timestamp", first.get("date")),
        "last_timestamp": last.get("timestamp", last.get("date")),
        "row_count": len(selected),
        "initial_X": _round(float(first["X"])),
        "final_X": _round(float(last["X"])),
        "change_X": _round(float(last["X"]) - float(first["X"])),
        "min_X": _round(min(x_values)),
        "max_X": _round(max(x_values)),
        "max_D": _round(max(float(row["D"]) for row in selected)),
        "trade_count_attributed_by_exit": sum(
            row.get("action") in {"exit", "forced_exit_end_of_sample"} for row in selected
        ),
    }


def _sign_flip_null(rows: list[dict[str, Any]], initial_capital: float) -> dict[str, Any]:
    """Reverse gross strategy value changes while preserving the benchmark path.

    This is a diagnostic falsification branch, not a probability estimate or p-value.
    """
    baseline_strategy = float(rows[0]["strategy_value"])
    cumulative = 0.0
    null_rows = []
    peak = 0.0
    for index, row in enumerate(rows):
        current_strategy = float(row["strategy_value"])
        previous_strategy = float(rows[index - 1]["strategy_value"]) if index else baseline_strategy
        cumulative += -(current_strategy - previous_strategy)
        null_strategy = initial_capital + cumulative
        null_x = null_strategy - float(row["benchmark_value"])
        peak = max(peak, null_x)
        null_rows.append({"X": null_x, "H": peak, "D": peak - null_x})
    return {
        "method": "sign_flip_gross_strategy_value_changes",
        "interpretation": "diagnostic_only_no_probability_claim",
        "initial_X": _round(null_rows[0]["X"]),
        "final_X": _round(null_rows[-1]["X"]),
        "mean_X": _round(sum(row["X"] for row in null_rows) / len(null_rows)),
        "max_D": _round(max(row["D"] for row in null_rows)),
        "outperformed_observed_final_X": null_rows[-1]["X"] > float(rows[-1]["X"]),
    }


def _trade_concentration(baseline: dict[str, Any]) -> dict[str, Any]:
    trades = baseline.get("trades", [])
    gross = [abs(float(trade.get("gross_pnl", 0))) for trade in trades]
    total = sum(gross)
    largest = max(gross, default=0.0)
    return {
        "trade_count": len(trades),
        "absolute_gross_pnl_total": _round(total),
        "largest_absolute_trade_gross_pnl": _round(largest),
        "largest_trade_share": _round(largest / total) if total else None,
        "concentration_flag": bool(total and largest / total > 0.5),
    }


def _sensitivity(path: Path) -> dict[str, Any]:
    report, digest = load_report(path, error=ValidationError)
    if report.get("schema_version") != "analytical-baseline.v1":
        raise ValidationError(f"sensitivity is not analytical-baseline.v1: {path}")
    summary = report.get("summary", {})
    return {
        "path": str(path),
        "sha256": digest,
        "timeframe": report.get("timeframe"),
        "delay_bars": report.get("parameters", {}).get("decision_delay_bars"),
        "cost_status": report.get("costs", {}).get("status"),
        "cost_scenario": report.get("costs", {}).get("profile", {}).get("scenario")
        if isinstance(report.get("costs", {}).get("profile"), dict)
        else None,
        "analysis_role": report.get("analysis_role"),
        "trade_count": summary.get("trade_count"),
        "gross_pnl": summary.get("gross_pnl"),
        "modeled_costs": summary.get("modeled_costs"),
        "final_X": summary.get("final_X"),
        "release_state": report.get("release_state"),
    }


def build_validation(
    baseline_path: Path,
    ledger_path: Path,
    sensitivity_paths: list[Path] | None = None,
    *,
    train_fraction: float = 0.6,
    confirmation_fraction: float = 0.2,
) -> dict[str, Any]:
    baseline, baseline_hash = load_report(baseline_path, error=ValidationError)
    ledger, ledger_hash = read_hashed_json(ledger_path, error=ValidationError)
    if baseline.get("schema_version") != "analytical-baseline.v1":
        raise ValidationError("unsupported baseline schema")
    if ledger.get("schema_version") != "execution-ledger.v1":
        raise ValidationError("unsupported ledger schema")
    if not 0.5 <= train_fraction < 1.0:
        raise ValidationError("train_fraction must be at least 0.5 and below 1")
    if not 0 < confirmation_fraction < 0.5:
        raise ValidationError("confirmation_fraction must be between 0 and 0.5")
    rows = baseline.get("daily")
    if not isinstance(rows, list) or len(rows) < 10:
        raise ValidationError("at least ten chronological baseline rows are required")
    if ledger.get("summary", {}).get("final_X") != baseline.get("summary", {}).get("final_X"):
        raise ValidationError("ledger and baseline final X do not match")
    n = len(rows)
    train_end = max(2, int(n * train_fraction))
    confirmation_start = min(n - 2, max(train_end + 1, int(n * (1 - confirmation_fraction))))
    if train_end >= confirmation_start:
        raise ValidationError("split leaves no validation interval")
    splits = {
        "train": _segment(rows, 0, train_end),
        "validation": _segment(rows, train_end, confirmation_start),
        "sealed_confirmation": _segment(rows, confirmation_start, n),
    }
    blockers = []
    if ledger.get("summary", {}).get("executable_value_available") is not True:
        blockers.append("historical executable quotes and fills are unavailable")
    if baseline.get("costs", {}).get("status") == "unknown":
        blockers.append("required trading and funding costs are unknown")
    elif baseline.get("costs", {}).get("status") != "verified":
        blockers.append("cost profile is illustrative or otherwise unverified")
    if baseline.get("economics", {}).get("contract_terms_status") != "historical_evidence":
        blockers.append("dated historical contract terms are not evidenced")
    if baseline.get("execution_evidence_status") != "historical_bid_ask_available":
        blockers.append("execution evidence status is not historical bid/ask available")
    if any(row.get("benchmark_source_kind") != "observed" for row in rows):
        blockers.append("benchmark includes a proxy period")
    if ledger.get("portfolio", {}).get("actual_account_reconciliation") is not True:
        blockers.append("actual account and collateral reconciliation is unavailable")
    sensitivity = [_sensitivity(path) for path in (sensitivity_paths or [])]
    result = {
        "schema_version": VALIDATION_SCHEMA,
        "release_state": "inconclusive",
        "verdict": "inconclusive_no_pass",
        "near": baseline.get("near"),
        "far": baseline.get("far"),
        "timeframe": baseline.get("timeframe"),
        "method": {
            "chronological_split": True,
            "train_fraction": train_fraction,
            "confirmation_fraction": confirmation_fraction,
            "sealed_confirmation_is_tuned": False,
            "parameter_selection_performed": False,
            "selection_adjustment": "not_applicable_fixed_preregistered_baseline",
        },
        "splits": splits,
        "sign_flip_null": _sign_flip_null(rows, float(baseline.get("parameters", {}).get("initial_capital", 30000))),
        "trade_concentration": _trade_concentration(baseline),
        "sensitivity": sensitivity,
        "gates": {
            "chronological_split": True,
            "sealed_confirmation": True,
            "sign_flip_diagnostic": True,
            "delay_cost_sensitivity": bool(sensitivity),
            "executable_fills": False,
            "verified_costs": baseline.get("costs", {}).get("status") == "verified",
            "historical_contract_terms": baseline.get("economics", {}).get("contract_terms_status") == "historical_evidence",
            "account_reconciliation": ledger.get("portfolio", {}).get("actual_account_reconciliation") is True,
            "pass_eligible": False,
        },
        "conclusion_blockers": blockers,
        "no_profitability_claim": True,
        "input_provenance": {
            "baseline_path": str(baseline_path),
            "baseline_sha256": baseline_hash,
            "ledger_path": str(ledger_path),
            "ledger_sha256": ledger_hash,
        },
    }
    return result
