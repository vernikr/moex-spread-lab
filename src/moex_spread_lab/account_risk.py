"""Working VTB EBS/KPUR margin and collateral stress calculations."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .analytics import load_contracts
from .w0 import W0Error


ACCOUNT_RISK_SCHEMA = "vtb-account-risk.v1"
DEFAULT_INITIAL_CAPITAL = 30_000.0
DEFAULT_STRESS_VM_BUFFER = 5_000.0
DEFAULT_UNCERTAINTY_BUFFER = 5_000.0
DEFAULT_KEY_RATE = 0.14


class AccountRiskError(W0Error):
    """Raised when the working account-risk calculation cannot be built."""


def _round(value: float) -> float:
    return round(float(value), 8)


def ebs_undercovered_margin_charge(
    gross_margin: float,
    rub_cash: float,
    *,
    key_rate: float = DEFAULT_KEY_RATE,
    days: int = 1,
) -> float:
    """Calculate the published EBS under-covered-margin sensitivity."""
    if gross_margin < 0 or rub_cash < 0 or key_rate < 0 or days < 0:
        raise AccountRiskError("margin, cash, rate and days must be non-negative")
    uncovered = max(gross_margin - rub_cash, 0.0)
    return uncovered * (key_rate + 0.02) * days / 365.0


def _observed_loss(observed_daily_pnl: Iterable[float] | None) -> float:
    if observed_daily_pnl is None:
        return 0.0
    values = [float(value) for value in observed_daily_pnl]
    if not values:
        return 0.0
    return max(0.0, -min(values))


def assess_vtb_pair(
    contracts_path: Path,
    near: str,
    far: str,
    *,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
    stress_vm_buffer: float = DEFAULT_STRESS_VM_BUFFER,
    uncertainty_buffer: float = DEFAULT_UNCERTAINTY_BUFFER,
    key_rate: float = DEFAULT_KEY_RATE,
    observed_daily_pnl: Iterable[float] | None = None,
) -> dict[str, Any]:
    """Assess minimum-lot capital under the project's working VTB profile.

    This is a fail-closed research screen. It does not claim the broker's
    client-specific margin or offset calculation and never credits LQDT as
    strategy cash.
    """
    if initial_capital <= 0 or stress_vm_buffer < 0 or uncertainty_buffer < 0:
        raise AccountRiskError("capital and buffers must be positive or zero")
    if key_rate < 0:
        raise AccountRiskError("key rate must be non-negative")
    contracts, contracts_hash = load_contracts(contracts_path, near, far)
    near_contract = contracts[near]
    far_contract = contracts[far]
    near_margin = float(near_contract["initial_margin"])
    far_margin = float(far_contract["initial_margin"])
    pair_margin = near_margin + far_margin
    one_leg_margin = max(near_margin, far_margin)
    observed_worst_loss = _observed_loss(observed_daily_pnl)
    effective_vm_buffer = max(stress_vm_buffer, observed_worst_loss)

    scenario_specs = (
        ("current_snapshot_no_offset", pair_margin, "current ISS margin sum; no offset credit"),
        ("single_leg_failure", one_leg_margin, "larger individual leg margin before the second leg"),
        ("two_times_margin_shock", pair_margin * 2.0, "2x pair margin shock"),
        ("three_times_margin_shock", pair_margin * 3.0, "3x pair margin shock"),
    )
    scenarios = []
    for scenario_id, margin, description in scenario_specs:
        total_required = margin + effective_vm_buffer + uncertainty_buffer
        scenarios.append({
            "id": scenario_id,
            "description": description,
            "margin_requirement_rub": _round(margin),
            "vm_buffer_rub": _round(effective_vm_buffer),
            "uncertainty_buffer_rub": _round(uncertainty_buffer),
            "total_required_rub": _round(total_required),
            "remaining_capital_rub": _round(initial_capital - total_required),
            "passed": total_required <= initial_capital,
        })

    zero_cash_charge = ebs_undercovered_margin_charge(
        pair_margin,
        0.0,
        key_rate=key_rate,
    )
    full_cash_charge = ebs_undercovered_margin_charge(
        pair_margin,
        initial_capital,
        key_rate=key_rate,
    )
    return {
        "schema_version": ACCOUNT_RISK_SCHEMA,
        "profile_id": "vtb_ebs_kpur_working_profile",
        "release_state": "offline_working_assumption",
        "account": {
            "broker": "VTB",
            "account_structure": "EBS",
            "risk_category": "KPUR",
            "core_lqdt_available_as_strategy_cash": False,
            "lqdt_collateral_assumption": "eligible_with_8_percent_kpur_risk_discount_snapshot",
        },
        "pair": {
            "near": near,
            "far": far,
            "quantity_per_leg": 1,
            "near_initial_margin_rub": _round(near_margin),
            "far_initial_margin_rub": _round(far_margin),
            "pair_margin_no_offset_rub": _round(pair_margin),
            "largest_single_leg_margin_rub": _round(one_leg_margin),
            "source_vintage": "current_iss_snapshot",
        },
        "stress_inputs": {
            "initial_capital_rub": _round(initial_capital),
            "configured_vm_buffer_rub": _round(stress_vm_buffer),
            "observed_worst_daily_loss_rub": _round(observed_worst_loss),
            "effective_vm_buffer_rub": _round(effective_vm_buffer),
            "uncertainty_buffer_rub": _round(uncertainty_buffer),
            "key_rate_assumption": key_rate,
            "basis": "R07_working_stress_proposal_plus_current_pair_snapshot",
        },
        "ebs_undercovered_margin_sensitivity": {
            "formula": "max(ГО - RubCash, 0) * (key_rate + 2%) * days / 365",
            "one_day_zero_rub_cash_charge_rub": _round(zero_cash_charge),
            "one_day_full_initial_capital_cash_charge_rub": _round(full_cash_charge),
            "charge_status": "sensitivity_not_historical_account_charge",
        },
        "scenarios": scenarios,
        "summary": {
            "all_scenarios_passed": all(scenario["passed"] for scenario in scenarios),
            "current_snapshot_passed": scenarios[0]["passed"],
            "most_severe_scenario": scenarios[-1]["id"],
            "most_severe_remaining_capital_rub": scenarios[-1]["remaining_capital_rub"],
            "live_account_reconciliation": False,
        },
        "provenance": {
            "contracts_path": str(contracts_path),
            "contracts_sha256": contracts_hash,
            "public_rules": [
                "docs/research/results/received/R01/R01.md",
                "docs/research/results/received/R02/R02.md",
            ],
            "working_profile": "docs/development/vtb-account-risk-profile.md",
        },
    }
