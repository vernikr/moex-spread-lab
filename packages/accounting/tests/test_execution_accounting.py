import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from moex_spread_lab.accounting.account_risk import (
    ACCOUNT_RISK_SCHEMA,
    AccountRiskError,
    assess_vtb_pair,
    ebs_undercovered_margin_charge,
)
from moex_spread_lab.accounting.costs import (
    cost_totals,
    lqdt_overlay_values,
    side_cost,
    tariff_profile,
)
from moex_spread_lab.accounting.portfolio import lqdt_overlay_values as portfolio_overlay
from moex_spread_lab.core.w0 import W0Error

NEAR = {
    "secid": "SBERF",
    "lot_size": 100,
    "tick_value": 1.0,
    "tick_size": 0.01,
    "initial_margin": 1000.0,
}
FAR = {
    "secid": "SRZ6",
    "lot_size": 100,
    "tick_value": 2.0,
    "tick_size": 1.0,
    "initial_margin": 1500.0,
}


def _contracts(tmp_path: Path) -> Path:
    path = tmp_path / "contracts.json"
    instruments = [
        {
            **c,
            "stable_id": c["secid"],
            "board": "RFUD",
            "expiry": "2026-12-17",
            "source_vintage": "current_snapshot",
        }
        for c in (NEAR, FAR)
    ]
    path.write_text(
        json.dumps({"schema_version": "iss-intake.v1", "instruments": instruments}),
        encoding="utf-8",
    )
    return path


def test_costs_tariff_native_spread_and_totals() -> None:
    profile = tariff_profile(2.0)
    assert profile["broker_fee"] == 0.0
    cost = side_cost(profile, [(NEAR, 300.0)], 3)
    assert cost == {"exchange_clearing": pytest.approx(27.0), "broker": 0.0, "slippage": 6.0}

    native = side_cost(profile, [(NEAR, 300.0), (FAR, 305.0)], 2, native_spread=True)
    two_leg = side_cost(profile, [(NEAR, 300.0), (FAR, 305.0)], 2)
    assert native["exchange_clearing"] == pytest.approx(two_leg["exchange_clearing"])
    assert (native["slippage"], two_leg["slippage"]) == (4.0, 12.0)

    custom = side_cost(profile, [({**NEAR, "secid": "X", "tariff_group": "currency"}, 10.0)], 1)
    assert custom["exchange_clearing"] == pytest.approx(0.0462)
    assert cost_totals(
        [{"entry": {"costs": cost}, "exit": {"costs": cost}}, {"entry": {}, "exit": {}}]
    ) == {
        "exchange_clearing": 54.0,
        "broker": 0.0,
        "slippage": 12.0,
    }


@pytest.mark.parametrize("bad", [-1, 10.5, True, "1"])
def test_costs_rejects_invalid_slippage_and_unknown_secid(bad: object) -> None:
    with pytest.raises(W0Error):
        tariff_profile(bad)
    with pytest.raises(W0Error):
        side_cost(tariff_profile(), [({**NEAR, "secid": "XX"}, 1.0)], 1)


def test_lqdt_overlay_compounds_free_cash_and_flags_negative_only() -> None:
    assert portfolio_overlay is lqdt_overlay_values
    flat, sf0 = lqdt_overlay_values(1000.0, [0, 0, 0], [0, 0, 0], [1.0, 1.1, 1.21])
    held, sf1 = lqdt_overlay_values(1000.0, [0, 0, 0], [0, 500, 500], [1.0, 1.1, 1.21])
    assert flat[-1] == pytest.approx(1210.0)
    assert held[-1] == pytest.approx(1160.0)
    assert sf0 == sf1 == 0
    assert lqdt_overlay_values(100.0, [0, -20.0], [0, 80.0], [1.0, 1.0])[1] == 0
    assert lqdt_overlay_values(100.0, [0, -150.0], [0, 80.0], [1.0, 1.0])[1] == 1


