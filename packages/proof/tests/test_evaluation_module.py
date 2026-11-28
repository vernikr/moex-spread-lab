"""Evaluation extraction keeps continuous-account and compatibility contracts."""

import math

import pytest

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING
from moex_spread_lab.core.settings import EvaluationSplit
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
