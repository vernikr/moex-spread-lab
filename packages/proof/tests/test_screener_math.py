"""Screener numeric checks fail closed for missing or non-finite scores."""

import math

import pytest

from moex_spread_lab.proof_runtime.screener_math import _annualized, _require_finite
from moex_spread_lab.proof_runtime.screener_search import _annualized as facade_annualized
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerError


def test_screener_finite_score_and_annualization_compatibility():
    assert _require_finite(-1.25) == -1.25
    for value in (None, math.nan, math.inf, -math.inf):
        with pytest.raises(ScreenerError, match="non-annualizable"):
            _require_finite(value)
    assert facade_annualized is _annualized
    assert (
        _annualized(
            [
                {"date": "2021-01-01", "V": 100, "B": 100},
                {"date": "2022-01-01", "V": 120, "B": 110},
            ],
            "2021-01-01",
            "2022-01-01",
        )
        == 10.0
    )
