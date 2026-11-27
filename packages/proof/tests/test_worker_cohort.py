"""Worker cohort extraction preserves sticky state, ordering and retry rules."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

import pytest

from moex_spread_lab.proof import proof_pool, worker_cohort
from moex_spread_lab.proof.landscape import Axis
from moex_spread_lab.strategies.funding_carry.carry import CarryStrategyError


def test_worker_exports_and_carry_retry_keep_shared_state():
    for name in (
        "_WORKER_COHORTS",
        "_worker_init_cohort",
        "_worker_release_cohort",
        "_worker_eval_indices",
        "_worker_eval_plateau_batch",
        "_worker_report_cohort",
    ):
        assert getattr(proof_pool, name) is getattr(worker_cohort, name)
    native = {"strategy": "funding_only", "parameters": {"lookback": 99}}
    case = object()
    with (
        patch.object(
            worker_cohort,
            "get_strategy_axes",
            return_value={"funding_only": {"lookback": Axis(5, 30, 1)}},
        ),
        patch.object(
            worker_cohort, "StudyWindow", side_effect=[CarryStrategyError("history"), case]
        ) as constructor,
    ):
        assert (
            worker_cohort._prepare_worker_case(None, native, *cast(Any, (None, None, None))) is case
        )
    assert constructor.call_args_list[0].args[1]["parameters"] == {"lookback": 30}
    assert constructor.call_args_list[1].args[1]["parameters"] == {"lookback": 5}
    assert native["parameters"] == {"lookback": 99}
    with (
        patch.object(
            worker_cohort,
            "get_strategy_axes",
            return_value={"funding_only": {"lookback": Axis(5, 30, 1)}},
        ),
        patch.object(
            worker_cohort, "StudyWindow", side_effect=CarryStrategyError("history")
        ) as constructor,
        pytest.raises(CarryStrategyError),
    ):
        worker_cohort._prepare_worker_case(
            None, {**native, "parameters": {"lookback": 5}}, *cast(Any, (None, None, None))
        )
    assert constructor.call_count == 1


def test_worker_evaluation_is_sorted_and_stops_at_first_failure():
    first = SimpleNamespace(
        request={"strategy": "x"},
        fit=SimpleNamespace(measure=Mock(return_value={"rank_score": 2, "utility": 2})),
    )
    second = SimpleNamespace(
        request={"strategy": "x"},
        fit=SimpleNamespace(
            measure=Mock(return_value={"rank_score": None, "utility": None, "reason": "bad"})
        ),
    )
    worker_cohort._WORKER_COHORTS["test"] = cast(
        Any, {2: ({"pair": "B"}, second), 1: ({"pair": "A"}, first)}
    )
    try:
        points = {"x": {"window": 5}}
        expected = ((2, "B", "bad"), [(1, 2.0, True)])
        assert worker_cohort._worker_eval_indices("test", points) == expected
        assert worker_cohort._worker_eval_plateau_batch("test", [(7, points)]) == {7: expected}
        assert worker_cohort._worker_eval_indices("test", points, exclude_idx=2) == (
            None,
            [(1, 2.0, True)],
        )
    finally:
        worker_cohort._worker_release_cohort("test")
    assert "test" not in worker_cohort._WORKER_COHORTS
