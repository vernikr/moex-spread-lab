"""Rounding each displayed trade differs from rounding the raw ledger total."""

from types import SimpleNamespace
from typing import cast

import pytest

from moex_spread_lab.proof import ledger_blocks
from moex_spread_lab.proof.pair_payload import (
    PairPayloadSpec,
    _summary_block,
    apply_raw_trade_totals,
)


def test_independently_rounded_trades_can_differ_from_unrounded_ledger(monkeypatch):
    # Three real, positive P&L increments smaller than half a kopek each.
    # Their sum rounds to one kopek, while each displayed trade rounds to zero.
    rows, trades = [], []
    for i in range(3):
        entry, exit_ = f"2026-01-{2 * i + 1:02}", f"2026-01-{2 * i + 2:02}"
        rows.extend(
            [
                {"date": entry, "near": 0.0, "far": 0.0},
                {"date": exit_, "near": 0.004, "far": 0.0},
            ]
        )
        trades.append(
            {
                "number": i + 1,
                "side": "long_spread",
                "entry": {"date": entry, "near_price": 0.0, "far_price": 0.0, "costs": {}},
                "exit": {"date": exit_, "near_price": 0.004, "far_price": 0.0, "costs": {}},
                "gross_rub": round(0.004, 2),
                "net_rub": round(0.004, 2),
                "costs_rub": 0.0,
                "lqdt_on_margin_rub": 0.0,
                "open_at_end": False,
            }
        )
    raw = []
    original = ledger_blocks._ledger_row

    def capture(row, position, mark, cost, cumulative, labels):
        raw.append(cumulative)
        return original(row, position, mark, cost, cumulative, labels)

    monkeypatch.setattr(ledger_blocks, "_ledger_row", capture)
    ledger = ledger_blocks.price_ledger(rows, trades, 1.0, 1.0, 1)
    summary = _summary_block(
        cast(PairPayloadSpec, SimpleNamespace(trades=trades, ledger=ledger, daily_rows=[])),
        trades,
        0.0,
        0.0,
        0.0,
    )
    assert raw[-1] == pytest.approx(sum([0.004] * 3))
    assert sum(t["net_rub"] for t in trades) == 0.0
    assert ledger[-1]["cumulative_net_rub"] == 0.01
    assert summary["ledger_reconciliation_rub"] == -0.01


def test_unrounded_aggregate_reconciles_without_mutating_trades_or_journal():
    from copy import deepcopy

    trades = [
        {"gross_rub": 0.0, "costs_rub": 0.0, "net_rub": 0.0, "lqdt_on_margin_rub": 0.0}
        for _ in range(3)
    ]
    ledger = [{"cumulative_net_rub": 0.01}]
    original_trades, original_ledger = deepcopy(trades), deepcopy(ledger)
    summary = {}
    apply_raw_trade_totals(summary, trades, ledger, (0.012, 0.0, 0.012))
    assert summary["net_rub"] == 0.01
    assert summary["ledger_reconciliation_rub"] == 0.0
    assert summary["displayed_trade_net_rounding_delta_rub"] == -0.01
    assert trades == original_trades and ledger == original_ledger

    # A real drift is still reported, never suppressed as rounding.
    ledger[-1]["cumulative_net_rub"] = 0.03
    apply_raw_trade_totals(summary, trades, ledger, (0.012, 0.0, 0.012))
    assert summary["ledger_reconciliation_rub"] == -0.02


def test_fixed_pair_summary_uses_raw_trade_values_from_engine(tmp_path, monkeypatch):
    from math import fsum

    from funding_fixtures import fixture, request
    from moex_spread_lab.proof import fixed_plugin_payload
    from moex_spread_lab.proof.proof_search import research_run
    from moex_spread_lab.proof.service import ProofService

    fixture(tmp_path)
    seen = []
    original = fixed_plugin_payload.trade_block

    def capture(spec, *args, **kwargs):
        seen.append(spec.net_rub)
        return original(spec, *args, **kwargs)

    monkeypatch.setattr(fixed_plugin_payload, "trade_block", capture)
    service = ProofService(tmp_path)
    try:
        result = research_run(service, request(), optimize=False, include_plateau=False)
    finally:
        service.close()
    assert seen
    # Every replay of the run (training cut-off and full window) prices its own trades;
    # the payload summary is the final replay's raw sum.
    final = seen[-len(result["trades"]) :]
    assert result["summary"]["net_rub"] == round(fsum(final), 2)
    assert result["summary"]["ledger_reconciliation_rub"] == round(
        result["summary"]["net_rub"] - result["ledger"][-1]["cumulative_net_rub"], 2
    )


def _assert_quantity_invariants(ledger: list[dict]) -> None:
    for row in ledger:
        qty = row["quantity"]
        assert type(qty) is int and qty >= 0, row
        assert (row["position"] == 0) == (qty == 0), row


