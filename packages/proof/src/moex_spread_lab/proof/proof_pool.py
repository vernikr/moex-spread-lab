"""Process pool (spawn) with Sticky Sharding, LPT load balancing, and Canary fail-fast."""

from __future__ import annotations

import atexit
import contextlib
import multiprocessing
import os
import sys
import threading
import uuid
import weakref
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED as FIRST_COMPLETED
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures import wait as wait
from pathlib import Path
from typing import Any, cast

from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE as CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error as W0Error
from moex_spread_lab.strategies.funding_carry.carry import CarryStrategyError as CarryStrategyError

from .cohort_scores import (
    CheckCancelled,
    EvalFailure,
    EvalResult,
    InitItem,
    Obj,
    PointMap,
    ProgressCallback,
)
from .cohort_scores import combine_shards as combine_shards
from .cohort_scores import failure_payload as failure_payload
from .cohort_scores import point_result as point_result
from .cohort_scores import score_payload as score_payload
from .pool_reports import await_futures, canary_failure, report_cohort, should_use_pool
from .pool_reports import configured_workers as choose_worker_count
from .pool_reports import shutdown_weak as _shutdown_weak
from .pool_reports import window_summaries as _window_summaries
from .proof_kernel import StudyWindow as StudyWindow
from .strategy_catalog import get_strategy_axes as get_strategy_axes
from .worker_metrics import submit_worker
from .worker_metrics import unwrap_worker_result as unwrap_worker_result

window_summaries = _window_summaries
_original_window_summaries = _window_summaries

# Facade hooks resolved by pool_reports at call time.
def _choose_hook[T](public: T, private: T, original: T) -> T:
    return private if private is not original else public

WORKER_COHORTS = cast(dict[str, dict[int, tuple[Obj, StudyWindow]]], _wc["_WORKER_COHORTS"])
init_cohort_task = cast(
    Callable[
        [str, str, ResearchSettings, tuple[list[str], dict[str, float]], list[InitItem]],
        InitResult,
    ],
    _wc["_worker_init_cohort"],
)
release_cohort_task = cast(Callable[[str], None], _wc["_worker_release_cohort"])
eval_indices_task = cast(
    Callable[[str, PointMap, list[int] | None, int | None], EvalResult],
    _wc["_worker_eval_indices"],
)
eval_plateau_task = cast(
    Callable[[str, list[tuple[int, PointMap]]], dict[int, EvalResult]],
    _wc["_worker_eval_plateau_batch"],
)
report_cohort_task = cast(
    Callable[[str, PointMap, bool, ResearchSettings], list[ReportRow]],
    _wc["_worker_report_cohort"],
)
window_summaries_task = cast(
    Callable[
        [str, ResearchSettings, tuple[list[str], dict[str, float]], list[SummaryItem]],
        SummaryRows,
    ],
    _ww["_worker_window_summaries"],
)
worker_service = cast(Callable[[str], Any], _ww["_worker_service"])


def plan_shards(
    items: list[InitItem], weights: dict[int, int], count: int
) -> tuple[list[list[InitItem]], dict[int, int]]:
    """LPT-balance cohort windows across ``count`` sticky shards, keeping canonical order."""
    shards: list[list[InitItem]] = [[] for _ in range(count)]
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


def fan_out(
    facade: Any,
    request: tuple[str, PointMap, list[Any]],
    shard_indices: list[int],
    indices: list[int] | None,
    exclude_idx: int | None,
    check_cancelled: Any,
) -> list[Any]:
    """Submit one evaluation task per shard and await them through the facade hooks."""
    cohort_id, point_by_strategy, slots = request
    futures = [
        facade.submit_worker(
            slots[s_idx],
            facade._worker_eval_indices,
            cohort_id,
            point_by_strategy,
            indices,
            exclude_idx,
        )
        for s_idx in shard_indices
    ]
    return list(facade._await_futures(futures, check_cancelled))


def plateau_payloads(points: list[tuple[int, PointMap]], shard_maps: list[Any]) -> dict[int, Obj]:
    """Fold per-shard plateau scores into canonical success or failure payloads."""
    results: dict[int, Obj] = {}
    for p_idx, _ in points:
        failure, scores = combine_shards(smap[p_idx] for smap in shard_maps)
        results[p_idx] = failure_payload(failure) if failure is not None else score_payload(scores)
    return results


def release_cohort_tasks(slots: list[Any], cohort_id: str, indices: list[int]) -> None:
    """Best-effort release: a slot that already died must not hide the remaining calls."""
    for s_idx in indices:
        if s_idx < len(slots):
            with contextlib.suppress(RuntimeError):
                slots[s_idx].submit(release_cohort_task, cohort_id)


