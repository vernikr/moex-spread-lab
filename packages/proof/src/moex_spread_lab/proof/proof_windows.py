"""Compact, read-only simulation results for the visible window table."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData

from .proof_kernel import StudyWindow
from .proof_pool import (
    CohortWorkerPool,
    await_chunks,
    estimate_weight,
    plan_shards,
    service_pool_factory,
)
from .request_validation import compact_result as compact_result
from .request_validation import identity as _identity
from .request_validation import validate as _validate
from .service import benchmark_marks as _benchmark_marks
from .worker_cohort import worker_service
from .worker_metrics import submit_worker

MAX_WINDOW_REQUESTS = 128
_DEFAULT_STUDY_WINDOW = StudyWindow

type _Obj = dict[str, Any]


def _window_identities(request: Any) -> list[_Obj]:
    if not isinstance(request, dict) or request.keys() != {"requests"}:
        raise W0Error("Для таблицы нужен список requests.")
    raw: Any = cast(dict[str, Any], request)["requests"]
    requests: list[Any] = raw
    if not isinstance(raw, list) or not 1 <= len(requests) <= MAX_WINDOW_REQUESTS:
        raise W0Error(f"В таблице допустимо 1–{MAX_WINDOW_REQUESTS} окон за запрос.")
    identities: list[_Obj] = []
    seen: set[tuple[tuple[str, Any], ...]] = set()
    for item in requests:
        if not isinstance(item, dict) or "targets" in item:
            raise W0Error("Строка таблицы должна описывать одно окно, не группу.")
        target = _identity(cast(_Obj, item))
        key = tuple(sorted(target.items()))
        if key in seen:
            raise W0Error("Окно таблицы указано дважды.")
        seen.add(key)
        identities.append(target)
    return identities


def _progress(on_progress: Callable[[_Obj], None] | None, done: int, total: int) -> None:
    if on_progress is not None:
        on_progress(
            {
                "stage": "windows",
                "done": done,
                "total": total,
                "percent": 4.0 if done == 0 else round(4.0 + 94.0 * (done / max(total, 1)), 1),
                "label": f"Окна: {done}/{total}",
            }
        )


def _summarize_window(
    service: Any,
    item: _Obj,
    target: _Obj,
    settings: ResearchSettings,
    benchmark: tuple[list[str], dict[str, float]],
    calendar: Any,
) -> _Obj:
    try:
        _validate(item)
        case = StudyWindow(service, item, settings, benchmark, calendar)
        result: _Obj = compact_result(case.report(case.seed))
    except (W0Error, OSError) as exc:
        result = {
            "status": "unavailable",
            "trades": None,
            "annualized_excess_pp": None,
            "reason": str(exc),
        }
    return {"target": target, **result}


def _worker_window_summaries(
    root_str: str,
    settings: ResearchSettings,
    benchmark: tuple[list[str], dict[str, float]],
    items: list[tuple[int, _Obj, _Obj]],
) -> list[tuple[int, _Obj]]:
    svc = worker_service(root_str)
    calendar = svc.calendar_data
    return [
        (idx, _summarize_window(svc, item, target, settings, benchmark, calendar))
        for idx, item, target in items
    ]


def _pool_window_summaries(
    pool: CohortWorkerPool,
    root: Path,
    settings: ResearchSettings,
    benchmark: tuple[list[str], dict[str, float]],
    items: list[tuple[int, _Obj, _Obj]],
    check_cancelled: Callable[[], None],
    on_progress: Callable[[int, int], None] | None,
) -> list[_Obj]:
    n = len(items)
    w = min(pool.max_workers, n)
    slots = pool.ensure_workers(w)
    seal_cache: _Obj = {}
    weights = {
        idx: estimate_weight(root, item, settings.timeframe, _seal_cache=seal_cache)
        for idx, item, _ in items
    }
    batches = min(n, w * 2) if n >= w * 2 else w
    shards, _ = plan_shards(items, weights, batches)
    root_str = str(root.resolve())
    fut_weights = {
        submit_worker(
            slots[b_idx % w], _worker_window_summaries, root_str, settings, benchmark, shard
        ): len(shard)
        for b_idx, shard in enumerate(shards)
        if shard
    }
    rows: list[tuple[int, _Obj]] = await_chunks(fut_weights, n, check_cancelled, on_progress)
    return [row for _, row in rows]


def window_summaries(
    service: Any,
    request: Any,
    *,
    settings_override: ResearchSettings | None = None,
    check_cancelled: Callable[[], None] = lambda: None,
    on_progress: Callable[[_Obj], None] | None = None,
) -> dict[str, Any]:
    """One compact row per requested window on one shared settings snapshot."""
    identities = _window_identities(request)
    requests: list[_Obj] = request["requests"]
    total_windows = len(requests)
    _progress(on_progress, 0, total_windows)
    settings = settings_override or ResearchSettings.load(service.root)
    benchmark = _benchmark_marks(service.root / "data/benchmark/lqdt-lineage.json")
    calendar = service._calendar or CalendarData(service.root)
    if StudyWindow is _DEFAULT_STUDY_WINDOW and type(calendar) is CalendarData:
        pool = service_pool_factory(service)(service)
        est = sum(estimate_weight(service.root, t, settings.timeframe) for t in identities)
        if pool.should_use(total_windows, est):
            payload = [
                (idx, item, target)
                for idx, (item, target) in enumerate(zip(requests, identities, strict=False))
            ]
            rows = _pool_window_summaries(
                pool,
                service.root,
                settings,
                benchmark,
                payload,
                check_cancelled,
                None
                if on_progress is None
                else lambda done, total: _progress(on_progress, done, total),
            )
            return {**settings.metadata(), "rows": rows}
    rows: list[_Obj] = []
    for idx, (item, target) in enumerate(zip(requests, identities, strict=False), start=1):
        check_cancelled()
        rows.append(_summarize_window(service, item, target, settings, benchmark, calendar))
        _progress(on_progress, idx, total_windows)
    return {**settings.metadata(), "rows": rows}
