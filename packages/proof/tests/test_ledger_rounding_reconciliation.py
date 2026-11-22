"""Rounding each displayed trade differs from rounding the raw ledger total."""

from types import SimpleNamespace

import pytest

from moex_spread_lab.proof_runtime import ledger_blocks
from moex_spread_lab.proof_runtime.pair_payload import _summary_block


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
        SimpleNamespace(trades=trades, ledger=ledger, daily_rows=[]), trades, 0.0, 0.0, 0.0
    )
    assert raw[-1] == pytest.approx(sum([0.004] * 3))
    assert sum(t["net_rub"] for t in trades) == 0.0
    assert ledger[-1]["cumulative_net_rub"] == 0.01
    assert summary["ledger_reconciliation_rub"] == -0.01
