"""Canonical aggregation and adaptive canary regression cases."""

from concurrent.futures import Future

from moex_spread_lab.proof import cohort_scores
from moex_spread_lab.proof.proof_pool import CohortWorkerPool


class ImmediateSlot:
    def __init__(self, handler):
        self.handler = handler

    def submit(self, fn, *args):
        future = Future()
        try:
            future.set_result(self.handler(fn.__name__, *args))
        except (
            BaseException
        ) as exc:  # Future must model executor error propagation, including cancellation.
            future.set_exception(exc)
        return future


def test_aggregation_preserves_canonical_order_failure_and_inactive_utility():
    failure, scores = cohort_scores.combine_shards(
        [
            ((3, "D", "later"), [(2, 3.0, False)]),
            ((1, "B", "first"), [(0, 1.0, False)]),
        ]
    )
    assert failure == (1, "B", "first")
    assert cohort_scores.failure_payload(failure)["reason"] == "B: first"
    assert cohort_scores.score_payload(scores) == {
        "rank_score": 2.0,
        "by_window_pp": [1.0, 3.0],
        "utility": None,
        "reason": "Открытого спреда не было на обучении.",
    }


def _canary_handler(slot, phase):
    def run(name, cohort_id, point, indices=None, exclude=None):
        assert name == "_worker_eval_indices" and cohort_id == "cohort"
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
