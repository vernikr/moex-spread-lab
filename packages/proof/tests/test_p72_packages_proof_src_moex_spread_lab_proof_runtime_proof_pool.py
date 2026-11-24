"""Isolated regressions for the strict-typed process pool facade (Issue #91)."""

from __future__ import annotations

from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any, cast

import pytest

from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.proof import (
    cohort_scores,
    pool_reports,
    proof_pool,
    worker_cohort,
    worker_metrics,
    worker_weight,
    worker_window,
)


class _ImmediateSlot:
    """Executor stand-in that resolves submitted work in the submitting thread."""

    def __init__(self, handler, *, fail_on: frozenset[str] = frozenset()) -> None:
        self.handler = handler
        self.fail_on = fail_on
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def submit(self, fn, *args):
        self.calls.append((fn.__name__, args))
        future: Future[Any] = Future()
        if fn.__name__ in self.fail_on:
            future.set_exception(RuntimeError(f"{fn.__name__} unavailable"))
            return future
        future.set_result(self.handler(fn.__name__, *args))
        return future


class _Service:
    def __init__(self, pool: Any = None) -> None:
        self._pool = pool


def test_p72_pool_facade_bindings_stay_lazy_modules() -> None:
    assert proof_pool.combine_shards is cohort_scores.combine_shards
    assert proof_pool.point_result is cohort_scores.point_result
    assert proof_pool.failure_payload is cohort_scores.failure_payload
    assert proof_pool.score_payload is cohort_scores.score_payload
    assert proof_pool._WORKER_COHORTS is worker_cohort._WORKER_COHORTS
    assert proof_pool._worker_eval_indices is worker_cohort._worker_eval_indices
    assert proof_pool._worker_report_cohort is worker_cohort._worker_report_cohort
    assert proof_pool._worker_window_summaries is worker_window._worker_window_summaries
    assert proof_pool._worker_service is worker_window._worker_service
    assert proof_pool._estimate_weight is worker_weight.estimate_weight
    assert proof_pool._estimate_weight is worker_weight._estimate_weight
    assert proof_pool.submit_worker is worker_metrics.submit_worker
    assert proof_pool.unwrap_worker_result is worker_metrics.unwrap_worker_result
    assert proof_pool.await_futures is pool_reports.await_futures
    assert proof_pool.MIN_POOL_WINDOWS == 3 and proof_pool.MIN_POOL_BARS == 1500


def test_p72_release_cohort_swallows_only_closed_slot_runtime_errors() -> None:
    pool = proof_pool.CohortWorkerPool(max_workers=2)
    dead = _ImmediateSlot(lambda name, *args: None, fail_on=frozenset({"_worker_release_cohort"}))
    live = _ImmediateSlot(lambda name, *args: None)
    slots = cast(Any, pool)._worker_slots
    try:
        slots[:] = [dead, live]
        pool.release_cohort("cohort", {0: 0, 1: 1})
        assert [name for name, _ in live.calls] == ["_worker_release_cohort"]
        assert [name for name, _ in dead.calls] == ["_worker_release_cohort"]
    finally:
        pool.shutdown()


