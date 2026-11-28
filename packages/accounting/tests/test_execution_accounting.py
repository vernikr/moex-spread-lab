import argparse
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
from moex_spread_lab.accounting.cli import register as register_accounting_cli
from moex_spread_lab.accounting.costs import (
    cost_totals,
    lqdt_overlay_values,
    side_cost,
    tariff_profile,
)
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
    cost = side_cost(profile, [(NEAR, 300.0)], 3, as_of="2026-11-02")
    assert cost == {"exchange_clearing": pytest.approx(27.0), "broker": 0.0, "slippage": 6.0}

    native = side_cost(
        profile, [(NEAR, 300.0), (FAR, 305.0)], 2, as_of="2026-11-02", native_spread=True
    )
    two_leg = side_cost(profile, [(NEAR, 300.0), (FAR, 305.0)], 2, as_of="2026-11-02")
    assert native["exchange_clearing"] == pytest.approx(two_leg["exchange_clearing"])
    assert (native["slippage"], two_leg["slippage"]) == (4.0, 12.0)

    custom = side_cost(
        profile,
        [({**NEAR, "secid": "X", "tariff_group": "currency"}, 10.0)],
        1,
        as_of="2026-11-02",
    )
    assert custom["exchange_clearing"] == pytest.approx(0.0462)
    assert cost_totals(
        [{"entry": {"costs": cost}, "exit": {"costs": cost}}, {"entry": {}, "exit": {}}]
    ) == {
        "exchange_clearing": 54.0,
        "broker": 0.0,
        "slippage": 12.0,
    }


def test_costs_honour_the_profile_rates_and_broker_fee() -> None:
    profile = tariff_profile(1.0, broker_fee=5.0, broker_fee_effective_to="2027-06-30")
    profile["taker_rate_percent"] = {"currency": 99.0}
    cost = side_cost(
        profile,
        [({"secid": "SiZ6", "tick_value": 1.0, "tick_size": 1.0}, 100000.0)],
        2,
        as_of="2026-11-02",
    )
    assert cost["exchange_clearing"] == pytest.approx(2 * 100000.0 * 99.0 / 100)
    assert cost["broker"] == pytest.approx(10.0)
    two_leg = side_cost(
        profile,
        [({"secid": "SiZ6", "tick_value": 1.0, "tick_size": 1.0}, 1.0)] * 2,
        3,
        as_of="2026-11-02",
    )
    assert two_leg["broker"] == pytest.approx(30.0)


def test_costs_fail_closed_outside_the_broker_fee_window() -> None:
    profile = tariff_profile(1.0)
    assert side_cost(profile, [(NEAR, 300.0)], 1, as_of="2026-12-31")["broker"] == 0.0
    with pytest.raises(W0Error, match="broker fee assumption expires"):
        side_cost(profile, [(NEAR, 300.0)], 1, as_of="2027-01-01")
    with pytest.raises(W0Error, match="ISO trade date"):
        side_cost(profile, [(NEAR, 300.0)], 1, as_of="not-a-date")
    with pytest.raises(W0Error, match="taker_rate_percent"):
        side_cost({**profile, "taker_rate_percent": {}}, [(NEAR, 300.0)], 1, as_of="2026-11-02")
    with pytest.raises(W0Error, match="no taker rate"):
        side_cost(
            {**profile, "taker_rate_percent": {"stock": 1.0}},
            [({**NEAR, "tariff_group": "currency"}, 1.0)],
            1,
            as_of="2026-11-02",
        )
    for bad_fee, bad_window in (
        (-1.0, "2026-12-31"),
        (float("nan"), "2026-12-31"),
        (0.0, "31.12.2026"),
    ):
        with pytest.raises(W0Error):
            tariff_profile(1.0, broker_fee=bad_fee, broker_fee_effective_to=bad_window)


@pytest.mark.parametrize("bad", [-1, 10.5, True, "1"])
def test_costs_rejects_invalid_slippage_and_unknown_secid(bad: object) -> None:
    with pytest.raises(W0Error):
        tariff_profile(bad)
    with pytest.raises(W0Error):
        side_cost(tariff_profile(), [({**NEAR, "secid": "XX"}, 1.0)], 1, as_of="2026-11-02")


def test_lqdt_overlay_compounds_free_cash_and_flags_negative_only() -> None:
    flat = lqdt_overlay_values(1000.0, [0, 0, 0], [0, 0, 0], [1.0, 1.1, 1.21])
    held = lqdt_overlay_values(1000.0, [0, 0, 0], [0, 500, 500], [1.0, 1.1, 1.21])
    assert flat.values[-1] == pytest.approx(1210.0)
    assert held.values[-1] == pytest.approx(1160.0)
    assert (flat.shortfall_rows, flat.first_shortfall_index, flat.max_deficit) == (0, None, 0.0)
    covered = lqdt_overlay_values(100.0, [0, -20.0], [0, 80.0], [1.0, 1.0])
    assert covered.shortfall_rows == 0
    short = lqdt_overlay_values(100.0, [0, -150.0], [0, 80.0], [1.0, 1.0])
    assert short.shortfall_rows == 1
    assert (short.first_shortfall_index, short.max_deficit) == (1, 130.0)


