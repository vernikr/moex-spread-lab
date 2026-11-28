"""Regression tests for issue #341 — proof/landscape Axis domain and grid bugs.

Covers:
* ``Axis.contains`` enforces the step grid for float axes (off-grid -> False);
* an ``int`` axis rejects non-integers and off-step integers;
* ``Axis.suggest`` routes integer parameters (including step > 1) through
  ``suggest_int`` with the correct step, not ``suggest_float``;
* ``validate`` rejects off-grid execution parameters such as ``slippage_ticks``;
* ``_intersect_axes`` raises a clean ``W0Error`` on disjoint member domains
  instead of building an inverted ``Axis``;
* the strategy-catalog derivative carries ``kind`` through to ``Axis``.
"""

from __future__ import annotations

import pytest

from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof.landscape import Axis
from moex_spread_lab.proof.request_validation import validate


class _FakeTrial:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def suggest_int(self, name: str, low: int, high: int, *, step: int = 1) -> int:
        self.calls.append(("int", name, low, high, step))
        return low

    def suggest_float(
        self, name: str, low: float, high: float, *, step: float | None = None
    ) -> float:
        self.calls.append(("float", name, low, high, step))
        return low


def test_contains_enforces_step_grid_on_float_axis() -> None:
    ax = Axis(0.0, 10.0, 0.5, kind="float")
    assert ax.contains(0.5) is True
    assert ax.contains(1.0) is True
    assert ax.contains(0.123) is False  # off-grid float
    assert ax.contains(1.537) is False  # off-grid float


def test_contains_int_kind_rejects_non_int_and_off_step() -> None:
    ax = Axis(2, 10, 2, kind="int")
    assert ax.contains(2) is True
    assert ax.contains(4) is True
    assert ax.contains(2.0) is False  # float on int axis
    assert ax.contains(3) is False  # off-step odd integer
    assert ax.contains(11) is False  # above high


def test_suggest_int_axis_with_step_gt_1_uses_suggest_int() -> None:
    ax = Axis(2, 10, 2, kind="int")
    trial = _FakeTrial()
    value = ax.suggest(trial, "lookback", 10)
    assert value == 2
    assert trial.calls == [("int", "lookback", 2, 10, 2)]


def test_suggest_float_axis_uses_suggest_float_with_step() -> None:
    ax = Axis(0.0, 2.0, 0.2, kind="float")
    trial = _FakeTrial()
    value = ax.suggest(trial, "entry_z", 2.0)
    assert value == 0.0
    assert trial.calls == [("float", "entry_z", 0.0, 2.0, 0.2)]


def test_validate_rejects_off_grid_slippage_ticks() -> None:
    with pytest.raises(W0Error):
        validate(
            {
                "universe": "pair_screen",
                "pair": "CNYRUBF/CRZ6",
                "strategy": "mean_reversion",
                "quantity": 2,
                "slippage_ticks": 0.123,
                "parameters": {"entry_z": 1.5, "exit_z": 0.3, "lookback": 20},
            }
        )


def test_intersect_axes_computes_overlap() -> None:
    from moex_spread_lab.proof.cohort_prepare import _intersect_axes

    axes = {"lookback": Axis(5, 30, 1)}
    _intersect_axes(axes, {"lookback": Axis(7, 40, 1)}, "mean_reversion")
    # [5, 30] ∩ [7, 40] == [7, 30]
    assert axes["lookback"] == Axis(7, 30, 1)


def test_intersect_axes_disjoint_raises_w0error() -> None:
    from moex_spread_lab.proof.cohort_prepare import _intersect_axes

    axes = {"lookback": Axis(5, 10, 1)}
    with pytest.raises(W0Error):
        _intersect_axes(axes, {"lookback": Axis(20, 30, 1)}, "mean_reversion")


def test_strategy_catalog_carries_kind() -> None:
    from moex_spread_lab.proof.strategy_catalog import get_strategy_axes

    axes = get_strategy_axes()
    assert "mean_reversion" in axes
    mr = axes["mean_reversion"]
    # Integer strategy parameters keep their int kind and stay integer-valued.
    assert mr["lookback"].kind == "int"
    assert mr["lookback"].is_integer is True
    # Float thresholds keep their float kind.
    assert mr["entry_z"].kind == "float"
    assert mr["entry_z"].is_integer is False