def merge_init_results(chunks: list[Any]) -> list[tuple[int, str, Obj]]:
    """Flatten per-shard init chunks back into canonical window order."""
    combined: list[tuple[int, str, Obj]] = []
    for chunk in chunks:
        combined.extend(chunk)
    combined.sort(key=lambda item: item[0])
    return combined


def collect_plateau_batches(
    futures: list[Any],
    await_hook: Callable[..., list[Any]],
    check_cancelled: Any,
    on_progress: Any,
) -> list[Any]:
    """Await per-slot plateau batches while reporting one progress step per slot."""
    done_slots = 0

    def _on_done(_fut: Any) -> None:
        nonlocal done_slots
        done_slots += 1
        on_progress(done_slots, len(futures))

    return await_hook(futures, check_cancelled, on_done=_on_done if on_progress else None)


def cohort_state(
    cohort_id: str, slots: list[Any], shards: Any, owner_of: dict[int, int], chunks: list[Any]
) -> Obj:
    """Assemble the sticky cohort handle returned to cohort preparation."""
    return {
        "cohort_id": cohort_id,
        "slots": slots,
        "shards": shards,
        "owner_of": owner_of,
        "results": merge_init_results(chunks),
    }

_WORKER_COHORTS = WORKER_COHORTS
_worker_report_cohort = report_cohort_task
_worker_window_summaries = window_summaries_task
_worker_service = worker_service
_worker_init_cohort = init_cohort_task
_worker_eval_indices = eval_indices_task
_worker_release_cohort = release_cohort_task
_worker_eval_plateau_batch = eval_plateau_task
# Worker-weight estimation remains patchable.
_estimate_weight = estimate_weight

# Shards expose canonical window indices.
type _Shards = list[list[tuple[Any, ...]]]

MIN_POOL_WINDOWS = 3
MIN_POOL_BARS = 1500
POLL_INTERVAL_SECONDS = 0.05


def configured_workers() -> int:
    return choose_worker_count(os, sys)


def _await_futures(
    futures: list[Future[Any]],
    check_cancelled: CheckCancelled,
    on_done: Callable[[Future[Any]], None] | None = None,
) -> list[Any]:
    return await_futures(futures, check_cancelled, on_done, sys.modules[__name__])


