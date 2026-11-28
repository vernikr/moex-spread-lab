"""Working VTB EBS/KPUR margin and collateral stress calculations."""

from __future__ import annotations

import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from moex_spread_lab.core.w0 import W0Error, load_contracts

ACCOUNT_RISK_SCHEMA = "vtb-account-risk.v1"
DEFAULT_INITIAL_CAPITAL = 30_000.0
DEFAULT_STRESS_VM_BUFFER = 5_000.0
DEFAULT_UNCERTAINTY_BUFFER = 5_000.0
DEFAULT_KEY_RATE = 0.14


class AccountRiskError(W0Error):
    pass


def _round(value: float) -> float:
    return round(float(value), 8)


def _finite(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise AccountRiskError(f"{name} must be a finite number")
    return float(value)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _key_rate(key_rate: object) -> float:
    rate = _finite(key_rate, "key rate")
    if rate < 0:
        raise AccountRiskError("key rate must be non-negative")
    if rate >= 0.5:
        raise AccountRiskError("key rate must be a fraction such as 0.14, not a percentage")
    return rate


def ebs_undercovered_margin_charge(
    gross_margin: float,
    rub_cash: float,
    *,
    key_rate: float = DEFAULT_KEY_RATE,
    days: int = 1,
) -> float:
    margin = _finite(gross_margin, "margin")
    cash = _finite(rub_cash, "cash")
    if margin < 0 or cash < 0:
        raise AccountRiskError("margin and cash must be non-negative")
    rate = _key_rate(key_rate)
    if not _is_int(days) or days < 0:
        raise AccountRiskError("days must be a non-negative integer")
    return max(margin - cash, 0.0) * (rate + 0.02) * days / 365.0


def _observed_loss(observed_daily_pnl: Iterable[float] | None) -> float:
    if observed_daily_pnl is None:
        return 0.0
    values = [float(value) for value in observed_daily_pnl]
    if any(not math.isfinite(value) for value in values):
        raise AccountRiskError(
            "observed daily pnl must be finite; a missing observation cannot become zero loss"
        )
    return max(0.0, -min(values)) if values else 0.0


def assess_vtb_pair(  # noqa: PLR0913  # public keyword-only stress screen API preserved for callers
    contracts_path: Path,
    near: str,
    far: str,
    *,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
    stress_vm_buffer: float = DEFAULT_STRESS_VM_BUFFER,
    uncertainty_buffer: float = DEFAULT_UNCERTAINTY_BUFFER,
    key_rate: float = DEFAULT_KEY_RATE,
    observed_daily_pnl: Iterable[float] | None = None,
    quantity: int = 1,
) -> dict[str, Any]:
    """Fail-closed working VTB capital screen; never credits LQDT as strategy cash."""
    capital = _finite(initial_capital, "initial capital")
    vm_buffer = _finite(stress_vm_buffer, "stress VM buffer")
    uncertainty = _finite(uncertainty_buffer, "uncertainty buffer")
    if capital <= 0 or vm_buffer < 0 or uncertainty < 0:
        raise AccountRiskError("capital and buffers must be positive or zero")
    rate = _key_rate(key_rate)
    if not _is_int(quantity) or quantity < 1:
        raise AccountRiskError("quantity must be a positive integer")

    contracts, contracts_hash = load_contracts(contracts_path, near, far)
    near_margin = _finite(contracts[near]["initial_margin"], "near margin") * quantity
    far_margin = _finite(contracts[far]["initial_margin"], "far margin") * quantity
    pair_margin = near_margin + far_margin
    one_leg_margin = max(near_margin, far_margin)
    observed_worst_loss = _observed_loss(observed_daily_pnl) * quantity
    effective_vm_buffer = max(vm_buffer * quantity, observed_worst_loss)

    scenarios: list[dict[str, Any]] = []
    for scenario_id, margin, description in (
        ("current_snapshot_no_offset", pair_margin, "current ISS margin sum; no offset credit"),
        (
            "single_leg_failure",
            one_leg_margin,
            "larger individual leg margin before the second leg",
        ),
        ("two_times_margin_shock", pair_margin * 2.0, "2x pair margin shock"),
        ("three_times_margin_shock", pair_margin * 3.0, "3x pair margin shock"),
    ):
        total_required = margin + effective_vm_buffer + uncertainty
        scenarios.append(
            {
                "id": scenario_id,
                "description": description,
                "margin_requirement_rub": _round(margin),
                "vm_buffer_rub": _round(effective_vm_buffer),
                "uncertainty_buffer_rub": _round(uncertainty),
                "total_required_rub": _round(total_required),
                "remaining_capital_rub": _round(capital - total_required),
                "passed": total_required <= capital,
            }
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
            "quantity_per_leg": int(quantity),
            "near_initial_margin_rub": _round(near_margin),
            "far_initial_margin_rub": _round(far_margin),
            "pair_margin_no_offset_rub": _round(pair_margin),
            "largest_single_leg_margin_rub": _round(one_leg_margin),
            "source_vintage": "current_iss_snapshot",
        },
        "stress_inputs": {
            "initial_capital_rub": _round(capital),
            "configured_vm_buffer_rub": _round(vm_buffer),
            "observed_worst_daily_loss_rub": _round(observed_worst_loss),
            "effective_vm_buffer_rub": _round(effective_vm_buffer),
            "uncertainty_buffer_rub": _round(uncertainty),
            "key_rate_assumption": rate,
            "basis": "R07_working_stress_proposal_plus_current_pair_snapshot",
        },
        "ebs_undercovered_margin_sensitivity": {
            "formula": "max(ГО - RubCash, 0) * (key_rate + 2%) * days / 365",
            "one_day_zero_rub_cash_charge_rub": _round(
                ebs_undercovered_margin_charge(pair_margin, 0.0, key_rate=rate)
            ),
            "one_day_full_initial_capital_cash_charge_rub": _round(
                ebs_undercovered_margin_charge(pair_margin, capital, key_rate=rate)
            ),
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
            "working_profile": "packages/accounting/docs/vtb-account-risk-profile.md",
        },
    }
