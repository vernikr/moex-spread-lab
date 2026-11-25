"""Evaluation extraction keeps continuous-account and compatibility contracts."""

import math

import pytest

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING
from moex_spread_lab.core.settings import EvaluationSplit
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof import evaluation, proof_kernel, proof_search


def test_evaluation_exports_remain_compatible():
    for name in ("_annualized_returns", "_excess_curve", "account_checks"):
        assert getattr(proof_kernel, name) is getattr(evaluation, name)
        assert getattr(proof_search, name) is getattr(evaluation, name)


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
    result = evaluation._annualized_returns(proof, marks, split)
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
            evaluation._annualized_returns(proof, {**marks, days[1]: invalid}, split)
