"""Sized-replay rounding disclosures and daily ledger quantity semantics."""

import pytest

from moex_spread_lab.accounting.position_sizing import size_proof
from moex_spread_lab.core.settings import ResearchSettings
from test_position_sizing import unit_proof


def _assert_quantity_invariants(ledger: list[dict]) -> None:
    for row in ledger:
        qty = row["quantity"]
        assert type(qty) is int and qty >= 0, row
        assert (row["position"] == 0) == (qty == 0), row


def test_sizing_discards_fixed_template_rounding_disclosures():
    proof, marks, contracts = unit_proof()
    for component in ("gross", "costs", "net"):
        proof["summary"][f"displayed_trade_{component}_rounding_delta_rub"] = 42
    result = size_proof(proof, marks, ResearchSettings(), contracts)
    assert not any("rounding_delta" in key for key in result["summary"])


def test_sizing_ledger_quantity_on_same_row_exit_and_reentry():
    unit, marks, contracts = unit_proof(entries=((0, 1), (1, 3)))
    result = size_proof(unit, marks, ResearchSettings(), contracts)
    _assert_quantity_invariants(result["ledger"])
    assert [t["quantity"] for t in result["trades"]] == [5, 4]
    assert [r["quantity"] for r in result["ledger"]] == [5, 4, 4, 0]
    assert result["ledger"][1]["event"] == "EXIT #1 + ENTRY #2"


def test_sizing_ledger_quantity_on_same_day_entry_and_exit_is_flat():
    unit, marks, contracts = unit_proof(near=(50000, 50100, 50100), entries=((1, 1),))
    result = size_proof(unit, marks, ResearchSettings(), contracts)
    _assert_quantity_invariants(result["ledger"])
    assert [t["quantity"] for t in result["trades"]] == [5]
    assert [(r["position"], r["quantity"]) for r in result["ledger"]] == [(0, 0), (0, 0), (0, 0)]


@pytest.mark.parametrize("funding_only", [False, True])
def test_sizing_ledger_quantity_on_terminal_open_at_end_row(funding_only: bool):
    unit, marks, contracts = unit_proof(near=(50000, 50000, 50000, 50000), entries=((0, 3),))
    unit["trades"][0]["open_at_end"] = True
    unit["series"][-1].update(funding_rub=2, funding_contracts=1)
    if funding_only:
        unit["selection"].update(strategy="funding_only", fill="same_day_settlement")
    result = size_proof(unit, marks, ResearchSettings(), contracts)
    qty = result["trades"][0]["quantity"]
    _assert_quantity_invariants(result["ledger"])
    assert [r["quantity"] for r in result["ledger"]] == [qty, qty, qty, 0]
    assert result["trades"][0]["funding_rub"] == 2 * qty


def test_sizing_ledger_quantity_on_skipped_entry_and_ineligible_row():
    skipped_unit, skipped_marks, skipped_contracts = unit_proof(margin=31000)
    skipped = size_proof(skipped_unit, skipped_marks, ResearchSettings(), skipped_contracts)
    _assert_quantity_invariants(skipped["ledger"])
    assert skipped["skipped_entries"] and not skipped["trades"]
    assert all(r["quantity"] == 0 for r in skipped["ledger"])

    unit, marks, contracts = unit_proof(near=(50000, 50100, 50200, 50300), entries=((0, 2),))
    unit["series"][1]["volume_eligible"] = False
    unit["series"][3]["volume_eligible"] = False
    result = size_proof(unit, marks, ResearchSettings(), contracts)
    qty = result["trades"][0]["quantity"]
    _assert_quantity_invariants(result["ledger"])
    assert [(r["position"], r["quantity"]) for r in result["ledger"]] == [
        (1, qty),
        (1, qty),
        (0, 0),
        (0, 0),
    ]
    assert result["ledger"][1]["mark_pnl_rub"] == 0.0
