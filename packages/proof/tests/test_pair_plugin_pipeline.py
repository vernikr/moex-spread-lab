"""Daily pair plugin pipeline characterization; this does not enable a proof adapter."""
from copy import deepcopy

import pytest

from moex_spread_lab.accounting.costs import tariff_profile
from moex_spread_lab.core.contracts import StrategyIntentRun, WindowSnapshot, CompatibilityVerdict
from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof_runtime.pair_plugin_accounting import size_daily_pair_plugin
from moex_spread_lab.strategies.funding_carry.proof_funding import FundingTimeline, FundingWindow
from moex_spread_lab.strategies.pair_mean_reversion.analytics import run_mean_reversion_baseline

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
        item = {"side": self.side, "entry_date": a["date"], "exit_date": b["date"],
                "entry_near_fill": a[f"near_{quote}"] + int(self.bad_price), "entry_far_fill": a[f"far_{quote}"],
                "exit_near_fill": b[f"near_{exit_quote}"], "exit_far_fill": b[f"far_{exit_quote}"],
                "reason": "end_of_sample" if self.terminal else "mean_reversion_exit",
                "signal": {"date": window.rows[signal_idx]["date"], "kind": "z", "value": -2 if self.side == "long_spread" else 2},
                "funding_rub": 1000000, "gross_pnl": 1000000, "quantity": 1000000}
        return StrategyIntentRun(self.strategy_id, fill, params, (item,))


def fixture():
    contracts = {s: {"secid": s, "tick_size": 1.0, "tick_value": 1.0,
                     "initial_margin": 2500, "lot_size": 10, "tariff_group": "stock"} for s in ("N", "F")}
    rows = [{"date": d, "near_open": 50000 + 10*i, "far_open": 50000,
             "near_close": 50001 + 10*i, "far_close": 50000, "near_volume": 300,
             "far_volume": 400, "spread_ticks": 1 + 10*i, "benchmark_price": 1.0,
             "benchmark_source_kind": "observed"} for i, d in enumerate(DAYS)]
    window = WindowSnapshot(universe="pair_screen", pair="N/F", near="N", far="F", rows=rows, contracts=contracts)
    rates = {d: .2 for d in DAYS}
    evidence = FundingWindow(FundingTimeline("TEST", rates, DAYS), DAYS)
    return window, evidence, rates


def pipeline(window, evidence, plugin, fill):
    return size_daily_pair_plugin(plugin, window, {}, fill, evidence,
                                  dict.fromkeys(DAYS, 1.0), ResearchSettings())


@pytest.mark.parametrize("fill", ["next_open", "next_close", "reference_same_bar"])
@pytest.mark.parametrize("side,z", [("long_spread", -2), ("short_spread", 2)])
@pytest.mark.parametrize("terminal", [False, True])
def test_validated_plugin_funding_and_sizing_reconcile_to_legacy(fill, side, z, terminal):
    window, evidence, rates = fixture()
    original = deepcopy(window)
    result = pipeline(window, evidence, PairPlugin(side, terminal), fill)
    assert window == original and len(result["trades"]) == 1
    actual = result["trades"][0]
    delay = 0 if fill == "reference_same_bar" else 1
    baseline = run_mean_reversion_baseline(
        {"rows": window.rows, "contracts": window.contracts, "near": "N", "far": "F", "provenance": {}},
        initial_capital=30000, lookback=2, entry_z=1.5, exit_z=.5, quantity=actual["quantity"],
        fill="open" if fill == "next_open" else "close", decision_delay_bars=delay,
        analysis_role="diagnostic_only", cost_profile=tariff_profile(1.0), cash_in_lqdt=True,
        funding_swaprates=rates, precomputed_z=[None, None, 0, z, z if terminal else 0])
    expected = baseline["trades"][0]
    assert actual["funding_rub"] == pytest.approx(expected["funding_rub"], abs=.02)
    assert actual["gross_rub"] == pytest.approx(expected["gross_pnl"], abs=.02)
    assert actual["costs_rub"] == pytest.approx(expected["modeled_costs"], abs=.02)
    assert actual["net_rub"] == pytest.approx(expected["net_pnl"], abs=.02)
    assert result["summary"]["ledger_reconciliation_rub"] == 0
    assert [row["funding_rub"] for row in result["ledger"]] == pytest.approx(
        [row["funding_rub"] for row in baseline["daily"]], abs=.02)
    assert result["ledger"][-1]["account_value_rub"] == pytest.approx(
        baseline["summary"]["final_strategy_value"], abs=.02)
    assert actual["open_at_end"] == terminal


def test_plugin_fictional_fill_and_missing_evidence_fail_before_sizing():
    window, evidence, _ = fixture()
    with pytest.raises(W0Error):
        pipeline(window, evidence, PairPlugin("long_spread", False, bad_price=True), "next_open")
    missing = FundingWindow(FundingTimeline("TEST", {d: None for d in DAYS}, DAYS), DAYS)
    with pytest.raises(W0Error):
        pipeline(window, missing, PairPlugin("long_spread", False), "next_open")


def test_plugin_economic_claims_do_not_change_result_or_run_id():
    from dataclasses import replace

    class FalseClaims(PairPlugin):
        def evaluate_intents(self, window, params, fill):
            run = super().evaluate_intents(window, params, fill)
            item = {**run.intents[0], "funding_rub": -999999, "gross_pnl": -999999,
                    "quantity": 99999, "costs": {"broker": 0}}
            return replace(run, intents=(item,))

    window, evidence, _ = fixture()
    first = pipeline(window, evidence, PairPlugin("long_spread", True), "next_open")
    second = pipeline(window, evidence, FalseClaims("long_spread", True), "next_open")
    assert first == second


def test_accounting_rejects_unsafe_inputs_and_no_funding_inference():
    from dataclasses import replace
    window, evidence, _ = fixture()
    plugin = PairPlugin("long_spread", False)
    settings = ResearchSettings()
    for changed in (replace(window, timeframe="1h"), replace(window, far="N"),
                    replace(window, rows=[{**r, "near_close": float("nan")} for r in window.rows])):
        with pytest.raises(W0Error):
            size_daily_pair_plugin(plugin, changed, {}, "next_open", evidence, dict.fromkeys(DAYS, 1.0), settings)
    with pytest.raises(W0Error):
        size_daily_pair_plugin(plugin, window, {}, "next_open", None, dict.fromkeys(DAYS, 1.0), settings)
    with pytest.raises(W0Error):
        size_daily_pair_plugin(plugin, window, {}, "next_open", evidence, {}, settings)


def test_incompatible_plugin_is_rejected_before_intent_evaluation():
    class Incompatible(PairPlugin):
        def check_compatibility(self, window, settings):
            return CompatibilityVerdict(False, "not compatible")
        def evaluate_intents(self, *args):
            raise AssertionError("incompatible plugin must not be called")
    window, evidence, _ = fixture()
    with pytest.raises(W0Error, match="not compatible"):
        pipeline(window, evidence, Incompatible("long_spread", False), "next_open")
