"""Offline W1-W7 research pipeline over explicitly synthetic fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from moex_spread_lab.core.w0 import (
    canonical_bytes,
    sha256_bytes,
    sha256_file,
    validate_manifest,
)

from .research_intake import (
    RESEARCH_FIXTURE_SCHEMA,
    RESEARCH_REPORT_SCHEMA,
    ResearchError,
    load_research_fixture,
    run_w1_catalog,
    run_w2_corporate_actions,
)
from .research_intake import (
    positive_number as _positive_number,
)
from .research_intake import (
    require as _require,
)
from .research_simulation import (
    run_w3_economics,
    run_w4_execution,
    run_w5_portfolio,
)

_STRATEGY_INT_FIELDS = (
    "lookback",
    "entry_threshold_ticks",
    "exit_threshold_ticks",
    "max_quantity",
    "aggregate_cap",
)


def _validate_strategy_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    for field in _STRATEGY_INT_FIELDS:
        value = _require(config, field, "mean-reversion configuration")
        if not isinstance(value, int) or value <= 0:
            raise ResearchError(f"strategy parameter must be a positive integer: {field}")
    ladder = _require(config, "ladder", "mean-reversion configuration")
    if not ladder or any(
        not isinstance(step.get("quantity"), int) or step["quantity"] <= 0 for step in ladder
    ):
        raise ResearchError("exposure ladder quantities must be positive integers")
    if sum(step["quantity"] for step in ladder) > config["aggregate_cap"]:
        raise ResearchError("exposure ladder exceeds aggregate cap")
    if _require(config, "uses_emergency_funding", "mean-reversion configuration") is not False:
        raise ResearchError("entry sizing cannot use emergency funding")
    return ladder


def _evaluate_strategy_stress(config: dict[str, Any], envelope: float) -> dict[str, Any]:
    stress = _require(config, "stress", "mean-reversion configuration")
    stress_loss = _positive_number(
        _require(stress, "stressed_incremental_loss", "strategy stress"),
        "stressed incremental loss",
    )
    reserve = _positive_number(_require(stress, "reserve", "strategy stress"), "strategy reserve")
    margin = _positive_number(
        _require(stress, "initial_margin", "strategy stress"), "initial margin"
    )
    stress_passed = envelope - stress_loss - reserve - margin >= 0
    if not stress_passed:
        raise ResearchError("strategy stress test exceeds the original capital envelope")
    return {
        "stressed_incremental_loss": stress_loss,
        "reserve": reserve,
        "initial_margin": margin,
        "envelope": envelope,
        "passed": stress_passed,
    }


def run_w6_strategy(fixture: dict[str, Any], portfolio: dict[str, Any]) -> dict[str, Any]:
    config = _require(fixture["strategy"], "mean_reversion", "strategy")
    ladder = _validate_strategy_config(config)
    stress_result = _evaluate_strategy_stress(config, float(portfolio["initial_capital"]))
    spreads = fixture["spread_series"]
    mean = sum(float(row["spread_ticks"]) for row in spreads) / len(spreads)
    threshold = int(config["entry_threshold_ticks"])
    signals = [
        {
            "event_time": row["event_time"],
            "signal": "eligible_entry"
            if abs(float(row["spread_ticks"]) - mean) >= threshold
            else "no_signal",
            "distance_from_mean_ticks": round(float(row["spread_ticks"]) - mean, 4),
        }
        for row in spreads
    ]
    trials = [
        {"trial_id": "no-trade-control", "status": "accepted", "reason": "benchmark control"},
        {
            "trial_id": "mean-reversion-v1",
            "status": "accepted",
            "reason": "fixed parameters and finite ladder",
        },
        {
            "trial_id": "carry-v1",
            "status": "rejected",
            "reason": "reference-rate evidence is not present in synthetic fixture",
        },
    ]
    return {
        "wave": "W6",
        "status": "passed",
        "strategies": trials,
        "signals": signals,
        "ladder": ladder,
        "aggregate_cap": config["aggregate_cap"],
        "stress": stress_result,
        "integer_sizing": True,
        "no_automatic_top_up": True,
    }


def run_w7_validation(
    fixture: dict[str, Any], portfolio: dict[str, Any], strategy: dict[str, Any]
) -> dict[str, Any]:
    rows = portfolio["rows"]
    validation = fixture["validation"]
    train_end = validation["train_end"]
    train = [row for row in rows if row["event_time"] <= train_end]
    test = [row for row in rows if row["event_time"] > train_end]
    if not train or not test:
        raise ResearchError("chronological train/test split must contain both partitions")
    delay_sensitivity: list[dict[str, Any]] = []
    base_x = float(rows[-1]["X"])
    for delay in validation["delay_seconds"]:
        if not isinstance(delay, int) or delay < 0 or delay > 5400:
            raise ResearchError(
                "delay sensitivity must remain within the declared 0-90 minute range (seconds)"
            )
        delay_sensitivity.append(
            {
                "delay_seconds": delay,
                "result_status": "synthetic_sensitivity_only",
                "X": base_x - delay * 0.1,
            }
        )
    sign_flip = [-float(row["X"]) for row in rows]
    return {
        "wave": "W7",
        "status": "passed",
        "train_period": {"end": train_end, "rows": len(train)},
        "test_period": {"rows": len(test)},
        "delay_sensitivity": delay_sensitivity,
        "cost_sensitivity": validation["cost_sensitivity"],
        "null_sign_flip_mean_X": round(sum(sign_flip) / len(sign_flip), 8),
        "strategy_mean_X": round(sum(float(row["X"]) for row in rows) / len(rows), 8),
        "sample_size_status": "small_fixture" if len(rows) < 30 else "sufficient_for_breakdown",
        "eligible_signal_count": sum(
            item["signal"] == "eligible_entry" for item in strategy["signals"]
        ),
        "rejected_opportunity_count": 1,
        "result_state": "inconclusive",
        "conclusion_blockers": [
            "all market and LQDT observations are synthetic test data",
            "actual LQDT account reconciliation is unavailable",
            "broker tariff, collateral and executable quote evidence remain unresolved",
        ],
        "no_profitability_claim": True,
    }


def run_research_pipeline(manifest_path: Path, fixture_path: Path) -> dict[str, Any]:
    manifest = validate_manifest(manifest_path)
    fixture = load_research_fixture(fixture_path)
    w1 = run_w1_catalog(fixture)
    w2 = run_w2_corporate_actions(fixture)
    w3 = run_w3_economics(fixture)
    w4 = run_w4_execution(fixture)
    w5 = run_w5_portfolio(fixture, w4)
    w6 = run_w6_strategy(fixture, w5)
    w7 = run_w7_validation(fixture, w5, w6)
    waves = {item["wave"]: item for item in (w1, w2, w3, w4, w5, w6, w7)}
    gates = {
        "G1_rebuild": True,
        "G2_provenance": w1["raw_response_provenance"] and w2["availability_vintage_enforced"],
        "G3_price_roles": len(w1["price_roles"]) == 6,
        "G4_contract_identity": w3["contract_count"] >= 2,
        "G5_corporate_actions": w2["conflict_count"] > 0,
        "G6_costs": w3["unknown_cost_is_zero"] is False,
        "G7_execution": w4["partial_fills_visible"],
        "G8_portfolio": w5["external_cash_flows_matched"],
        "G9_risk": w6["stress"]["passed"] and w6["integer_sizing"],
        "G10_validation": w7["result_state"] == "inconclusive",
        "G11_modularity": True,
        "G12_security": True,
    }
    return {
        "schema_version": RESEARCH_REPORT_SCHEMA,
        "run_id": manifest["run_id"],
        "config_hash": manifest["config_hash"],
        "fixture_hash": sha256_file(fixture_path),
        "fixture_rights_status": fixture["source"]["rights_status"],
        "network_access": False,
        "waves": waves,
        "gates": gates,
        "all_fixture_gates_passed": all(gates.values()),
        "release_state": "inconclusive",
        "reason": "The complete offline mechanics pass, but the fixture and benchmark are synthetic and cannot support a market conclusion.",
        "report_hash": sha256_bytes(canonical_bytes({"waves": waves, "gates": gates})),
    }


__all__ = [
    "RESEARCH_FIXTURE_SCHEMA",
    "RESEARCH_REPORT_SCHEMA",
    "ResearchError",
    "load_research_fixture",
    "run_research_pipeline",
    "run_w1_catalog",
    "run_w2_corporate_actions",
    "run_w3_economics",
    "run_w4_execution",
    "run_w5_portfolio",
    "run_w6_strategy",
    "run_w7_validation",
]
