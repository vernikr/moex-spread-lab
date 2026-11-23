"""Synthetic pair-plugin environment for the P7.2 pipeline characterization.

Builders moved out of ``test_pair_plugin_pipeline.py`` so that file meets the
code-health size limits. This module only builds a window, its funding evidence
and a scripted plugin; it holds no assertion and no numeric expectation.
"""

from __future__ import annotations

from moex_spread_lab.core.contracts import CompatibilityVerdict, StrategyIntentRun, WindowSnapshot
from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.proof_runtime.pair_plugin_accounting import size_daily_pair_plugin
from moex_spread_lab.strategies.funding_carry.proof_funding import FundingTimeline, FundingWindow

DAYS = [f"2026-01-0{i}" for i in range(1, 6)]


class PairPlugin:
    strategy_id = "pair_test_plugin"
    supported_fills = ("next_open", "next_close", "reference_same_bar")

    def validate_parameters(self, params, context):
        return dict(params)

    def check_compatibility(self, window, settings):
        return CompatibilityVerdict(True)

    def __init__(self, side, terminal, bad_price=False):
        self.side, self.terminal, self.bad_price = side, terminal, bad_price

    def evaluate_intents(self, window, params, fill):
        quote = "open" if fill == "next_open" else "close"
        a, b = window.rows[3], window.rows[4]
        exit_quote = "close" if self.terminal else quote
        signal_idx = 3 if fill == "reference_same_bar" else 2
        item = {
            "side": self.side,
            "entry_date": a["date"],
            "exit_date": b["date"],
            "entry_near_fill": a[f"near_{quote}"] + int(self.bad_price),
            "entry_far_fill": a[f"far_{quote}"],
            "exit_near_fill": b[f"near_{exit_quote}"],
            "exit_far_fill": b[f"far_{exit_quote}"],
            "reason": "end_of_sample" if self.terminal else "mean_reversion_exit",
            "signal": {
                "date": window.rows[signal_idx]["date"],
                "kind": "z",
                "value": -2 if self.side == "long_spread" else 2,
            },
            "funding_rub": 1000000,
            "gross_pnl": 1000000,
            "quantity": 1000000,
        }
        return StrategyIntentRun(self.strategy_id, fill, params, (item,))


def fixture():
    contracts = {
        s: {
            "secid": s,
            "tick_size": 1.0,
            "tick_value": 1.0,
            "initial_margin": 2500,
            "lot_size": 10,
            "tariff_group": "stock",
        }
        for s in ("N", "F")
    }
    rows = [
        {
            "date": d,
            "near_open": 50000 + 10 * i,
            "far_open": 50000,
            "near_close": 50001 + 10 * i,
            "far_close": 50000,
            "near_volume": 300,
            "far_volume": 400,
            "spread_ticks": 1 + 10 * i,
            "benchmark_price": 1.0,
            "benchmark_source_kind": "observed",
        }
        for i, d in enumerate(DAYS)
    ]
    window = WindowSnapshot(
        universe="pair_screen", pair="N/F", near="N", far="F", rows=rows, contracts=contracts
    )
    rates = dict.fromkeys(DAYS, 0.2)
    evidence = FundingWindow(FundingTimeline("N", rates, DAYS), DAYS)
    return window, evidence, rates


def pipeline(window, evidence, plugin, fill):
    return size_daily_pair_plugin(
        plugin, window, {}, fill, evidence, dict.fromkeys(DAYS, 1.0), ResearchSettings()
    )