def test_lqdt_overlay_does_not_finance_a_deficit_at_the_benchmark_rate() -> None:
    # free cash 100 earns +10% then loses 200 -> -90; the deficit then stays put
    # instead of compounding at +10% a step into -99 and -108.9.
    overlay = lqdt_overlay_values(100.0, [0, -200, -200, -200], [0, 0, 0, 0], [1, 1.1, 1.21, 1.331])
    assert overlay.values == pytest.approx([100.0, -90.0, -90.0, -90.0])
    assert overlay.shortfall_rows == 3
    assert overlay.first_shortfall_index == 1
    assert overlay.max_deficit == pytest.approx(90.0)
    recovered = lqdt_overlay_values(100.0, [0, -200, -150], [0, 0, 0], [1, 1.1, 1.21])
    assert recovered.values == pytest.approx([100.0, -90.0, -40.0])
    assert recovered.first_shortfall_index == 1
    assert recovered.max_deficit == pytest.approx(90.0)


def test_cost_totals_keeps_every_component_or_fails_closed() -> None:
    trade = {
        "entry": {"costs": {"exchange_clearing": 1.0, "broker": 0.5, "slippage": 2.0}},
        "exit": {"costs": {"exchange_clearing": 3.0, "broker": 0.5, "slippage": 4.0}},
    }
    totals = cost_totals([trade, {"entry": {}, "exit": {}}])
    assert totals == {"exchange_clearing": 4.0, "broker": 1.0, "slippage": 6.0}
    assert sum(totals.values()) == pytest.approx(11.0)
    with pytest.raises(W0Error, match="unknown cost component clearing_fee"):
        cost_totals([{"entry": {"costs": {"clearing_fee": 7.0}}, "exit": {}}])
    for bad in (None, float("nan"), float("inf"), True, "1"):
        with pytest.raises(W0Error, match="must be a finite RUB amount"):
            cost_totals([{"entry": {"costs": {"slippage": bad}}, "exit": {}}])
    with pytest.raises(W0Error, match="must be a cost block"):
        cost_totals([{"entry": {"costs": {}}, "exit": []}])


@pytest.mark.parametrize(
    ("capital", "pnl", "margins", "prices", "message"),
    [
        (100.0, [0, 1, 2], [0, 0], [1, 1, 1], "align row by row"),
        (100.0, [0], [0, 0, 0], [1, 1, 1], "align row by row"),
        (100.0, [0, 1, 2], [0, 0, 0], [1, 0, 1], "benchmark prices must be >"),
        (100.0, [0, float("nan")], [0, 0], [1, 1], "cumulative pnl must be finite"),
        (100.0, [0, float("inf")], [0, 0], [1, 1], "cumulative pnl must be finite"),
        (100.0, [0, 1], [0, -1], [1, 1], "margins must be >= 0.0"),
        (0.0, [0], [0], [1], "capital must be a finite positive"),
        (-5.0, [0], [0], [1], "capital must be a finite positive"),
        (float("nan"), [0], [0], [1], "capital must be a finite positive"),
        (100.0, [], [], [], "cannot be empty"),
    ],
)
def test_lqdt_overlay_validates_its_inputs(
    capital: float,
    pnl: list[float],
    margins: list[float],
    prices: list[float],
    message: str,
) -> None:
    with pytest.raises(W0Error, match=message):
        lqdt_overlay_values(capital, pnl, margins, prices)


def test_lqdt_overlay_accepts_a_negative_cumulative_pnl() -> None:
    overlay = lqdt_overlay_values(100.0, [0, -5, -10], [0, 0, 0], [1, 1.1, 1.21])
    assert overlay.values == pytest.approx([100.0, 105.0, 110.5])
    assert overlay.shortfall_rows == 0


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


@pytest.mark.parametrize(
    "name",
    [
        "accounting.ledger",
        "accounting.portfolio",
        "accounting.research",
        "accounting.research_intake",
        "accounting.research_simulation",
        "accounting.validation",
        "account_risk",
        "costs",
        "ledger",
        "portfolio",
        "position_sizing",
        "research",
        "validation",
    ],
)
def test_retired_modules_are_removed(name: str) -> None:
    module = f"moex_spread_lab.{name}"
    with pytest.raises(ModuleNotFoundError) as exc:
        importlib.import_module(module)
    assert exc.value.name == module


def test_costs_exposes_only_canonical_helpers() -> None:
    from moex_spread_lab import accounting
    from moex_spread_lab.accounting import costs

    assert accounting.lqdt_overlay_values is lqdt_overlay_values
    assert not hasattr(costs, "_cost_totals")


def test_accounting_cli_exposes_only_live_commands() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    register_accounting_cli(commands)
    assert set(commands.choices) == {"account-risk-check"}
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(
            ["execution-ledger", "--baseline", "baseline.json", "--output", "ledger.json"]
        )
    assert exc.value.code == 2


def test_accounting_manifest_exposes_only_live_contracts() -> None:
    manifest = json.loads((Path(__file__).parents[1] / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["capabilities"]) == {
        "moex_tariff_costs",
        "vtb_account_risk_screen",
        "matched_lqdt_wealth_overlay",
        "causal_position_sizing",
    }
    assert manifest["inputs"] == ["iss-intake.v1"]
    assert manifest["outputs"] == ["vtb-account-risk.v1"]
