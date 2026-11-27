"""Process pool (spawn) with Sticky Sharding, LPT load balancing, and Canary fail-fast."""

from __future__ import annotations

import atexit
import contextlib
import multiprocessing
import threading
import uuid
import weakref
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from pathlib import Path
from typing import Any, cast

from moex_spread_lab.core.settings import ResearchSettings

from .pool_reports import (
    CheckCancelled,
    EvalResult,
    InitItem,
    Obj,
    PointMap,
    ProgressCallback,
    await_chunks,
    await_futures,
    canary_failure,
    combine_shards,
    configured_workers,
    estimate_weight,
    failure_payload,
    point_result,
    report_cohort,
    score_payload,
    should_use_pool,
    shutdown_weak,
    window_summaries,
)
from .worker_cohort import (
    worker_eval_indices,
    worker_eval_plateau_batch,
    worker_init_cohort,
    worker_release_cohort,
)
from .worker_cohort import worker_report_cohort as worker_report_cohort
from .worker_metrics import submit_worker

type _Shards = list[list[tuple[Any, ...]]]


def plan_shards[*Ts](
    items: Sequence[tuple[int, *Ts]], weights: Mapping[int, int], count: int
) -> tuple[list[list[tuple[int, *Ts]]], dict[int, int]]:
    """LPT-balance cohort windows across ``count`` sticky shards, keeping canonical order."""
    shards: list[list[tuple[int, *Ts]]] = [[] for _ in range(count)]
    loads = [0] * count
    owner_of: dict[int, int] = {}
    for entry in sorted(items, key=lambda item: weights[item[0]], reverse=True):
        target = min(range(count), key=lambda k: loads[k])
        shards[target].append(entry)
        loads[target] += weights[entry[0]]
        owner_of[entry[0]] = target
    for shard in shards:
        shard.sort(key=lambda entry: entry[0])
    return shards, owner_of


def _fan_out(
    cohort_id: str,
    point_by_strategy: PointMap,
    slots: Sequence[Any],
    shard_indices: Sequence[int],
    exclude_idx: int | None,
    check_cancelled: CheckCancelled,
) -> list[EvalResult]:
    futures = [
        submit_worker(
            slots[s_idx],
            worker_eval_indices,
            cohort_id,
            point_by_strategy,
            None,
            exclude_idx,
        )
        for s_idx in shard_indices
    ]
    return await_futures(futures, check_cancelled)