def test_ebs_charge_and_validation() -> None:
    assert ebs_undercovered_margin_charge(12000.0, 10000.0, key_rate=0.14, days=5) == pytest.approx(
        2000.0 * 0.16 * 5 / 365.0
    )
    assert ebs_undercovered_margin_charge(5000.0, 10000.0) == 0.0
    for gross, cash, rate, days in (
        (-1.0, 0.0, 0.14, 1),
        (1.0, -1.0, 0.14, 1),
        (1.0, 0.0, -0.1, 1),
        (1.0, 0.0, 0.14, -1),
    ):
        with pytest.raises(AccountRiskError):
            ebs_undercovered_margin_charge(gross, cash, key_rate=rate, days=days)


def test_assess_vtb_pair_scenarios_and_buffers(tmp_path: Path) -> None:
    path = _contracts(tmp_path)
    ok = assess_vtb_pair(path, "SBERF", "SRZ6", initial_capital=30000.0)
    assert ok["schema_version"] == ACCOUNT_RISK_SCHEMA
    assert (
        ok["pair"]["pair_margin_no_offset_rub"],
        ok["pair"]["largest_single_leg_margin_rub"],
    ) == (2500.0, 1500.0)
    assert [s["id"] for s in ok["scenarios"]] == [
        "current_snapshot_no_offset",
        "single_leg_failure",
        "two_times_margin_shock",
        "three_times_margin_shock",
    ]
    assert ok["summary"]["all_scenarios_passed"] is True
    assert (
        ok["ebs_undercovered_margin_sensitivity"]["one_day_full_initial_capital_cash_charge_rub"]
        == 0.0
    )

    partial = assess_vtb_pair(path, "SBERF", "SRZ6", initial_capital=15000.0)
    assert (
        partial["summary"]["current_snapshot_passed"],
        partial["summary"]["all_scenarios_passed"],
    ) == (True, False)
    assert partial["summary"]["most_severe_remaining_capital_rub"] == -2500.0

    stressed = assess_vtb_pair(
        path,
        "SBERF",
        "SRZ6",
        initial_capital=30000.0,
        stress_vm_buffer=4000.0,
        uncertainty_buffer=2000.0,
        observed_daily_pnl=[-6000.0, 50.0],
        quantity=2,
        key_rate=0.18,
    )
    assert stressed["pair"]["pair_margin_no_offset_rub"] == 5000.0
    assert stressed["stress_inputs"]["observed_worst_daily_loss_rub"] == 12000.0
    assert stressed["stress_inputs"]["effective_vm_buffer_rub"] == 12000.0
    assert stressed["summary"]["all_scenarios_passed"] is True

    small = assess_vtb_pair(path, "SBERF", "SRZ6", observed_daily_pnl=[-450.0, 20.0], quantity=2)
    assert small["stress_inputs"]["effective_vm_buffer_rub"] == 10000.0


def test_assess_vtb_pair_rejects_bad_inputs_and_unknown_kwargs(tmp_path: Path) -> None:
    path = _contracts(tmp_path)
    bad: tuple[dict[str, Any], ...] = (
        {"initial_capital": 0.0},
        {"stress_vm_buffer": -1.0},
        {"uncertainty_buffer": -1.0},
        {"key_rate": -0.01},
        {"quantity": 0},
    )
    for kw in bad:
        with pytest.raises(AccountRiskError):
            assess_vtb_pair(path, "SBERF", "SRZ6", **kw)
    extra: dict[str, Any] = {"unknown_option": 1}
    with pytest.raises(TypeError):
        assess_vtb_pair(path, "SBERF", "SRZ6", **extra)


def test_research_and_validation_modules_are_removed() -> None:
    for name in (
        "moex_spread_lab.accounting.research",
        "moex_spread_lab.accounting.research_intake",
        "moex_spread_lab.accounting.research_simulation",
        "moex_spread_lab.accounting.validation",
    ):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(name)
