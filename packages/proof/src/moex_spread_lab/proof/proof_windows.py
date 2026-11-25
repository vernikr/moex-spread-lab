"""Compact, read-only simulation results for the visible window table.

No optimization, cohort pruning, saved trials or changes to per-window parameters.
Every row is measured by the exact same continuous proof/evaluation as its chart.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData

from .pool_reports import estimate_weight as _estimate_weight
from .proof_kernel import StudyWindow
from .proof_pool import get_worker_pool
from .request_validation import compact_result as compact_result
from .request_validation import identity as _identity
from .request_validation import validate as _validate
from .service import benchmark_marks as _benchmark_marks

MAX_WINDOW_REQUESTS = 128
_DEFAULT_STUDY_WINDOW = StudyWindow

# Normalized payload rows/targets shared with request_validation and the pool.
_Obj = dict[str, Any]


class _StudyWindowLike(Protocol):
    """Surface of StudyWindow used here: the seed point and its measured report."""

    seed: _Obj

    def report(self, point: Any) -> _Obj: ...


class _WindowPoolLike(Protocol):
    """Surface of CohortWorkerPool used here; sibling signatures stay untouched."""

    def should_use(self, count: int, estimated_bars: int) -> bool: ...

    def window_summaries(
        self,
        root: Path,
        settings: ResearchSettings,
        benchmark: tuple[list[str], dict[str, float]],
        items: list[tuple[int, _Obj, _Obj]],
        check_cancelled: Callable[[], None],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[_Obj]: ...


def _settings(root: Path, override: ResearchSettings | None) -> ResearchSettings:
    return override or ResearchSettings.load(root)


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


def window_summaries(
    service: Any,
    request: Any,
    *,
    settings_override: ResearchSettings | None = None,
    check_cancelled: Callable[[], None] = lambda: None,
    on_progress: Callable[[_Obj], None] | None = None,
) -> dict[str, Any]:
    """One compact row per requested window on one shared settings snapshot.

    `service` is the duck-typed proof facade (`root`, `_calendar`, pool hooks)
    forwarded unchanged to StudyWindow and the worker pool.
    """
    identities = _window_identities(request)
    requests: list[_Obj] = request["requests"]
    total_windows = len(requests)
    _progress(on_progress, 0, total_windows)
    settings = _settings(service.root, settings_override)
    benchmark = _benchmark_marks(service.root / "data/benchmark/lqdt-lineage.json")
    calendar = service._calendar or CalendarData(service.root)
    if StudyWindow is _DEFAULT_STUDY_WINDOW and type(calendar) is CalendarData:
        pool = cast(_WindowPoolLike, get_worker_pool(service))
        est = sum(_estimate_weight(service.root, t, settings.timeframe) for t in identities)
        if pool.should_use(len(requests), est):
            items = list(enumerate(zip(requests, identities, strict=False)))
            payload = [(idx, item, target) for idx, (item, target) in items]
            rows = pool.window_summaries(
                service.root,
                settings,
                benchmark,
                payload,
                check_cancelled,
                on_progress=(
                    None
                    if on_progress is None
                    else lambda done, total: _progress(on_progress, done, total)
                ),
            )
            return {**settings.metadata(), "rows": rows}
    rows: list[_Obj] = []
    for idx, (item, target) in enumerate(zip(requests, identities, strict=False), start=1):
        check_cancelled()
        try:
            _validate(item)
            case = cast(_StudyWindowLike, StudyWindow(service, item, settings, benchmark, calendar))
            result: _Obj = compact_result(case.report(case.seed))
        except (W0Error, OSError) as exc:
            result = {
                "status": "unavailable",
                "trades": None,
                "annualized_excess_pp": None,
                "reason": str(exc),
            }
        rows.append({"target": target, **result})
        _progress(on_progress, idx, total_windows)
    return {**settings.metadata(), "rows": rows}