class CohortWorkerPool:
    """Long-lived spawn worker pool for cohort search, plateau, and window summaries."""

    def __init__(self, max_workers: int | None = None) -> None:
        self.max_workers = max_workers if max_workers is not None else configured_workers()
        self._lock = threading.Lock()
        self._executor: ProcessPoolExecutor | None = None
        self._worker_slots: list[ProcessPoolExecutor] = []
        self._closed = False
        self._atexit_ref = weakref.ref(self)
        atexit.register(shutdown_weak, self._atexit_ref)

    def ensure_workers(self, count: int) -> list[ProcessPoolExecutor]:
        """One-worker-per-executor lets us pin sticky cohort shards to exact processes."""
        with self._lock:
            if self._closed:
                raise RuntimeError("CohortWorkerPool is closed")
            ctx = multiprocessing.get_context("spawn")
            while len(self._worker_slots) < count:
                self._worker_slots.append(ProcessPoolExecutor(max_workers=1, mp_context=ctx))
            return list(self._worker_slots[:count])

    def should_use(self, count: int, estimated_bars: int) -> bool:
        return should_use_pool(self, count, estimated_bars)

    def init_cohort(
        self,
        root: Path,
        settings: ResearchSettings,
        benchmark: tuple[list[str], dict[str, float]],
        items: list[InitItem],
        check_cancelled: CheckCancelled,
        on_progress: ProgressCallback | None = None,
    ) -> Obj:
        """Distribute windows LPT-wise across worker processes and initialize StudyWindows."""
        n = len(items)
        w = min(self.max_workers, n)
        slots = self.ensure_workers(w)
        seal_cache: Obj = {}
        weights = {
            entry[0]: _estimate_weight(root, entry[1], settings.timeframe, _seal_cache=seal_cache)
            for entry in items
        }
        shards, owner_of = plan_shards(items, weights, w)

        cohort_id = uuid.uuid4().hex
        root_str = str(root.resolve())
        futures: list[Future[Any]] = []
        fut_weights: dict[Future[Any], int] = {}
        for s_idx, shard in enumerate(shards):
            if shard:
                fut = submit_worker(
                    slots[s_idx],
                    worker_init_cohort,
                    cohort_id,
                    root_str,
                    settings,
                    benchmark,
                    shard,
                )
                futures.append(fut)
                fut_weights[fut] = len(shard)
        try:
            results: list[tuple[int, str, Obj]] = await_chunks(
                futures, fut_weights, n, check_cancelled, on_progress
            )
        except BaseException:
            self.release_cohort(cohort_id, owner_of)
            raise
        return {
            "cohort_id": cohort_id,
            "slots": slots,
            "shards": shards,
            "owner_of": owner_of,
            "results": results,
        }

    def release_cohort(self, cohort_id: str, owner_of: dict[int, int] | None = None) -> None:
        with self._lock:
            slots = list(self._worker_slots)
        if not slots:
            return
        indices = (
            sorted(set(owner_of.values())) if owner_of is not None else list(range(len(slots)))
        )
        for s_idx in indices:
            if s_idx < len(slots):
                with contextlib.suppress(RuntimeError):
                    slots[s_idx].submit(worker_release_cohort, cohort_id)

    def eval_point(
        self,
        cohort_state: Obj,
        point_by_strategy: PointMap,
        canary_box: list[int],
        total_members: int,
        check_cancelled: CheckCancelled,
    ) -> Obj:
        """Adaptive Canary / Single-Roundtrip Fan-Out with canonical minimum-index failure."""
        cohort_id = cast(str, cohort_state["cohort_id"])
        slots = cast(list[Any], cohort_state["slots"])
        owner_of = cast(dict[int, int], cohort_state["owner_of"])
        shards = cast(_Shards, cohort_state["shards"])
        active_slots = sorted(set(owner_of.values()))

        if not cohort_state.get("canary_active"):
            return point_result(
                cohort_state,
                canary_box,
                _fan_out(cohort_id, point_by_strategy, slots, active_slots, None, check_cancelled),
            )

        canary = canary_box[0]
        fail, canary_scores = cast(
            EvalResult,
            await_futures(
                [
                    submit_worker(
                        slots[owner_of[canary]],
                        worker_eval_indices,
                        cohort_id,
                        point_by_strategy,
                        [canary],
                    )
                ],
                check_cancelled,
            )[0],
        )
        if fail is not None:
            return canary_failure(
                cohort_state, point_by_strategy, canary_box, fail, check_cancelled
            )

        cohort_state["canary_active"] = False
        if total_members == 1:
            return score_payload(canary_scores)

        remaining = [
            s_idx for s_idx in active_slots if any(entry[0] != canary for entry in shards[s_idx])
        ]
        return point_result(
            cohort_state,
            canary_box,
            _fan_out(cohort_id, point_by_strategy, slots, remaining, canary, check_cancelled),
            canary_scores,
        )

    def eval_plateau_batch(
        self,
        cohort_state: Obj,
        points: list[tuple[int, PointMap]],
        check_cancelled: CheckCancelled,
        on_progress: ProgressCallback | None = None,
    ) -> dict[int, Obj]:
        """Evaluate multiple uncached neighbor points in a single parallel batch."""
        if not points:
            return {}
        cohort_id = cast(str, cohort_state["cohort_id"])
        slots = cast(list[Any], cohort_state["slots"])
        active_slots = sorted(set(cast(dict[int, int], cohort_state["owner_of"]).values()))
        futs: list[Future[Any]] = [
            submit_worker(slots[s_idx], worker_eval_plateau_batch, cohort_id, points)
            for s_idx in active_slots
        ]
        done_slots = 0

        def _on_done(_fut: Future[Any]) -> None:
            nonlocal done_slots
            done_slots += 1
            if on_progress is not None:
                on_progress(done_slots, len(futs))

        shard_maps = await_futures(
            futs, check_cancelled, on_done=_on_done if on_progress is not None else None
        )
        results: dict[int, Obj] = {}
        for p_idx, _ in points:
            failure, scores = combine_shards(smap[p_idx] for smap in shard_maps)
            results[p_idx] = (
                failure_payload(failure) if failure is not None else score_payload(scores)
            )
        return results

    def report_cohort(
        self,
        cohort_state: Obj,
        point_by_strategy: PointMap,
        best_is_none: bool,
        settings: ResearchSettings,
        check_cancelled: CheckCancelled,
        on_progress: ProgressCallback | None = None,
    ) -> list[tuple[int, Obj, bool, Obj | None]]:
        return report_cohort(
            cohort_state,
            point_by_strategy,
            best_is_none,
            settings,
            check_cancelled,
            on_progress,
        )

    def window_summaries(
        self,
        root: Path,
        settings: ResearchSettings,
        benchmark: tuple[list[str], dict[str, float]],
        items: list[tuple[int, Obj, Obj]],
        check_cancelled: CheckCancelled,
        on_progress: ProgressCallback | None = None,
        worker_fn: Callable[..., list[tuple[int, Obj]]] | None = None,
    ) -> list[Obj]:
        return window_summaries(
            (self, worker_fn),
            root,
            settings,
            benchmark,
            items,
            check_cancelled,
            on_progress,
        )

    def shutdown(self, wait_for: bool = True) -> None:
        with self._lock:
            self._closed = True
            slots = list(self._worker_slots)
            self._worker_slots.clear()
        for slot in slots:
            with contextlib.suppress(Exception):
                slot.shutdown(wait=wait_for, cancel_futures=True)


def get_worker_pool(service: Any) -> CohortWorkerPool:
    if service._pool is None:
        service._pool = CohortWorkerPool()
    return cast(CohortWorkerPool, service._pool)


_estimate_weight = estimate_weight
shared_estimate_weight = estimate_weight
_await_futures = await_futures
