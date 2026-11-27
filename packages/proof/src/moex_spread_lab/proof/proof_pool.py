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
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from pathlib import Path
from statistics import fmean
from typing import Any, cast

from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.data.lake.bar_types import SealedH1Reader

from .worker_cohort import (
    worker_eval_indices,
    worker_eval_plateau_batch,
    worker_init_cohort,
    worker_release_cohort,
    worker_report_cohort,
)
from .worker_metrics import submit_worker, unwrap_worker_result

MIN_POOL_WINDOWS = 3
MIN_POOL_BARS = 1500
POLL_INTERVAL_SECONDS = 0.05

type Obj = dict[str, Any]
type PointMap = dict[str, Obj]
type EvalFailure = tuple[int, str, str]
type EvalScore = tuple[int, float, bool]
type EvalResult = tuple[EvalFailure | None, list[EvalScore]]
type InitItem = tuple[int, Obj, Obj, bool]
type CheckCancelled = Any
type ProgressCallback = Any


def configured_workers() -> int:
    raw = os.environ.get("MOEX_PROOF_WORKERS")
    if raw is not None:
        with contextlib.suppress(ValueError):
            return max(1, int(raw))
    cpu = os.cpu_count() or 6
    if sys.platform == "darwin" and cpu > 4:
        return max(2, min(6, cpu // 2))
    return max(1, min(8, cpu))


def _pair_h1_weight(root: Path, contract: str, cache: Obj | None) -> int:
    if cache is not None and "reader" in cache:
        reader, seal, by_legacy = cache["reader"], cache["seal"], cache["by_legacy"]
    else:
        reader = SealedH1Reader(root)
        seal = reader.payload()
        by_legacy = {e["legacy_path"]: e for e in seal["plan"]["files"]} if seal else {}
        if cache is not None:
            cache.update(reader=reader, seal=seal, by_legacy=by_legacy)
    if seal is not None:
        entry = by_legacy.get(reader.select(contract).relative_to(reader.root).as_posix())
        return max(100, entry["logical_bytes"] // 128) if entry is not None else 1000
    for subdir in ("candles-screen", "candles"):
        with contextlib.suppress(OSError):
            return max(
                100, (root / "data/w1" / subdir / f"{contract}_1h.json").stat().st_size // 128
            )
    return 1000


def estimate_weight(
    root: Path, item: Obj, timeframe: str, *, _seal_cache: Obj | None = None
) -> int:
    if timeframe == "1h" and item.get("universe") == "pair_screen":
        parts = str(item.get("pair") or "").split("/")
        if len(parts) == 2:
            return _pair_h1_weight(root, parts[1], _seal_cache)
    return 1000 if timeframe == "1h" else 200


def plan_shards[*Ts](
    items: Sequence[tuple[int, *Ts]], weights: Mapping[int, int], count: int
) -> tuple[list[list[tuple[int, *Ts]]], dict[int, int]]:
    """LPT-balance windows across ``count`` shards, preserving canonical order per shard."""
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


def await_futures(
    futures: Sequence[Future[Any]],
    check_cancelled: CheckCancelled,
    on_done: Callable[[Future[Any]], None] | None = None,
) -> list[Any]:
    pending = set(futures)
    while pending:
        try:
            check_cancelled()
        except BaseException:
            for fut in pending:
                fut.cancel()
            raise
        done, pending = wait(pending, timeout=POLL_INTERVAL_SECONDS, return_when=FIRST_COMPLETED)
        if on_done is not None and done:
            for fut in done:
                on_done(fut)
    return [unwrap_worker_result(fut.result()) for fut in futures]


def _progress_hook(
    fut_weights: Mapping[Future[Any], int], total: int, on_progress: ProgressCallback | None
) -> Callable[[Future[Any]], None] | None:
    if on_progress is None:
        return None
    done_count = 0

    def _on_done(fut: Future[Any]) -> None:
        nonlocal done_count
        done_count += fut_weights.get(fut, 0)
        on_progress(done_count, total)

    return _on_done


def await_chunks[*Ts](
    fut_weights: Mapping[Future[Any], int],
    total: int,
    check_cancelled: CheckCancelled,
    on_progress: ProgressCallback | None,
) -> list[tuple[int, *Ts]]:
    chunks = await_futures(
        list(fut_weights), check_cancelled, _progress_hook(fut_weights, total, on_progress)
    )
    combined: list[tuple[int, *Ts]] = [item for chunk in chunks for item in chunk]
    combined.sort(key=lambda item: item[0])
    return combined


def failure_payload(failure: EvalFailure) -> Obj:
    return {"utility": None, "rank_score": None, "reason": f"{failure[1]}: {failure[2]}"}


def score_payload(scores: list[EvalScore]) -> Obj:
    scores.sort(key=lambda item: item[0])
    ordered = [item[1] for item in scores]
    exposed = any(item[2] for item in scores)
    score = ordered[0] if len(ordered) == 1 else fmean(ordered)
    return {
        "rank_score": score,
        "by_window_pp": ordered,
        "utility": score if exposed else None,
        **({} if exposed else {"reason": "Открытого спреда не было на обучении."}),
    }


def combine_shards(
    shard_results: Iterable[EvalResult], initial_scores: Sequence[EvalScore] = ()
) -> EvalResult:
    min_fail: EvalFailure | None = None
    scores = list(initial_scores)
    for sub_fail, sub_scores in shard_results:
        if sub_fail is not None and (min_fail is None or sub_fail[0] < min_fail[0]):
            min_fail = sub_fail
        scores.extend(sub_scores)
    return min_fail, scores


def point_result(
    cohort_state: Obj,
    canary_box: list[int],
    shard_results: Iterable[EvalResult],
    initial_scores: Sequence[EvalScore] = (),
) -> Obj:
    failure, scores = combine_shards(shard_results, initial_scores)
    if failure is not None:
        canary_box[0] = failure[0]
        cohort_state["canary_active"] = True
        return failure_payload(failure)
    return score_payload(scores)


def _fan_out(
    cohort_state: Obj,
    point_by_strategy: PointMap,
    shard_indices: Iterable[int],
    exclude_idx: int | None,
    check_cancelled: CheckCancelled,
) -> list[EvalResult]:
    cohort_id, slots = cohort_state["cohort_id"], cohort_state["slots"]
    futs = [
        submit_worker(
            slots[s_idx], worker_eval_indices, cohort_id, point_by_strategy, None, exclude_idx
        )
        for s_idx in shard_indices
    ]
    return await_futures(futs, check_cancelled)


def _shutdown_weak(ref: weakref.ReferenceType[Any]) -> None:
    if (pool := ref()) is not None:
        pool.shutdown(wait_for=False)


class CohortWorkerPool:
    """Long-lived spawn worker pool for cohort search, plateau, and window summaries."""

    def __init__(self, max_workers: int | None = None) -> None:
        self.max_workers = max_workers if max_workers is not None else configured_workers()
        self._lock = threading.Lock()
        self._worker_slots: list[ProcessPoolExecutor] = []
        self._closed = False
        atexit.register(_shutdown_weak, weakref.ref(self))

    def ensure_workers(self, count: int) -> list[ProcessPoolExecutor]:
        """One-worker-per-executor pins sticky cohort shards to exact processes."""
        with self._lock:
            if self._closed:
                raise RuntimeError("CohortWorkerPool is closed")
            ctx = multiprocessing.get_context("spawn")
            while len(self._worker_slots) < count:
                self._worker_slots.append(ProcessPoolExecutor(max_workers=1, mp_context=ctx))
            return list(self._worker_slots[:count])

    def should_use(self, count: int, estimated_bars: int) -> bool:
        if self.max_workers <= 1 or count < MIN_POOL_WINDOWS:
            return False
        if os.environ.get("MOEX_PROOF_FORCE_POOL") == "1":
            return True
        return estimated_bars >= MIN_POOL_BARS or count >= 4

    def init_cohort(
        self,
        root: Path,
        settings: ResearchSettings,
        benchmark: tuple[list[str], dict[str, float]],
        items: list[InitItem],
        check_cancelled: CheckCancelled,
        on_progress: ProgressCallback | None = None,
    ) -> Obj:
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
        fut_weights = {
            submit_worker(
                slots[s_idx], worker_init_cohort, cohort_id, root_str, settings, benchmark, shard
            ): len(shard)
            for s_idx, shard in enumerate(shards)
            if shard
        }
        try:
            results: list[tuple[int, str, Obj]] = await_chunks(
                fut_weights, n, check_cancelled, on_progress
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
        indices = sorted(set(owner_of.values())) if owner_of is not None else range(len(slots))
        for s_idx in indices:
            if s_idx < len(slots):
                with contextlib.suppress(RuntimeError):
                    slots[s_idx].submit(worker_release_cohort, cohort_id)

    def _canary_failure(
        self,
        cohort_state: Obj,
        point_by_strategy: PointMap,
        canary_box: list[int],
        fail: EvalFailure,
        check_cancelled: CheckCancelled,
    ) -> Obj:
        canary = canary_box[0]
        if canary == 0:
            return failure_payload(fail)
        owner_of, slots = cohort_state["owner_of"], cohort_state["slots"]
        shards, cohort_id = cohort_state["shards"], cohort_state["cohort_id"]
        sub_futs = [
            submit_worker(
                slots[s_idx],
                worker_eval_indices,
                cohort_id,
                point_by_strategy,
                [entry[0] for entry in shards[s_idx] if entry[0] < canary],
            )
            for s_idx in sorted({owner_of[i] for i in range(canary)})
        ]
        best_fail = fail
        for sub_fail, _ in await_futures(sub_futs, check_cancelled):
            if sub_fail is not None and sub_fail[0] < best_fail[0]:
                best_fail = sub_fail
        canary_box[0] = best_fail[0]
        return failure_payload(best_fail)

    def eval_point(
        self,
        cohort_state: Obj,
        point_by_strategy: PointMap,
        canary_box: list[int],
        total_members: int,
        check_cancelled: CheckCancelled,
    ) -> Obj:
        """Adaptive Canary / Single-Roundtrip Fan-Out with canonical minimum-index failure."""
        cohort_id, slots = cohort_state["cohort_id"], cohort_state["slots"]
        owner_of, shards = cohort_state["owner_of"], cohort_state["shards"]
        active_slots = sorted(set(owner_of.values()))
        if not cohort_state.get("canary_active"):
            return point_result(
                cohort_state,
                canary_box,
                _fan_out(cohort_state, point_by_strategy, active_slots, None, check_cancelled),
            )

        canary = canary_box[0]
        probe = submit_worker(
            slots[owner_of[canary]], worker_eval_indices, cohort_id, point_by_strategy, [canary]
        )
        fail, canary_scores = cast(EvalResult, await_futures([probe], check_cancelled)[0])
        if fail is not None:
            return self._canary_failure(
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
            _fan_out(cohort_state, point_by_strategy, remaining, canary, check_cancelled),
            canary_scores,
        )

    def eval_plateau_batch(
        self,
        cohort_state: Obj,
        points: list[tuple[int, PointMap]],
        check_cancelled: CheckCancelled,
        on_progress: ProgressCallback | None = None,
    ) -> dict[int, Obj]:
        if not points:
            return {}
        cohort_id, slots = cohort_state["cohort_id"], cohort_state["slots"]
        active_slots = sorted(set(cohort_state["owner_of"].values()))
        fut_weights = {
            submit_worker(slots[s_idx], worker_eval_plateau_batch, cohort_id, points): 1
            for s_idx in active_slots
        }
        shard_maps = await_futures(
            list(fut_weights),
            check_cancelled,
            _progress_hook(fut_weights, len(fut_weights), on_progress),
        )
        results: dict[int, Obj] = {}
        for p_idx, _ in points:
            failure, scores = combine_shards(smap[p_idx] for smap in shard_maps)
            results[p_idx] = failure_payload(failure) if failure else score_payload(scores)
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
        cohort_id = cohort_state["cohort_id"]
        slots, shards = cohort_state["slots"], cohort_state["shards"]
        active_slots = sorted(set(cohort_state["owner_of"].values()))
        total_members = sum(len(shards[s_idx]) for s_idx in active_slots)
        fut_weights = {
            submit_worker(
                slots[s_idx],
                worker_report_cohort,
                cohort_id,
                point_by_strategy,
                best_is_none,
                settings,
            ): len(shards[s_idx])
            for s_idx in active_slots
        }
        return await_chunks(fut_weights, total_members, check_cancelled, on_progress)

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