class CohortWorkerPool:
    """Long-lived spawn worker pool for cohort search, plateau, and window summaries."""

    def __init__(self, max_workers: int | None = None) -> None:
        self.max_workers = max_workers if max_workers is not None else configured_workers()
        self._lock = threading.Lock()
        self._executor: ProcessPoolExecutor | None = None
        self._worker_slots: list[ProcessPoolExecutor] = []
        self._closed = False
        self._atexit_ref = weakref.ref(self)
        atexit.register(_shutdown_weak, self._atexit_ref)

    def _ensure_workers(self, count: int) -> list[ProcessPoolExecutor]:
        """One-worker-per-executor lets us pin sticky cohort shards to exact processes."""
        with self._lock:
            if self._closed:
                raise RuntimeError("CohortWorkerPool is closed")
            ctx = multiprocessing.get_context("spawn")
            while len(self._worker_slots) < count:
                self._worker_slots.append(ProcessPoolExecutor(max_workers=1, mp_context=ctx))
            return list(self._worker_slots[:count])

    def should_use(self, count: int, estimated_bars: int) -> bool:
        return should_use_pool(self, count, estimated_bars, sys.modules[__name__])

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
        slots = self._ensure_workers(w)
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
                    _worker_init_cohort,
                    cohort_id,
                    root_str,
                    settings,
                    benchmark,
                    shard,
                )
                futures.append(fut)
                fut_weights[fut] = len(shard)
        done_count = 0

        def _on_done(fut: Future[Any]) -> None:
            nonlocal done_count
            done_count += fut_weights.get(fut, 0)
            if on_progress is not None:
                on_progress(done_count, n)

        try:
            chunks = _await_futures(
                futures,
                check_cancelled,
                on_done=_on_done if on_progress is not None else None,
            )
        except BaseException:
            self.release_cohort(cohort_id, owner_of)
            raise
        return cohort_state(cohort_id, slots, shards, owner_of, chunks)

    def release_cohort(self, cohort_id: str, owner_of: dict[int, int] | None = None) -> None:
        with self._lock:
            slots = list(self._worker_slots)
        if not slots:
            return
        indices = (
            sorted(set(owner_of.values())) if owner_of is not None else list(range(len(slots)))
        )
        release_cohort_tasks(slots, cohort_id, indices)

    def eval_point(
        self,
        cohort_state: Obj,
        point_by_strategy: PointMap,
        canary_box: list[int],
        total_members: int,
        check_cancelled: Any,
    ) -> Obj:
        """Adaptive Canary / Single-Roundtrip Fan-Out with guaranteed canonical minimum-index failure reason."""
        cohort_id = cast(str, cohort_state["cohort_id"])
        slots = cast(list[Any], cohort_state["slots"])
        owner_of = cast(dict[int, int], cohort_state["owner_of"])
        shards = cast(_Shards, cohort_state["shards"])

        if not cohort_state.get("canary_active"):
            # Fan out across active shards in one roundtrip.
            active_slots = sorted(set(owner_of.values()))
            return point_result(
                cohort_state,
                canary_box,
                fan_out(
                    sys.modules[__name__],
                    (cohort_id, point_by_strategy, slots),
                    active_slots,
                    None,
                    None,
                    check_cancelled,
                ),
            )

        canary = canary_box[0]

        # Phase 1: probe the canary window first (1 IPC round-trip).
        fail, canary_scores = cast(
            EvalResult,
            _await_futures(
                [
                    submit_worker(
                        slots[owner_of[canary]],
                        _worker_eval_indices,
                        cohort_id,
                        point_by_strategy,
                        [canary],
                    )
                ],
                check_cancelled,
            )[0],
        )
        if fail is not None:
            return self._canary_failure(
                cohort_state, point_by_strategy, canary_box, fail, check_cancelled
            )

        # Restore one-roundtrip fan-out after the canary passes.
        cohort_state["canary_active"] = False

        # Phase 2: canary passed; evaluate all remaining windows across active shards.
        if total_members == 1:
            _idx, score, exp = canary_scores[0]
            return {
                "rank_score": score,
                "by_window_pp": [score],
                "utility": score if exp else None,
                **({} if exp else {"reason": "Открытого спреда не было на обучении."}),
            }

        active_slots = sorted(set(owner_of.values()))
        remaining = [
            s_idx for s_idx in active_slots if any(entry[0] != canary for entry in shards[s_idx])
        ]
        return point_result(
            cohort_state,
            canary_box,
            fan_out(
                sys.modules[__name__],
                (cohort_id, point_by_strategy, slots),
                remaining,
                None,
                canary,
                check_cancelled,
            ),
            canary_scores,
        )

    def _canary_failure(
        self,
        cohort_state: Obj,
        point_by_strategy: PointMap,
        canary_box: list[int],
        fail: EvalFailure,
        check_cancelled: CheckCancelled,
    ) -> Obj:
        return canary_failure(
            cohort_state,
            point_by_strategy,
            canary_box,
            fail,
            check_cancelled,
            sys.modules[__name__],
        )

    def eval_plateau_batch(
        self,
        cohort_state: Obj,
        points: list[tuple[int, PointMap]],
        check_cancelled: Any,
        on_progress: ProgressCallback | None = None,
    ) -> dict[int, Obj]:
        """Evaluate multiple uncached neighbor points in a single parallel batch."""
        if not points:
            return {}
        cohort_id = cast(str, cohort_state["cohort_id"])
        slots = cast(list[Any], cohort_state["slots"])
        active_slots = sorted(set(cast(dict[int, int], cohort_state["owner_of"]).values()))
        futs: list[Future[Any]] = [
            submit_worker(slots[s_idx], _worker_eval_plateau_batch, cohort_id, points)
            for s_idx in active_slots
        ]
        return plateau_payloads(
            points,
            collect_plateau_batches(futs, _await_futures, check_cancelled, on_progress),
        )

    def report_cohort(
        self,
        cohort_state: Obj,
        point_by_strategy: PointMap,
        best_is_none: bool,
        settings: ResearchSettings,
        check_cancelled: Any,
        on_progress: ProgressCallback | None = None,
    ) -> list[tuple[int, Obj, bool, Obj | None]]:
        return report_cohort(
            cohort_state,
            point_by_strategy,
            best_is_none,
            settings,
            check_cancelled,
            on_progress,
            sys.modules[__name__],
        )

    def window_summaries(
        self,
        root: Path,
        settings: ResearchSettings,
        benchmark: tuple[list[str], dict[str, float]],
        items: list[tuple[int, Obj, Obj]],
        check_cancelled: CheckCancelled,
        on_progress: ProgressCallback | None = None,
    ) -> list[Obj]:
        return _choose_hook(window_summaries, _window_summaries, _original_window_summaries)(
            (self, sys.modules[__name__]),
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
            # Shutdown is best-effort: one unresponsive executor must not block the rest.
            with contextlib.suppress(Exception):
                slot.shutdown(wait=wait_for, cancel_futures=True)


def get_worker_pool(service: Any) -> CohortWorkerPool:
    if service._pool is None:
        service._pool = CohortWorkerPool()
    return cast(CohortWorkerPool, service._pool)
