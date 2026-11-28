"""Evaluation extraction keeps continuous-account and compatibility contracts."""

import math

import pytest

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING
from moex_spread_lab.core.settings import EvaluationSplit, ResearchSettings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof import evaluation, proof_kernel, proof_search


def test_evaluation_exports_remain_compatible():
    for name in ("_annualized_returns", "_excess_curve", "account_checks"):
        assert getattr(proof_kernel, name) is getattr(evaluation, name.lstrip("_"))
        assert getattr(proof_search, name) is getattr(evaluation, name.lstrip("_"))


def test_validation_uses_continuous_automatic_account_and_reports_shortfall():
    days = ["2021-01-01", "2022-01-01", "2023-01-01"]
    proof = {
        "selection": {"sizing": AUTO_SIZING},
        "sizing": {"initial_capital_rub": 100},
        "ledger": [
            {
                "date": day,
                "account_value_rub": value,
                "cash_shortfall": shortfall,
                "free_cash_rub": free,
            }
            for day, value, shortfall, free in zip(
                days, [100, 120, 132], [False, True, False], [20, -5, 15], strict=True
            )
        ],
        "trades": [],
    }
    marks = dict(zip(days, [100.0, 110.0, 121.0], strict=True))
    split = EvaluationSplit.from_dates(days, 25)
    result = evaluation.annualized_returns(proof, marks, split)
    assert result["calendar_days"] == 365
    assert result["strategy_annualized_pct"] == pytest.approx(10)
    assert result["lqdt_annualized_pct"] == pytest.approx(10)
    assert result["annualized_excess_pp"] == 0
    assert result["cash_shortfall"] is True
    assert evaluation.account_checks(proof) == {
        "cash_shortfall_rows": 1,
        "first_cash_shortfall_date": days[1],
        "minimum_free_cash_rub": -5,
    }
    for invalid in (0, math.inf, math.nan):
        with pytest.raises(W0Error, match="LQDT"):
            evaluation.annualized_returns(proof, {**marks, days[1]: invalid}, split)


def test_idle_lqdt_only_ledger_starting_before_training_scores_exactly_zero():
    # The account compounds in LQDT from ledger[0] while training starts later
    # (journal-only leading rows); both legs must open at the same anchor, so
    # an idle book scores 0.0 excess without a validation split (issue #303).
    initial = 30000.0
    days = [f"2026-01-{day:02}" for day in range(1, 9)]
    marks = {day: 1.001**i for i, day in enumerate(days)}
    proof = {
        "selection": {"sizing": AUTO_SIZING},
        "sizing": {"initial_capital_rub": initial},
        "ledger": [
            {"date": day, "account_value_rub": initial * marks[day], "cash_shortfall": False}
            for day in days
        ],
        "trades": [],
    }
    split = EvaluationSplit.from_dates(days[4:], 0)
    assert split.training[0] == days[4] and not split.validation
    result = evaluation.annualized_returns(proof, marks, split)
    assert result["strategy_annualized_pct"] == pytest.approx(result["lqdt_annualized_pct"])
    assert result["annualized_excess_pp"] == 0.0
    assert evaluation.excess_curve(proof, marks)[days[-1]] == 0.0


def test_fixed_sizing_parity_with_automatic_when_ledger_starts_before_training():
    # Issue #342: the fixed-size wealth path must walk the full ledger from
    # ledger[0] (compounding LQDT on leading zero-volume rows) so a 1-lot fixed
    # run and an identical max_affordable run report the same annualized return.
    initial = 2500.0
    months = [f"2026-{m:02d}-01" for m in range(1, 9)]
    marks = {
        months[0]: 1.0,
        months[1]: 1.05,
        months[2]: 1.10,
        months[3]: 1.15,
        months[4]: 1.20,
        months[5]: 1.20,
        months[6]: 1.20,
        months[7]: 1.20,
    }
    # account_value_rub is the LQDT overlay of free cash + posted margin.
    account = [2500, 2625, 2750, 2875, 3000, 3200, 3200, 3200]
    cumulative_net = [0, 0, 0, 0, 0, 200, 200, 200]
    ledger = [
        {"date": d, "account_value_rub": a, "cumulative_net_rub": c, "cash_shortfall": False}
        for d, a, c in zip(months, account, cumulative_net, strict=True)
    ]
    trades = [
        {
            "entry": {"date": months[4]},
            "exit": {"date": months[5]},
            "margin_rub": 1000.0,
            "lqdt_on_margin_rub": 0.0,
        }
    ]
    split = EvaluationSplit.from_dates(months[4:], 0)

    fixed = {
        "selection": {"sizing": "fixed"},
        "sizing": {"initial_capital_rub": initial},
        "ledger": ledger,
        "trades": trades,
    }
    auto = {
        "selection": {"sizing": AUTO_SIZING},
        "sizing": {"initial_capital_rub": initial},
        "ledger": ledger,
        "trades": trades,
    }

    rf = evaluation.annualized_returns(fixed, marks, split)
    ra = evaluation.annualized_returns(auto, marks, split)
    assert rf["strategy_annualized_pct"] == pytest.approx(ra["strategy_annualized_pct"])
    assert rf["annualized_excess_pp"] == pytest.approx(ra["annualized_excess_pp"])
    assert rf["annualized_excess_pp"] == pytest.approx(29.181676, rel=1e-6)


