"""Shared cohort preparation preserves ordering, intersections and exclusions."""

from types import SimpleNamespace

import pytest

from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof_runtime import cohort_prepare_helpers as helpers
from moex_spread_lab.proof_runtime import proof_batch
from moex_spread_lab.proof_runtime.axis import Axis


def test_cohort_request_helpers_remain_reexported():
    for name in ("MAX_TARGETS", "_targets", "_translate", "_native_request", "_active_seed"):
        assert getattr(proof_batch, name) is getattr(helpers, name)


def test_target_sorting_duplicates_and_strategy_translation():
    pair_a = {"universe": "pair_screen", "pair": "A/B"}
    pair_z = {"universe": "pair_screen", "pair": "Z/Y"}
    assert helpers._targets({"targets": [pair_z, pair_a]}) == [pair_a, pair_z]
    with pytest.raises(W0Error, match="дважды"):
        helpers._targets({"targets": [pair_a, pair_a]})
    assert helpers._translate({"window": 12, "entry_z": 1.5}, "mean_reversion") == {
        "lookback": 12,
        "entry_z": 1.5,
    }
    assert helpers._translate({"lookback": 9}, "zscore_mean_reversion") == {"window": 9}


def test_intersection_rejection_and_confirmation_preserve_denominator():
    axes = {"lookback": Axis(5, 50, 1)}
    helpers._intersect_axes(axes, {"window": Axis(7, 40, 1)}, "mean_reversion")
    assert axes["lookback"] == Axis(7, 40, 1)
    short_target, valid_target = {"pair": "A/B"}, {"pair": "C/D"}
    short = SimpleNamespace(split=SimpleNamespace(training=list(range(8))))
    valid = SimpleNamespace(split=SimpleNamespace(training=list(range(9))))
    rejected = [{"target": {"pair": "E/F"}, "reason": "earlier"}]
    members = helpers._filter_training(
        [(short_target, short), (valid_target, valid)], axes, rejected
    )
    assert members == [(valid_target, valid)]
    assert rejected[1] == {
        "target": short_target,
        "reason": "Для общего окна сигнала нужно не менее 9 наблюдений на обучении.",
    }
    settings = SimpleNamespace(metadata=lambda: {"validation_percent": 25})
    assert helpers._confirmation(settings, rejected, members) == {
        "status": "selection_requires_confirmation",
        "validation_percent": 25,
        "excluded": rejected,
        "remaining_count": 1,
        "trial_count": 0,
        "selected_parameters": None,
    }
