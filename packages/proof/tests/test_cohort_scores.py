"""Canonical aggregation and adaptive canary regression cases, plus the remote
cohort plateau prewarm/lifecycle contracts that feed them."""

import importlib
from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from moex_spread_lab.proof import cohort_models, proof_batch, proof_pool
from moex_spread_lab.proof.landscape import Axis, Landscape
from moex_spread_lab.proof.proof_pool import CohortWorkerPool


class ImmediateSlot:
    def __init__(self, handler):
        self.handler = handler

    def submit(self, fn, *args):
        future = Future()
        try:
            future.set_result(self.handler(fn.__name__, *args))
        except BaseException as exc:  # noqa: BLE001 - executor futures capture even BaseException.
            future.set_exception(exc)
        return future


def test_aggregation_preserves_canonical_order_failure_and_inactive_utility():
    failure, scores = proof_pool.combine_shards(
        [
            ((3, "D", "later"), [(2, 3.0, False)]),
            ((1, "B", "first"), [(0, 1.0, False)]),
        ]
    )
    assert failure is not None
    assert failure == (1, "B", "first")
    assert proof_pool.failure_payload(failure)["reason"] == "B: first"
    assert proof_pool.score_payload(scores) == {
        "rank_score": 2.0,
        "by_window_pp": [1.0, 3.0],
        "utility": None,
        "reason": "Открытого спреда не было на обучении.",
    }


def _canary_handler(slot, phase):
    def run(name, cohort_id, point, indices=None, exclude=None):
        assert name == "worker_eval_indices" and cohort_id == "cohort"
        if phase[0] == 0:
            return (
                ((2, "C", "initial"), [(0, 1.0, True)])
                if slot == 0
                else ((3, "D", "later"), [(1, 2.0, True)])
            )
        if phase[0] == 1:
            if indices == [2]:
                return (2, "C", "canary"), []
            return ((1, "B", "earliest"), []) if slot == 1 else (None, [(0, 1.0, True)])
        if indices == [1]:
            return None, [(1, 2.0, True)]
        assert exclude == 1
        return (None, [(0, 1.0, True), (2, 3.0, True)]) if slot == 0 else (None, [(3, 4.0, True)])

    return run


def test_canary_switches_and_selects_earliest_failure():
    phase = [0]
    state = {
        "cohort_id": "cohort",
        "slots": [
            ImmediateSlot(_canary_handler(0, phase)),
            ImmediateSlot(_canary_handler(1, phase)),
        ],
        "owner_of": {0: 0, 1: 1, 2: 0, 3: 1},
        "shards": [[(0,), (2,)], [(1,), (3,)]],
    }
    canary = [0]
    pool = CohortWorkerPool(max_workers=2)
    try:
        result = pool.eval_point(state, {}, canary, 4, lambda: None)
        assert result["reason"] == "C: initial" and canary == [2] and state["canary_active"]
        phase[0] = 1
        result = pool.eval_point(state, {}, canary, 4, lambda: None)
        assert result["reason"] == "B: earliest" and canary == [1] and state["canary_active"]
        phase[0] = 2
        result = pool.eval_point(state, {}, canary, 4, lambda: None)
        assert result == {"rank_score": 2.5, "by_window_pp": [1.0, 2.0, 3.0, 4.0], "utility": 2.5}
        assert state["canary_active"] is False
    finally:
        pool.shutdown()


def test_await_futures_cancels_pending_when_check_raises():
    futures = [Future(), Future()]
    with pytest.raises(RuntimeError, match="cancelled"):
        proof_pool.await_futures(futures, Mock(side_effect=RuntimeError("cancelled")))
    for future in futures:
        assert future.cancelled()


@pytest.mark.parametrize("module", ["pool_reports", "worker_window"])
def test_retired_pool_shards_stay_deleted(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f"moex_spread_lab.proof.{module}")


# ---- remote cohort prewarm and lifecycle (folded into the scores suite) ----


def test_remote_plateau_prewarm_deduplicates_and_release_is_idempotent():
    axes = {"lookback": Axis(3, 7, 1)}
    land = Landscape(axes, lambda _: {"utility": 10.0})
    case = SimpleNamespace(request={"strategy": "mean_reversion"}, lamp_warning=lambda: None)
    pool = Mock()

    def batch(state, payload, check_cancelled, *, on_progress):
        assert [p[1]["mean_reversion"]["lookback"] for p in payload] == [5, 3, 4, 6, 7]
        on_progress(1, 1)
        return {i: {"utility": 10.0} for i, _ in payload}

    pool.eval_plateau_batch.side_effect = batch
    state = {"cohort_id": "id", "owner_of": {0: 0}}
    group = cohort_models.RemoteCohort(
        active="mean_reversion",
        axes=axes,
        seed={"lookback": 5},
        members=[({}, cast(Any, case))],
        land=land,
        pool=pool,
        cohort_state=state,
        distinct_strategies=("mean_reversion",),
        check_cancelled=lambda: None,
    )
    events = []
    result = group.plateau(
        {"lookback": 5}, on_progress=lambda done, total: events.append((done, total))
    )
    assert result["fields"]["lookback"]["score"] == 100.0
    assert events == [(1, 1)] and len(land.cache) == 5
    assert group.plateau({"lookback": 5}) == result
    assert pool.eval_plateau_batch.call_count == 1
    group.close()
    group.close()
    pool.release_cohort.assert_called_once_with("id", {0: 0})
    assert proof_batch._Cohort is cohort_models.Cohort
    assert proof_batch._RemoteCohort is cohort_models.RemoteCohort


def test_invalid_center_does_not_submit_plateau_work():
    axes = {"lookback": Axis(3, 7, 1)}
    land = Landscape(axes, lambda _: {"utility": 10.0})
    case = SimpleNamespace(request={"strategy": "mean_reversion"}, lamp_warning=lambda: None)
    pool = Mock()
    group = cohort_models.RemoteCohort(
        active="mean_reversion",
        axes=axes,
        seed={"lookback": 5},
        members=[({}, cast(Any, case))],
        land=land,
        pool=pool,
        cohort_state={},
        distinct_strategies=("mean_reversion",),
        check_cancelled=lambda: None,
    )
    field = group.plateau({"lookback": 9})["fields"]["lookback"]
    assert field["score"] is None and "диапазон" in field["reason"]
    pool.eval_plateau_batch.assert_not_called()