def test_auto_sizing_lqdt_on_margin_reflects_margin_hurdle_not_reinvested_profit():
    # Issue #343: when realized profit compounds in LQDT after a trade closes,
    # excess_rub exceeds net_rub; lqdt_on_margin_rub must report the actual
    # opportunity cost on posted margin (never negative net - excess).
    days = [f"2026-01-{d:02d}" for d in range(1, 7)]
    # Trade open d1 -> d2 (LQDT 1.00 -> 1.02 on 1000 margin => 20.0 hurdle, +500 net);
    # flat d3..d5 while LQDT grows 1.02 -> 1.224 (+20%), compounding free cash.
    marks = {
        days[0]: 1.00,
        days[1]: 1.00,
        days[2]: 1.02,
        days[3]: 1.10,
        days[4]: 1.15,
        days[5]: 1.224,
    }
    # initial=10000: d0=10000; d1 enter margin=1000 free=9000 av=10000;
    # d2 free grows 9000*1.02=9180 + margin 1000 + net 500 = 10680 (exit -> all free);
    # d3..d5 compound 10680 by marks[d]/1.02 -> d5 = 10680 * 1.2 = 12816.
    account = [10000.0, 10000.0, 10680.0, 11517.6470588, 12041.1764706, 12816.0]
    cum_net = [0.0, 0.0, 500.0, 500.0, 500.0, 500.0]
    proof = {
        "selection": {"sizing": AUTO_SIZING, "strategy": "mean_reversion"},
        "window": {"passes_capital_screen": True, "tradable_every_day": True},
        "instrument": {"family": "quarterly_spread"},
        "provenance": {},
        "sizing": {"initial_capital_rub": 10000.0},
        "ledger": [
            {"date": d, "account_value_rub": a, "cumulative_net_rub": c, "cash_shortfall": False}
            for d, a, c in zip(days, account, cum_net, strict=True)
        ],
        "trades": [
            {
                "entry": {"date": days[1]},
                "exit": {"date": days[2]},
                "margin_rub": 1000.0,
                "lqdt_on_margin_rub": 20.0,
            }
        ],
    }
    full = proof_kernel.measure(
        proof, marks, EvaluationSplit.from_dates(days, 0), ResearchSettings()
    )
    assert full["net_rub"] == 500.0
    assert full["excess_rub"] == 576.0  # 12816 - 10000 * 1.224 > net_rub!
    assert full["lqdt_on_margin_rub"] == 20.0

    # Validation split (days[3:] has zero trades and zero open positions):
    # validation excess_rub and lqdt_on_margin_rub must both be 0.0.
    val = proof_kernel.measure(
        proof, marks, EvaluationSplit.from_dates(days, 50), ResearchSettings()
    )
    assert val["net_rub"] == 0.0
    assert val["lqdt_on_margin_rub"] == 0.0
    assert val["excess_rub"] == 0.0
    assert val["validation"] == 0.0
    assert val["rub_per_session"] == 0.0
    assert val["annualized_excess_pp"] == 0.0


def test_measure_scalar_train_anchors_at_training_start_and_uses_margin_hurdle():
    # Issue #343: scalar training path must anchor excess_rub at training[0]
    # (not initial/p0) and report _scalar_train_hurdle (not net - excess).
    start, end = "2026-02-01", "2026-03-01"
    marks = {start: 1.10, end: 1.21}
    proof = {
        "selection": {"sizing": AUTO_SIZING, "strategy": "funding_only"},
        "window": {"passes_capital_screen": True, "tradable_every_day": True},
        "_scalar_train_summary": (
            30000.0,  # initial
            1.0,  # p0 at ledger[0]
            marks[end],
            39050.0,  # end_wealth = 35000 * 1.1 + 550 excess
            500.0,  # end_cum_net
            35000.0,  # start_wealth at training[0]
            False,
            True,
        ),
        "_scalar_train_open_wealth": 35000.0,
        "_scalar_train_hurdle": 12.34,
    }
    measured = proof_kernel.measure(
        proof, marks, EvaluationSplit.from_dates([start, end], 0), ResearchSettings()
    )
    assert measured["net_rub"] == 500.0
    assert measured["excess_rub"] == 550.0
    assert measured["lqdt_on_margin_rub"] == 12.34
