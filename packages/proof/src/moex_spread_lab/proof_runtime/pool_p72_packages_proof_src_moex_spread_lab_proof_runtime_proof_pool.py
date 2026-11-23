"""Typed worker bindings and shard planning for the pooled cohort facade (Issue #91)."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any, cast

from moex_spread_lab.core.evaluation_periods import ResearchSettings

from . import worker_cohort as _worker_cohort
from . import worker_window as _worker_window
from .cohort_scores import (
    EvalResult,
    InitItem,
    InitResult,
    Obj,
    PointMap,
    ReportRow,
    SummaryItem,
    SummaryRows,
    combine_shards,
    failure_payload,
    score_payload,
)
from .proof_kernel import StudyWindow
from .worker_weight import estimate_weight as estimate_weight

# The pool reaches its task functions through module namespaces so that a host can
# patch ``proof_pool`` attributes without a second binding chain. Reading the same
# objects through module dicts keeps those live bindings, while the casts below
# give the facade whole-file strict types for worker payloads.
_wc, _ww = (_worker_cohort.__dict__, _worker_window.__dict__)

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
