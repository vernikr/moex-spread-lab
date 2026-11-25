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
