"""Cohort reports and window batches dispatched to sticky worker slots."""

import weakref
from collections.abc import Iterable, Sequence
from pathlib import Path
from statistics import fmean
from typing import Any

from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.data.lake.bar_types import SealedH1Reader


def report_cohort(
    cohort_state: dict[str, Any],
    point_by_strategy: dict[str, dict[str, Any]],
    best_is_none: bool,
    settings: ResearchSettings,
    check_cancelled: Any,
    on_progress: Any,
    facade: Any,
) -> list[tuple[int, dict[str, Any], bool, dict[str, Any] | None]]:
    cohort_id = cohort_state["cohort_id"]
    slots = cohort_state["slots"]
    shards = cohort_state["shards"]
    active_slots = sorted(set(cohort_state["owner_of"].values()))
    total_members = sum(len(shards[s_idx]) for s_idx in active_slots)
    futs: list[Any] = []
    fut_weights: dict[Any, int] = {}
    for s_idx in active_slots:
        fut = facade.submit_worker(
            slots[s_idx],
            facade._worker_report_cohort,
            cohort_id,
            point_by_strategy,
            best_is_none,
            settings,
        )
        futs.append(fut)
        fut_weights[fut] = len(shards[s_idx])
    done_count = 0

    def _on_done(fut: Any) -> None:
        nonlocal done_count
        done_count += fut_weights.get(fut, 0)
        if on_progress is not None:
            on_progress(done_count, total_members)

    combined: list[tuple[int, dict[str, Any], bool, dict[str, Any] | None]] = []
    for chunk in facade._await_futures(
        futs, check_cancelled, on_done=_on_done if on_progress is not None else None
    ):
        combined.extend(chunk)
    combined.sort(key=lambda item: item[0])
    return combined


def window_summaries(
    deps: tuple[Any, Any],
    root: Path,
    settings: ResearchSettings,
    benchmark: tuple[list[str], dict[str, float]],
    items: list[tuple[int, dict[str, Any], dict[str, Any]]],
    check_cancelled: Any,
    on_progress: Any,
) -> list[dict[str, Any]]:
    pool, facade = deps
    n = len(items)
    w = min(pool.max_workers, n)
    slots = pool._ensure_workers(w)
    seal_cache: dict[str, Any] = {}
    weights = {
        entry[0]: facade._estimate_weight(
            root, entry[1], settings.timeframe, _seal_cache=seal_cache
        )
        for entry in items
    }
    weighted = sorted(items, key=lambda entry: weights[entry[0]], reverse=True)
    # Split into up to 2 micro-batches per worker for larger sets so progress updates smoothly.
    batches = min(n, w * 2) if n >= w * 2 else w
    shards: list[list[tuple[int, dict[str, Any], dict[str, Any]]]] = [[] for _ in range(batches)]
    shard_loads = [0] * batches
    for entry in weighted:
        s_idx = min(range(batches), key=lambda k: shard_loads[k])
        shards[s_idx].append(entry)
        shard_loads[s_idx] += weights[entry[0]]
    root_str = str(root.resolve())
    futs: list[Any] = []
    fut_weights: dict[Any, int] = {}
    for b_idx, shard in enumerate(shards):
        if shard:
            fut = facade.submit_worker(
                slots[b_idx % w],
                facade._worker_window_summaries,
                root_str,
                settings,
                benchmark,
                shard,
            )
            futs.append(fut)
            fut_weights[fut] = len(shard)
    done_count = 0

    def _on_done(fut: Any) -> None:
        nonlocal done_count
        done_count += fut_weights.get(fut, 0)
        if on_progress is not None:
            on_progress(done_count, n)

    combined: list[tuple[int, dict[str, Any]]] = []
    for chunk in facade._await_futures(
        futs, check_cancelled, on_done=_on_done if on_progress is not None else None
    ):
        combined.extend(chunk)
    combined.sort(key=lambda entry: entry[0])
    return [entry[1] for entry in combined]