def test_price_ledger_quantity_covers_flat_active_ineligible_and_transition_days():
    rows = [
        {"date": "2026-01-01", "near": 100.0, "far": 100.0, "volume_eligible": True},
        {"date": "2026-01-02", "near": 101.0, "far": 100.0, "volume_eligible": True},
        {"date": "2026-01-03", "near": 999.0, "far": 100.0, "volume_eligible": False},
        {"date": "2026-01-04", "near": 102.0, "far": 100.0, "volume_eligible": True},
        {"date": "2026-01-05", "near": 103.0, "far": 100.0, "volume_eligible": True},
        {"date": "2026-01-06", "near": 104.0, "far": 100.0, "volume_eligible": False},
        {"date": "2026-01-07", "near": 105.0, "far": 100.0, "volume_eligible": True},
    ]
    trades = [
        {
            "number": 1,
            "side": "long_spread",
            "entry": {"date": "2026-01-02", "near_price": 101.0, "far_price": 100.0, "costs": {}},
            "exit": {"date": "2026-01-04", "near_price": 102.0, "far_price": 100.0, "costs": {}},
        },
        {
            "number": 2,
            "side": "short_spread",
            "entry": {"date": "2026-01-04", "near_price": 102.0, "far_price": 100.0, "costs": {}},
            "exit": {"date": "2026-01-05", "near_price": 103.0, "far_price": 100.0, "costs": {}},
        },
    ]
    ledger = ledger_blocks.price_ledger(rows, trades, 1.0, 1.0, 3)
    _assert_quantity_invariants(ledger)
    # Day 1 flat, Day 2 entry long (3), Day 3 ineligible while holding (3, mark_pnl=0),
    # Day 4 exit #1 + entry #2 short (3), Day 5 exit #2 flat (0), Day 6 ineligible flat (0),
    # Day 7 flat (0).
    assert [(r["position"], r["quantity"]) for r in ledger] == [
        (0, 0),
        (1, 3),
        (1, 3),
        (-1, 3),
        (0, 0),
        (0, 0),
        (0, 0),
    ]
    assert ledger[2]["mark_pnl_rub"] == 0.0
    assert ledger[3]["event"] == "EXIT #1 + ENTRY #2"

    flat_ledger = ledger_blocks.price_ledger(rows[:3], [], 1.0, 1.0, 3)
    _assert_quantity_invariants(flat_ledger)
    assert [r["quantity"] for r in flat_ledger] == [0, 0, 0]


def test_funding_ledger_quantity_covers_flat_active_exit_transition_and_open_at_end():
    daily_rows = [
        {
            "date": f"2026-01-0{i}",
            "position": pos,
            "gross_pnl_increment": 0.0,
            "daily_funding_included_rub": 0.0,
        }
        for i, pos in enumerate((0, 1, 1, 0, 1, 1), start=1)
    ]
    trades = [
        {
            "number": 1,
            "quantity": 2,
            "open_at_end": False,
            "entry": {"date": "2026-01-02", "costs": {}},
            "exit": {"date": "2026-01-04", "costs": {}},
        },
        {
            "number": 2,
            "quantity": 4,
            "open_at_end": True,
            "entry": {"date": "2026-01-05", "costs": {}},
            "exit": {"date": "2026-01-06", "costs": {}},
        },
    ]
    ledger = ledger_blocks.funding_ledger(daily_rows, trades)
    _assert_quantity_invariants(ledger)
    assert [(r["position"], r["quantity"]) for r in ledger] == [
        (0, 0),
        (-1, 2),
        (-1, 2),
        (0, 0),
        (-1, 4),
        (-1, 4),
    ]

    # Same-day exit #1 + entry #2 switches held quantity; same-day entry+exit stays flat.
    transition_rows = [
        {
            "date": f"2026-02-0{i}",
            "position": pos,
            "gross_pnl_increment": 0.0,
            "daily_funding_included_rub": 0.0,
        }
        for i, pos in enumerate((1, 1, 0, 0), start=1)
    ]
    transition_trades = [
        {
            "number": 1,
            "quantity": 2,
            "open_at_end": False,
            "entry": {"date": "2026-02-01", "costs": {}},
            "exit": {"date": "2026-02-02", "costs": {}},
        },
        {
            "number": 2,
            "quantity": 5,
            "open_at_end": False,
            "entry": {"date": "2026-02-02", "costs": {}},
            "exit": {"date": "2026-02-03", "costs": {}},
        },
        {
            "number": 3,
            "quantity": 3,
            "open_at_end": False,
            "entry": {"date": "2026-02-04", "costs": {}},
            "exit": {"date": "2026-02-04", "costs": {}},
        },
    ]
    transition_ledger = ledger_blocks.funding_ledger(transition_rows, transition_trades)
    _assert_quantity_invariants(transition_ledger)
    assert [(r["position"], r["quantity"]) for r in transition_ledger] == [
        (-1, 2),
        (-1, 5),
        (0, 0),
        (0, 0),
    ]


def test_proof_payload_carries_ledger_quantity_across_fixed_and_sized_strategies(tmp_path):
    from funding_fixtures import fixture, request
    from moex_spread_lab.proof.proof_search import research_run
    from moex_spread_lab.proof.service import ProofService

    fixture(tmp_path)
    service = ProofService(tmp_path)
    try:
        fixed_mr = {**request("mean_reversion"), "quantity": 2}
        fixed_carry = {**request("funding_only"), "quantity": 2}
        sized_mr = {**request("mean_reversion"), "sizing": "max_affordable"}
        sized_mr.pop("quantity")
        sized_carry = {**request("funding_only"), "sizing": "max_affordable"}
        sized_carry.pop("quantity")

        for label, req, expected_fixed_qty in (
            ("fixed_mr", fixed_mr, 2),
            ("fixed_carry", fixed_carry, 2),
            ("sized_mr", sized_mr, None),
            ("sized_carry", sized_carry, None),
        ):
            payload = research_run(service, req, optimize=False, include_plateau=False)
            ledger = payload["ledger"]
            assert ledger and payload["trades"], label
            _assert_quantity_invariants(ledger)
            active = [r for r in ledger if r["position"] != 0]
            flat = [r for r in ledger if r["position"] == 0]
            assert active and flat, label
            if expected_fixed_qty is not None:
                assert {r["quantity"] for r in active} == {expected_fixed_qty}, label
            else:
                trade_qtys = {t["quantity"] for t in payload["trades"]}
                assert {r["quantity"] for r in active} <= trade_qtys, label
            assert payload["summary"]["ledger_reconciliation_rub"] == 0.0
    finally:
        service.close()
