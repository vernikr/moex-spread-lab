"""Daily pair plugin pipeline characterization; this does not enable a proof adapter."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest

from moex_spread_lab.accounting.costs import tariff_profile
from moex_spread_lab.accounting.position_sizing import size_proof
from moex_spread_lab.core.contracts import (
    CompatibilityVerdict,
    EntryBandPoint,
    InstrumentContext,
    ParameterSpec,
    StrategyIntentRun,
    WindowSnapshot,
)
from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof.fixed_plugin_payload import _summary_update, fixed_pair_accounting
from moex_spread_lab.proof.pair_plugin_accounting import (
    prepare_daily_pair_unit,
)
from moex_spread_lab.strategies.funding_carry.proof_funding import FundingTimeline, FundingWindow
from moex_spread_lab.strategies.pair_mean_reversion.analytics import run_mean_reversion_baseline

DAYS = [f"2026-01-0{i}" for i in range(1, 6)]


class PairPlugin:
    """Minimal ``StrategyPlugin`` double: only the members the pipeline touches."""

    strategy_id: str = "pair_test_plugin"
    family_id: str = "pair_test"
    label_ru: str = "Тестовый парный плагин"
    short_label_ru: str = "Тест"
    supported_universes: frozenset[str] = frozenset({"pair_screen"})
    supported_fills: tuple[str, ...] = ("next_open", "next_close", "reference_same_bar")
    requires_daily_bars: bool = True

    def parameter_schema(
        self, context: InstrumentContext | None = None
    ) -> tuple[ParameterSpec, ...]:
        return ()

    def validate_parameters(
        self, raw: Mapping[str, Any], context: InstrumentContext | None = None
    ) -> dict[str, Any]:
        return dict(raw)

    def check_compatibility(
        self, window: WindowSnapshot, settings: ResearchSettings | None = None
    ) -> CompatibilityVerdict:
        return CompatibilityVerdict(True)

    def compute_entry_bands(
        self,
        series: list[dict[str, Any]],
        params: Mapping[str, Any],
        fill: str,
        *,
        far_point_value_rub: float = 1.0,
    ) -> list[EntryBandPoint]:
        del far_point_value_rub
        return []

    def __init__(self, side: str, terminal: bool, bad_price: bool = False) -> None:
        self.side, self.terminal, self.bad_price = side, terminal, bad_price

    def evaluate_intents(
        self, window: WindowSnapshot, params: Mapping[str, Any], fill: str
    ) -> StrategyIntentRun:
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
        return StrategyIntentRun(self.strategy_id, fill, dict(params), (item,))


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


def sized_unit(plugin, window, params, fill, evidence, marks, settings, **options):
    unit, used_marks = prepare_daily_pair_unit(
        plugin, window, params, fill, evidence, marks, settings, **options
    )
    return size_proof(unit, used_marks, settings, window.contracts)


def pipeline(window, evidence, plugin, fill):
    return sized_unit(
        plugin, window, {}, fill, evidence, dict.fromkeys(DAYS, 1.0), ResearchSettings()
    )


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
        {
            "rows": window.rows,
            "contracts": window.contracts,
            "near": "N",
            "far": "F",
            "provenance": {},
        },
        initial_capital=30000,
        lookback=2,
        entry_z=1.5,
        exit_z=0.5,
        quantity=actual["quantity"],
        fill="open" if fill == "next_open" else "close",
        decision_delay_bars=delay,
        analysis_role="diagnostic_only",
        cost_profile=tariff_profile(1.0),
        cash_in_lqdt=True,
        funding_swaprates=rates,
        precomputed_z=[None, None, 0, z, z if terminal else 0],
    )
    expected = baseline["trades"][0]
    assert actual["funding_rub"] == pytest.approx(expected["funding_rub"], abs=0.02)
    assert actual["gross_rub"] == pytest.approx(expected["gross_pnl"], abs=0.02)
    assert actual["costs_rub"] == pytest.approx(expected["modeled_costs"], abs=0.02)
    assert actual["net_rub"] == pytest.approx(expected["net_pnl"], abs=0.02)
    assert result["summary"]["ledger_reconciliation_rub"] == 0
    assert [row["funding_rub"] for row in result["ledger"]] == pytest.approx(
        [row["funding_rub"] for row in baseline["daily"]], abs=0.02
    )
    assert result["ledger"][-1]["account_value_rub"] == pytest.approx(
        baseline["summary"]["final_strategy_value"], abs=0.02
    )
    assert actual["open_at_end"] == terminal


def test_plugin_fictional_fill_and_missing_evidence_fail_before_sizing():
    window, evidence, _ = fixture()
    with pytest.raises(W0Error):
        pipeline(window, evidence, PairPlugin("long_spread", False, bad_price=True), "next_open")
    missing = FundingWindow(FundingTimeline("N", dict.fromkeys(DAYS), DAYS), DAYS)
    with pytest.raises(W0Error):
        pipeline(window, missing, PairPlugin("long_spread", False), "next_open")


def test_plugin_economic_claims_do_not_change_result_or_run_id():
    class FalseClaims(PairPlugin):
        def evaluate_intents(self, window, params, fill):
            run = super().evaluate_intents(window, params, fill)
            item = {
                **run.intents[0],
                "funding_rub": -999999,
                "gross_pnl": -999999,
                "quantity": 99999,
                "costs": {"broker": 0},
            }
            return replace(run, intents=(item,))

    window, evidence, _ = fixture()
    first = pipeline(window, evidence, PairPlugin("long_spread", True), "next_open")
    second = pipeline(window, evidence, FalseClaims("long_spread", True), "next_open")
    assert first == second


def test_accounting_rejects_unsafe_inputs_and_no_funding_inference():
    window, evidence, _ = fixture()
    plugin = PairPlugin("long_spread", False)
    settings = ResearchSettings()
    for changed in (
        replace(window, timeframe="1h"),
        replace(window, far="N"),
        replace(window, rows=[{**r, "near_close": float("nan")} for r in window.rows]),
    ):
        with pytest.raises(W0Error):
            sized_unit(
                plugin, changed, {}, "next_open", evidence, dict.fromkeys(DAYS, 1.0), settings
            )
    with pytest.raises(W0Error):
        sized_unit(plugin, window, {}, "next_open", None, dict.fromkeys(DAYS, 1.0), settings)
    with pytest.raises(W0Error):
        sized_unit(plugin, window, {}, "next_open", evidence, {}, settings)


def test_incompatible_plugin_is_rejected_before_intent_evaluation():
    class Incompatible(PairPlugin):
        def check_compatibility(self, window, settings=None):
            return CompatibilityVerdict(False, "not compatible")

        def evaluate_intents(self, window, params, fill):
            raise AssertionError("incompatible plugin must not be called")

    window, evidence, _ = fixture()
    with pytest.raises(W0Error, match="not compatible"):
        pipeline(window, evidence, Incompatible("long_spread", False), "next_open")


def test_journal_only_bar_does_not_revalue_or_receive_funding():
    window, _, _ = fixture()
    rows = deepcopy(window.rows)
    rows[2].update(near_volume=0, near_close=90000)
    window = replace(window, rows=rows)
    days = [d for d in DAYS if d != DAYS[2]]
    evidence = FundingWindow(FundingTimeline("N", dict.fromkeys(days, 0.2), days), days)

    class AcrossGap(PairPlugin):
        def evaluate_intents(self, window, params, fill):
            assert [r["date"] for r in window.rows] == days
            a, b = window.rows[1], window.rows[3]
            item = {
                "side": "long_spread",
                "entry_date": a["date"],
                "exit_date": b["date"],
                "entry_near_fill": a["near_open"],
                "entry_far_fill": a["far_open"],
                "exit_near_fill": b["near_open"],
                "exit_far_fill": b["far_open"],
                "reason": "exit",
                "signal": {"date": days[0], "kind": "x", "value": 1},
            }
            return StrategyIntentRun(self.strategy_id, fill, dict(params), (item,))

    result = pipeline(window, evidence, AcrossGap("long_spread", False), "next_open")
    journal = result["ledger"][2]
    assert result["series"][2]["volume_eligible"] is False
    assert journal["mark_pnl_rub"] == 0 and journal["funding_rub"] is None
    assert journal["posted_margin_rub"] == result["ledger"][1]["posted_margin_rub"]
    assert result["summary"]["ledger_reconciliation_rub"] == 0


def test_prefix_hides_future_rows_and_extra_snapshot_data_and_identity():
    window, evidence, _ = fixture()
    window = replace(
        window,
        funding_swaprates={DAYS[-1]: 123},
        benchmark={"future": 2},
        carry_study={"future": 3},
        extra={"future": 4},
    )

    class PrefixPlugin(PairPlugin):
        def evaluate_intents(self, window, params, fill):
            assert [r["date"] for r in window.rows] == DAYS[:4]
            assert window.funding_swaprates is None and window.carry_study is None
            assert window.benchmark is None and not window.extra
            return StrategyIntentRun(self.strategy_id, fill, dict(params), ())

    plugin = PrefixPlugin("long_spread", False)
    first = sized_unit(
        plugin,
        window,
        {},
        "next_open",
        evidence,
        dict.fromkeys(DAYS, 1.0),
        ResearchSettings(),
        through=DAYS[3],
    )
    altered = deepcopy(window.rows)
    altered[-1]["near_close"] = 123456
    second = sized_unit(
        plugin,
        replace(window, rows=altered),
        {},
        "next_open",
        evidence,
        {**dict.fromkeys(DAYS, 1.0), DAYS[-1]: 999},
        ResearchSettings(),
        through=DAYS[3],
    )
    assert first == second
    assert [r["date"] for r in first["series"]] == DAYS[:4]


def test_dated_pair_without_funding_does_not_invent_zero_rate():
    window, _, _ = fixture()
    contracts = {
        "GLZ6": {**window.contracts["N"], "secid": "GLZ6"},
        "GLH7": {**window.contracts["F"], "secid": "GLH7"},
    }
    dated = replace(window, pair="GLZ6/GLH7", near="GLZ6", far="GLH7", contracts=contracts)
    result = sized_unit(
        PairPlugin("long_spread", False),
        dated,
        {},
        "next_open",
        None,
        dict.fromkeys(DAYS, 1.0),
        ResearchSettings(),
    )
    assert result["provenance"]["funding_applicability"] == "no_perpetual_leg"
    assert all(
        row["funding_rub"] is None and row["funding_contracts"] == 0 for row in result["series"]
    )
    assert result["summary"]["ledger_reconciliation_rub"] == 0
    wrong_funding = FundingWindow(FundingTimeline("GLZ6", dict.fromkeys(DAYS, 0.2), DAYS), DAYS)
    with pytest.raises(W0Error, match="неприменима"):
        sized_unit(
            PairPlugin("long_spread", False),
            dated,
            {},
            "next_open",
            wrong_funding,
            dict.fromkeys(DAYS, 1.0),
            ResearchSettings(),
        )
    with pytest.raises(W0Error):
        sized_unit(
            PairPlugin("long_spread", False),
            replace(dated, pair="CNYRUBF/CRZ6"),
            {},
            "next_open",
            None,
            dict.fromkeys(DAYS, 1.0),
            ResearchSettings(),
        )


def test_funding_history_must_match_near_contract():
    window, _, _ = fixture()
    other = FundingWindow(FundingTimeline("OTHER", dict.fromkeys(DAYS, 0.2), DAYS), DAYS)
    with pytest.raises(W0Error, match="ближней"):
        pipeline(window, other, PairPlugin("long_spread", True), "next_open")


@pytest.mark.parametrize("far_mult", [1.0, 3.0])
@pytest.mark.parametrize("side", ["long_spread", "short_spread"])
@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("fill", ["next_open", "next_close", "reference_same_bar"])
def test_fixed_pair_curve_uses_each_leg_multiplier(far_mult, side, terminal, fill):
    window, evidence, _ = fixture()
    window.contracts["F"]["tick_value"] = far_mult
    window.rows[3].update(far_close=50010)
    window.rows[4].update(far_open=49980, far_close=50050)
    unit, _ = prepare_daily_pair_unit(
        PairPlugin(side, terminal),
        window,
        {},
        fill,
        evidence,
        dict.fromkeys(DAYS, 1.0),
        ResearchSettings(),
    )
    rows, trades, ledger, drawdown, totals = fixed_pair_accounting(
        unit["series"],
        unit,
        (
            {"contracts": window.contracts, "provenance": {}},
            {"near": "N", "far": "F"},
            unit["provenance"],
        ),
        {"quantity": 2, "slippage_ticks": 1.0},
        (DAYS, dict.fromkeys(DAYS, 1.0)),
        30000,
        builtin=False,
    )
    trade = trades[0]
    entry, exit_ = trade["entry"], trade["exit"]
    sign = 1 if side == "long_spread" else -1
    entry_cost = sum(entry["costs"].values())
    exit_cost = sum(exit_["costs"].values())

    def price_pnl(near, far):
        return sign * 2 * ((near - entry["near_price"]) - (far - entry["far_price"]) * far_mult)

    first_pnl = (
        price_pnl(window.rows[3]["near_close"], window.rows[3]["far_close"])
        - entry_cost
        + rows[3]["funding_rub"]
    )
    final_pnl = (
        price_pnl(exit_["near_price"], exit_["far_price"])
        - entry_cost
        - exit_cost
        + sum(row["funding_rub"] for row in rows)
    )
    expected_drawdown = max(0, -first_pnl, max(0, first_pnl) - final_pnl)
    assert drawdown == pytest.approx(expected_drawdown, abs=1e-7)
    assert totals[2] == pytest.approx(final_pnl, abs=1e-7)
    assert ledger[-1]["cumulative_net_rub"] == round(final_pnl, 2)
    assert trade["net_rub"] == round(final_pnl, 2)
    template: dict[str, Any] = {
        "summary": dict.fromkeys(("gross_rub", "costs_rub", "net_rub"), -999)
    }
    _summary_update(template, trades, ledger, drawdown, totals)
    summary = template["summary"]
    assert [summary[key] for key in ("gross_rub", "costs_rub", "net_rub")] == [
        round(value, 2) for value in totals
    ]
    assert summary["ledger_reconciliation_rub"] == 0
    assert summary["max_excess_drawdown_rub"] == round(expected_drawdown, 2)