def test_p72_init_cohort_releases_workers_when_cancelled(monkeypatch, tmp_path) -> None:
    pool = proof_pool.CohortWorkerPool(max_workers=1)
    slot = _ImmediateSlot(lambda name, *args: [])
    cast(Any, pool)._worker_slots.append(slot)
    monkeypatch.setattr(pool, "_ensure_workers", lambda count: [slot])
    monkeypatch.setattr(proof_pool, "_estimate_weight", lambda *args, **kwargs: 1)
    monkeypatch.setattr(
        proof_pool, "submit_worker", lambda executor, fn, *args: executor.submit(fn, *args)
    )

    def _cancel(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("cancelled")

    monkeypatch.setattr(proof_pool, "_await_futures", _cancel)
    try:
        with pytest.raises(RuntimeError, match="cancelled"):
            pool.init_cohort(
                tmp_path,
                ResearchSettings(),
                ([], {}),
                [(0, {}, {}, True)],
                lambda: None,
            )
    finally:
        pool.shutdown()
    assert [name for name, _ in slot.calls] == [
        "_worker_init_cohort",
        "_worker_release_cohort",
    ]


def test_p72_init_cohort_reports_progress_and_sorts_results(monkeypatch, tmp_path) -> None:
    pool = proof_pool.CohortWorkerPool(max_workers=1)
    slot = _ImmediateSlot(lambda name, *args: [])
    monkeypatch.setattr(pool, "_ensure_workers", lambda count: [slot])
    monkeypatch.setattr(proof_pool, "_estimate_weight", lambda *args, **kwargs: 1)
    monkeypatch.setattr(
        proof_pool, "submit_worker", lambda executor, fn, *args: executor.submit(fn, *args)
    )

    def _await(futures: list[Any], check_cancelled: Any, on_done: Any = None) -> list[Any]:
        if on_done is not None:
            for future in futures:
                on_done(future)
        return [[(1, "ok", {}), (0, "ok", {})]]

    monkeypatch.setattr(proof_pool, "_await_futures", _await)
    events: list[tuple[int, int]] = []
    try:
        state = pool.init_cohort(
            tmp_path,
            ResearchSettings(),
            ([], {}),
            [(0, {}, {}, True), (1, {}, {}, True)],
            lambda: None,
            on_progress=lambda done, total: events.append((done, total)),
        )
    finally:
        pool.shutdown()
    assert [row[0] for row in state["results"]] == [0, 1]
    assert state["owner_of"] == {0: 0, 1: 0}
    assert events == [(2, 2)]


def test_p72_plateau_batch_progress_and_payload_shapes(monkeypatch) -> None:
    pool = proof_pool.CohortWorkerPool(max_workers=1)
    state = {"cohort_id": "cohort", "slots": ["slot"], "owner_of": {0: 0, 1: 0}, "shards": []}
    assert pool.eval_plateau_batch(state, [], lambda: None) == {}
    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(
        proof_pool,
        "_await_futures",
        lambda futures, check_cancelled, on_done=None: (
            on_done("future") if on_done is not None else None,
            [{4: (None, [(0, 2.0, True), (1, 4.0, True)])}, {4: ((1, "B", "bad"), [])}],
        )[1],
    )
    monkeypatch.setattr(
        proof_pool,
        "submit_worker",
        lambda executor, fn, *args: SimpleNamespace(name="future"),
    )
    try:
        out = pool.eval_plateau_batch(
            state,
            [(4, {"mean_reversion": {"lookback": 5}})],
            lambda: None,
            on_progress=lambda done, total: seen.append((done, total)),
        )
    finally:
        pool.shutdown()
    assert out == {4: {"utility": None, "rank_score": None, "reason": "B: bad"}}
    assert seen == [(1, 1)]


def test_p72_pool_shutdown_is_best_effort_and_closed_pool_rejects_work() -> None:
    class _FailingSlot:
        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            raise RuntimeError("already gone")

    class _RecordingSlot(_FailingSlot):
        def __init__(self) -> None:
            self.calls: list[tuple[bool, bool]] = []

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            self.calls.append((wait, cancel_futures))

    recording = _RecordingSlot()
    pool = proof_pool.CohortWorkerPool(max_workers=2)
    slots = cast(Any, pool)._worker_slots
    slots.extend([recording, _FailingSlot()])
    pool.shutdown(wait_for=False)
    assert recording.calls == [(False, True)]
    assert pool._worker_slots == []
    with pytest.raises(RuntimeError, match="closed"):
        pool._ensure_workers(1)


def test_p72_get_worker_pool_creates_once_and_reuses_instance() -> None:
    service = _Service()
    first = proof_pool.get_worker_pool(service)
    try:
        assert isinstance(first, proof_pool.CohortWorkerPool)
        assert proof_pool.get_worker_pool(service) is first
    finally:
        first.shutdown()


def test_p72_should_use_delegates_to_pool_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = proof_pool.CohortWorkerPool(max_workers=1)
    try:
        assert pool.should_use(10, 10**6) is False
    finally:
        pool.shutdown()
    pool = proof_pool.CohortWorkerPool(max_workers=4)
    monkeypatch.setenv("MOEX_PROOF_FORCE_POOL", "1")
    try:
        assert pool.should_use(3, 0) is True
    finally:
        pool.shutdown()


def test_p72_eval_point_single_member_uses_canary_score() -> None:
    pool = proof_pool.CohortWorkerPool(max_workers=1)
    slot = _ImmediateSlot(lambda name, *args: (None, [(0, 2.5, False)]))
    state: dict[str, Any] = {
        "cohort_id": "cohort",
        "slots": [slot],
        "owner_of": {0: 0},
        "shards": [[(0,)]],
        "canary_active": True,
    }
    canary = [0]
    try:
        result = pool.eval_point(state, {"x": {"lookback": 5}}, canary, 1, lambda: None)
    finally:
        pool.shutdown()
    assert result == {
        "rank_score": 2.5,
        "by_window_pp": [2.5],
        "utility": None,
        "reason": "Открытого спреда не было на обучении.",
    }
    assert state["canary_active"] is False
    assert canary == [0]
