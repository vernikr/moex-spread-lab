"""Cohort reports and window batches dispatched to sticky worker slots."""

from __future__ import annotations

import os
import sys
import weakref
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
from pathlib import Path
from statistics import fmean
from typing import Any

from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.data.lake.bar_types import SealedH1Reader

from .worker_cohort import worker_eval_indices, worker_report_cohort
from .worker_metrics import submit_worker, unwrap_worker_result

MIN_POOL_WINDOWS = 3
MIN_POOL_BARS = 1500
POLL_INTERVAL_SECONDS = 0.05

type Obj = dict[str, Any]
type PointMap = dict[str, Obj]
type EvalFailure = tuple[int, str, str]
type EvalScore = tuple[int, float, bool]
type EvalScores = list[EvalScore]
type EvalResult = tuple[EvalFailure | None, EvalScores]
type InitItem = tuple[int, Obj, Obj, bool]
type CheckCancelled = Any
type ProgressCallback = Any


def await_futures(
    futures: list[Future[Any]],
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


def await_chunks[*Ts](
    futures: list[Future[Any]],
    fut_weights: Mapping[Future[Any], int],
    total: int,
    check_cancelled: CheckCancelled,
    on_progress: ProgressCallback | None,
) -> list[tuple[int, *Ts]]:
    done_count = 0

    def _on_done(fut: Future[Any]) -> None:
        nonlocal done_count
        done_count += fut_weights.get(fut, 0)
        if on_progress is not None:
            on_progress(done_count, total)

    chunks = await_futures(
        futures, check_cancelled, on_done=_on_done if on_progress is not None else None
    )
    combined: list[tuple[int, *Ts]] = [item for chunk in chunks for item in chunk]
    combined.sort(key=lambda item: item[0])
    return combined


def report_cohort(
    cohort_state: Obj,
    point_by_strategy: PointMap,
    best_is_none: bool,
    settings: ResearchSettings,
    check_cancelled: CheckCancelled,
    on_progress: ProgressCallback | None,
) -> list[tuple[int, Obj, bool, Obj | None]]:
    cohort_id = cohort_state["cohort_id"]
    slots = cohort_state["slots"]
    shards = cohort_state["shards"]
    active_slots = sorted(set(cohort_state["owner_of"].values()))
    total_members = sum(len(shards[s_idx]) for s_idx in active_slots)
    futs: list[Future[Any]] = []
    fut_weights: dict[Future[Any], int] = {}
    for s_idx in active_slots:
        fut = submit_worker(
            slots[s_idx],
            worker_report_cohort,
            cohort_id,
            point_by_strategy,
            best_is_none,
            settings,
        )
        futs.append(fut)
        fut_weights[fut] = len(shards[s_idx])
    return await_chunks(futs, fut_weights, total_members, check_cancelled, on_progress)


def window_summaries(
    deps: tuple[Any, Callable[..., list[tuple[int, Obj]]] | None],
    root: Path,
    settings: ResearchSettings,
    benchmark: tuple[list[str], dict[str, float]],
    items: list[tuple[int, Obj, Obj]],
    check_cancelled: CheckCancelled,
    on_progress: ProgressCallback | None,
) -> list[Obj]:
    pool, worker_fn = deps
    assert worker_fn is not None
    n = len(items)
    w = min(pool.max_workers, n)
    slots = pool.ensure_workers(w)
    seal_cache: Obj = {}
    weights = {
        entry[0]: estimate_weight(root, entry[1], settings.timeframe, _seal_cache=seal_cache)
        for entry in items
    }
    weighted = sorted(items, key=lambda entry: weights[entry[0]], reverse=True)
    batches = min(n, w * 2) if n >= w * 2 else w
    shards: list[list[tuple[int, Obj, Obj]]] = [[] for _ in range(batches)]
    shard_loads = [0] * batches
    for entry in weighted:
        s_idx = min(range(batches), key=lambda k: shard_loads[k])
        shards[s_idx].append(entry)
        shard_loads[s_idx] += weights[entry[0]]
    root_str = str(root.resolve())
    futs: list[Future[Any]] = []
    fut_weights: dict[Future[Any], int] = {}
    for b_idx, shard in enumerate(shards):
        if shard:
            fut = submit_worker(slots[b_idx % w], worker_fn, root_str, settings, benchmark, shard)
            futs.append(fut)
            fut_weights[fut] = len(shard)
    rows: list[tuple[int, Obj]] = await_chunks(futs, fut_weights, n, check_cancelled, on_progress)
    return [row for _, row in rows]


def canary_failure(
    cohort_state: Obj,
    point_by_strategy: PointMap,
    canary_box: list[int],
    fail: EvalFailure,
    check_cancelled: CheckCancelled,
) -> Obj:
    canary = canary_box[0]
    if canary == 0:
        return failure_payload(fail)
    owner_of = cohort_state["owner_of"]
    slots = cohort_state["slots"]
    shards = cohort_state["shards"]
    cohort_id = cohort_state["cohort_id"]
    earlier_slots = sorted({owner_of[i] for i in range(canary)})
    sub_futs = [
        submit_worker(
            slots[s_idx],
            worker_eval_indices,
            cohort_id,
            point_by_strategy,
            [entry[0] for entry in shards[s_idx] if entry[0] < canary],
        )
        for s_idx in earlier_slots
    ]
    best_fail = fail
    for sub_fail, _ in await_futures(sub_futs, check_cancelled):
        if sub_fail is not None and sub_fail[0] < best_fail[0]:
            best_fail = sub_fail
    canary_box[0] = best_fail[0]
    return failure_payload(best_fail)


def configured_workers() -> int:
    raw = os.environ.get("MOEX_PROOF_WORKERS")
    if raw is not None:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    cpu = os.cpu_count() or 6
    if sys.platform == "darwin" and cpu > 4:
        return max(2, min(6, cpu // 2))
    return max(1, min(8, cpu))


def shutdown_weak(ref: weakref.ReferenceType[Any]) -> None:
    pool = ref()
    if pool is not None:
        pool.shutdown(wait_for=False)


def should_use_pool(pool: Any, count: int, estimated_bars: int) -> bool:
    if pool.max_workers <= 1 or count < MIN_POOL_WINDOWS:
        return False
    if os.environ.get("MOEX_PROOF_FORCE_POOL") == "1":
        return True
    return estimated_bars >= MIN_POOL_BARS or count >= 4


def failure_payload(failure: EvalFailure) -> Obj:
    return {
        "utility": None,
        "rank_score": None,
        "reason": f"{failure[1]}: {failure[2]}",
    }


def score_payload(scores: EvalScores) -> Obj:
    scores.sort(key=lambda item: item[0])
    ordered_scores = [item[1] for item in scores]
    exposed = any(item[2] for item in scores)
    score = ordered_scores[0] if len(ordered_scores) == 1 else fmean(ordered_scores)
    return {
        "rank_score": score,
        "by_window_pp": ordered_scores,
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


def _seal_data(root: Path, cache: Obj | None):
    if cache is not None and "reader" in cache:
        return cache["reader"], cache["seal"], cache["by_legacy"]
    reader = SealedH1Reader(root)
    seal = reader.payload()
    by_legacy = {e["legacy_path"]: e for e in seal["plan"]["files"]} if seal is not None else {}
    if cache is not None:
        cache.update(reader=reader, seal=seal, by_legacy=by_legacy)
    return reader, seal, by_legacy


def _pair_h1_weight(root: Path, contract: str, cache: Obj | None) -> int:
    reader, seal, by_legacy = _seal_data(root, cache)
    if seal is not None:
        selected = reader.select(contract).relative_to(reader.root).as_posix()
        entry = by_legacy.get(selected)
        return max(100, entry["logical_bytes"] // 128) if entry is not None else 1000
    for subdir in ("candles-screen", "candles"):
        path = root / "data/w1" / subdir / f"{contract}_1h.json"
        try:
            return max(100, path.stat().st_size // 128)
        except OSError:
            pass
    return 1000


def estimate_weight(
    root: Path, item: Obj, timeframe: str, *, _seal_cache: Obj | None = None
) -> int:
    if timeframe == "1h" and item.get("universe") == "pair_screen":
        parts = str(item.get("pair") or "").split("/")
        if len(parts) == 2:
            return _pair_h1_weight(root, parts[1], _seal_cache)
    return 1000 if timeframe == "1h" else 200


_estimate_weight = estimate_weight