def canary_failure(
    cohort_state: dict[str, Any],
    point_by_strategy: dict[str, dict[str, Any]],
    canary_box: list[int],
    fail: Any,
    check_cancelled: Any,
    facade: Any,
) -> dict[str, Any]:
    canary = canary_box[0]
    if canary == 0:
        return facade.failure_payload(fail)
    owner_of = cohort_state["owner_of"]
    slots = cohort_state["slots"]
    shards = cohort_state["shards"]
    cohort_id = cohort_state["cohort_id"]
    # A lower canonical index may also fail; inspect only indices before the canary.
    earlier_slots = sorted({owner_of[i] for i in range(canary)})
    sub_futs = [
        facade.submit_worker(
            slots[s_idx],
            facade._worker_eval_indices,
            cohort_id,
            point_by_strategy,
            [entry[0] for entry in shards[s_idx] if entry[0] < canary],
        )
        for s_idx in earlier_slots
    ]
    best_fail = fail
    for sub_fail, _ in facade._await_futures(sub_futs, check_cancelled):
        if sub_fail is not None and sub_fail[0] < best_fail[0]:
            best_fail = sub_fail
    canary_box[0] = best_fail[0]
    return facade.failure_payload(best_fail)


def configured_workers(os: Any, sys: Any) -> int:
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


def await_futures(futures: list[Any], check_cancelled: Any, on_done: Any, facade: Any) -> list[Any]:
    pending = set(futures)
    while pending:
        try:
            check_cancelled()
        except BaseException:
            for fut in pending:
                fut.cancel()
            raise
        done, pending = facade.wait(
            pending, timeout=facade.POLL_INTERVAL_SECONDS, return_when=facade.FIRST_COMPLETED
        )
        if on_done is not None and done:
            for fut in done:
                on_done(fut)
    return [facade.unwrap_worker_result(fut.result()) for fut in futures]


def shutdown_weak(ref: weakref.ReferenceType[Any]) -> None:
    pool = ref()
    if pool is not None:
        pool.shutdown(wait_for=False)


def should_use_pool(pool: Any, count: int, estimated_bars: int, facade: Any) -> bool:
    if pool.max_workers <= 1 or count < facade.MIN_POOL_WINDOWS:
        return False
    if facade.os.environ.get("MOEX_PROOF_FORCE_POOL") == "1":
        return True
    return estimated_bars >= facade.MIN_POOL_BARS or count >= 4


type Obj = dict[str, Any]
type PointMap = dict[str, Obj]
type EvalFailure = tuple[int, str, str]
type EvalScore = tuple[int, float, bool]
type EvalScores = list[EvalScore]
type EvalResult = tuple[EvalFailure | None, EvalScores]
type InitItem = tuple[int, Obj, Obj, bool]
type InitResult = list[tuple[int, str, Obj]]
type SummaryItem = tuple[int, Obj, Obj]
type ReportRow = tuple[int, Obj, bool, Obj | None]
type SummaryRows = list[tuple[int, Obj]]

# Cancellation tokens are opaque callables supplied by the caller.
type CheckCancelled = Any
type ProgressCallback = Any


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
    score = fmean(ordered_scores)
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


def _seal_data(root: Path, cache: dict[str, Any] | None):
    if cache is not None and "reader" in cache:
        return cache["reader"], cache["seal"], cache["by_legacy"]
    reader = SealedH1Reader(root)
    seal = reader.payload()
    by_legacy = {e["legacy_path"]: e for e in seal["plan"]["files"]} if seal is not None else {}
    if cache is not None:
        cache.update(reader=reader, seal=seal, by_legacy=by_legacy)
    return reader, seal, by_legacy


def _pair_h1_weight(root: Path, contract: str, cache: dict[str, Any] | None) -> int:
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
    root: Path, item: dict[str, Any], timeframe: str, *, _seal_cache: dict[str, Any] | None = None
) -> int:
    if timeframe == "1h" and item.get("universe") == "pair_screen":
        parts = str(item.get("pair") or "").split("/")
        if len(parts) == 2:
            return _pair_h1_weight(root, parts[1], _seal_cache)
    return 1000 if timeframe == "1h" else 200


# Compatibility for earlier private consumers.
_estimate_weight = estimate_weight
