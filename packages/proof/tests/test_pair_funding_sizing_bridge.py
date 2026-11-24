"""Pair unit funding preparation must feed funded sizing without fictional exposure."""

from copy import deepcopy

import pytest

from moex_spread_lab.accounting.position_sizing import size_proof
from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof.pair_funding_replay import attach_daily_unit_funding
from moex_spread_lab.strategies.funding_carry.proof_funding import FundingTimeline, FundingWindow

DAYS = [f"2026-01-0{i}" for i in range(1, 6)]


def setup(margin=5000, terminal=True):
    rows = [
        {
            "date": d,
            "near": 50000,
            "far": 50000,
            "spread": 0,
            "near_open": 50000,
            "far_open": 50000,
            "volume_eligible": True,
        }
        for d in DAYS
    ]

    def fill(i):
        return {
            "date": DAYS[i],
            "near_price": 50000,
            "far_price": 50000,
            "spread_fill": 0,
            "costs": {},
        }

    intent = {
        "number": 1,
        "quantity": 1,
        "side": "long_spread",
        "entry": fill(1),
        "exit": fill(4),
        "open_at_end": terminal,
        "margin_rub": margin,
        "signal": {"kind": "test", "date": DAYS[0]},
    }
    rates = dict.fromkeys(DAYS, 0.2)
    window = FundingWindow(FundingTimeline("TEST", rates, DAYS), DAYS)
    contracts = {
        s: {
            "secid": s,
            "tick_size": 1.0,
            "tick_value": 1.0,
            "initial_margin": margin / 2,
            "tariff_group": "stock",
        }
        for s in ("N", "F")
    }
    unit = {
        "run_id": "unit",
        "universe": "pair_screen",
        "selection": {
            "universe": "pair_screen",
            "strategy": "mean_reversion",
            "parameters": {},
            "fill": "next_open",
        },
        "instrument": {"near": "N", "far": "F"},
        "contract": {},
        "window": {"passes_capital_screen": True},
        "series": rows,
        "trades": [intent],
        "summary": {},
        "notes": [],
    }
    return unit, contracts, window


@pytest.mark.parametrize("terminal", [False, True])
def test_sizing_scales_funding_only_on_accepted_exposure(terminal):
    unit, contracts, window = setup(terminal=terminal)
    before = deepcopy(unit)
    prepared = attach_daily_unit_funding(unit["series"], unit["trades"], window, near_lot_size=10)
    assert unit == before
    assert [r["funding_contracts"] for r in prepared["series"]] == [0, 1, 1, 1, int(terminal)]
    unit["series"] = prepared["series"]
    result = size_proof(unit, dict.fromkeys(DAYS, 1.0), ResearchSettings(), contracts)
    assert len(result["trades"]) == 1
    quantity = result["trades"][0]["quantity"]
    assert quantity > 0
    assert result["trades"][0]["funding_rub"] == -2 * quantity * (3 + int(terminal))
    assert sum(r["funding_rub"] for r in result["ledger"]) == result["trades"][0]["funding_rub"]
    assert result["summary"]["ledger_reconciliation_rub"] == 0


def test_rejected_intent_does_not_generate_funding():
    unit, contracts, window = setup(margin=31000)
    unit["series"] = attach_daily_unit_funding(
        unit["series"], unit["trades"], window, near_lot_size=10
    )["series"]
    result = size_proof(unit, dict.fromkeys(DAYS, 1.0), ResearchSettings(), contracts)
    assert not result["trades"] and len(result["skipped_entries"]) == 1
    assert all(
        row["funding_rub"] == 0 and row["funding_contracts"] == 0 for row in result["series"]
    )
    assert result["summary"]["ledger_reconciliation_rub"] == 0


def test_missing_session_and_ineligible_rows_rejected():
    unit, _, window = setup()
    with pytest.raises(W0Error):
        attach_daily_unit_funding(unit["series"][1:], unit["trades"], window, near_lot_size=10)
    unit["series"][2]["volume_eligible"] = False
    with pytest.raises(W0Error):
        attach_daily_unit_funding(unit["series"], unit["trades"], window, near_lot_size=10)


def test_verified_no_clearing_is_not_a_rate_or_funded_contract():
    unit, _, _ = setup()
    days = ["2025-08-15", "2025-08-16", "2025-08-17", "2025-08-18", "2025-08-19"]
    for row, day in zip(unit["series"], days, strict=True):
        row["date"] = day
    unit["trades"][0]["entry"]["date"] = days[0]
    unit["trades"][0]["exit"]["date"] = days[-1]
    rates = {days[0]: 0, days[3]: 0.2, days[4]: 0.3}
    window = FundingWindow(FundingTimeline("GAZPF", rates, days), days)
    rows = attach_daily_unit_funding(unit["series"], unit["trades"], window, near_lot_size=10)[
        "series"
    ]
    assert rows[0]["funding_contracts"] == 1 and rows[0]["funding_swaprate_rub_per_unit"] == 0
    for row in rows[1:3]:
        assert row["funding_rate_kind"] == "no_clearing"
        assert row["funding_swaprate_rub_per_unit"] is None
        assert row["funding_contracts"] == 0 and row["funding_rub"] == 0


def test_sizing_short_position_receives_positive_funding():
    unit, contracts, window = setup(terminal=False)
    unit["trades"][0]["side"] = "short_spread"
    unit["series"] = attach_daily_unit_funding(
        unit["series"], unit["trades"], window, near_lot_size=10
    )["series"]
    result = size_proof(unit, dict.fromkeys(DAYS, 1.0), ResearchSettings(), contracts)
    trade = result["trades"][0]
    assert trade["funding_rub"] == 6 * trade["quantity"]
    assert result["summary"]["ledger_reconciliation_rub"] == 0
