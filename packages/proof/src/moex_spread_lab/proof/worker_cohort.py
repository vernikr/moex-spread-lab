"""Worker-local cohort initialization, evaluation and report tasks."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, cast

from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData
from moex_spread_lab.strategies.funding_carry.carry import CarryStrategyError

from .axis import Axis
from .proof_kernel import StudyWindow
from .strategy_catalog import get_strategy_axes
from .worker_window import worker_service as _worker_service

# Wildcard surface frozen to the pre-P7.2 revision: star imports keep exposing exactly the
# bindings they exposed before the strict-typing pass. ``_WORKER_COHORTS`` and the task
# functions stay module attributes under their historical names because the pool facade and
# ``spawn`` subprocess targets resolve them here.
__all__ = [
    "CALENDAR_UNIVERSE",
    "Any",
    "CalendarData",
    "CarryStrategyError",
    "ResearchSettings",
    "StudyWindow",
    "W0Error",
    "annotations",
    "cast",
    "get_strategy_axes",
]

_Obj = dict[str, Any]
_Axes = dict[str, Axis]
_Failure = tuple[int, str, str]
_Scores = list[tuple[int, float, bool]]
_Evaluation = tuple[_Failure | None, _Scores]
_ReportRow = tuple[int, _Obj, bool, _Obj | None]

# The service factory stays private to `worker_window`; resolving it through the
# module facade keeps this file off that module's protected surface.


class _SplitLike(Protocol):
    training: Sequence[str]

    def metadata(self) -> _Obj: ...


class _WorkerService(Protocol):
    """Proof service handed to worker windows; the factory always installs its calendar."""

    calendar_data: CalendarData


class _FitLike(Protocol):
    def measure(self, values: _Obj) -> _Obj: ...


class _CaseLike(Protocol):
    """Structural view of the untyped `StudyWindow` case used by worker tasks."""

    request: _Obj
    strategy_axes: _Axes
    execution_axes: _Axes
    axes: _Axes
    seed: _Obj
    split: _SplitLike
    fit: _FitLike
    full: _Obj

    def admission(self) -> str | None: ...
    def lamp_warning(self) -> str | None: ...
    def report(self, point: _Obj) -> _Obj: ...


_WORKER_COHORTS: dict[str, dict[int, tuple[_Obj, _CaseLike]]] = {}


def _prepare_worker_case(
    svc: _WorkerService | None,
    native: _Obj,
    settings: ResearchSettings,
    benchmark: tuple[list[str], dict[str, float]],
    calendar: CalendarData | None,
) -> _CaseLike:
    # Worker startup is lazy: discovery happens here, not at import time.
    strategy_axes = get_strategy_axes()
    initial: _Obj = dict(cast(_Obj, native["parameters"]))
    for name, axis in strategy_axes[cast(str, native["strategy"])].items():
        if name in initial:
            initial[name] = min(axis.high, max(axis.low, initial[name]))
    try:
        case = cast(
            _CaseLike,
            StudyWindow(
                svc,
                {**native, "parameters": initial},
                settings,
                benchmark,
                calendar,
            ),
        )
    except CarryStrategyError:
        if native["strategy"] != "funding_only" or initial["lookback"] == 5:
            raise
        case = cast(
            _CaseLike,
            StudyWindow(
                svc,
                {**native, "parameters": {"lookback": 5}},
                settings,
                benchmark,
                calendar,
            ),
        )
    return case


def _worker_init_cohort(
    cohort_id: str,
    root_str: str,
    settings: ResearchSettings,
    benchmark: tuple[list[str], dict[str, float]],
    items: list[tuple[int, _Obj, _Obj, bool]],
) -> list[tuple[int, str, _Obj]]:
    """Initialize assigned StudyWindows inside the worker and keep them sticky."""
    svc = cast(_WorkerService, _worker_service(root_str))
    # The worker-service factory always installs CalendarData for this process.
    calendar = svc.calendar_data
    bucket = _WORKER_COHORTS.setdefault(cohort_id, {})
    results: list[tuple[int, str, _Obj]] = []
    for idx, target, native, checked in items:
        try:
            if checked and target["universe"] == CALENDAR_UNIVERSE:
                calendar.check_training_window(
                    cast(str, target["asset"]),
                    cast(str, target["pair"]),
                    cast(int, native.get("quantity", 1)),
                    settings,
                )
            case = _prepare_worker_case(svc, native, settings, benchmark, calendar)
            reason = case.admission()
            if reason:
                results.append((idx, "unavailable" if not checked else "error", {"reason": reason}))
                continue
            bucket[idx] = (target, case)
            results.append(
                (
                    idx,
                    "ok",
                    {
                        "strategy": case.request["strategy"],
                        "strategy_axes": case.strategy_axes,
                        "execution_axes": case.execution_axes,
                        "axes": case.axes,
                        "seed": case.seed,
                        "training_first": case.split.training[0],
                        "training_last": case.split.training[-1],
                        "training_count": len(case.split.training),
                        "lamp_warning": case.lamp_warning(),
                    },
                )
            )
        except (W0Error, OSError) as exc:
            results.append((idx, "error", {"reason": str(exc)}))
    return results


def _worker_release_cohort(cohort_id: str) -> None:
    _WORKER_COHORTS.pop(cohort_id, None)


def _worker_eval_indices(
    cohort_id: str,
    point_by_strategy: dict[str, _Obj],
    indices: list[int] | None = None,
    exclude_idx: int | None = None,
) -> _Evaluation:
    """Evaluate `indices` (or all shard indices) in canonical order; short-circuit on fail."""
    bucket = _WORKER_COHORTS.get(cohort_id, {})
    target_indices = sorted(indices) if indices is not None else sorted(bucket)
    scores: _Scores = []
    for idx in target_indices:
        if exclude_idx is not None and idx == exclude_idx:
            continue
        target, case = bucket[idx]
        m = case.fit.measure(point_by_strategy[cast(str, case.request["strategy"])])
        score = m.get("rank_score")
        if score is None:
            return (idx, target["pair"], m.get("reason", "нет допустимого результата")), scores
        scores.append((idx, float(score), m["utility"] is not None))
    return None, scores


def _worker_eval_plateau_batch(
    cohort_id: str,
    points: list[tuple[int, dict[str, _Obj]]],
) -> dict[int, _Evaluation]:
    """Evaluate all uncached plateau neighbor points across this shard's windows."""
    bucket = _WORKER_COHORTS.get(cohort_id, {})
    ordered_indices = sorted(bucket)
    out: dict[int, _Evaluation] = {}
    for p_idx, point_by_strategy in points:
        scores: _Scores = []
        failure: _Failure | None = None
        for idx in ordered_indices:
            target, case = bucket[idx]
            m = case.fit.measure(point_by_strategy[cast(str, case.request["strategy"])])
            score = m.get("rank_score")
            if score is None:
                failure = (idx, target["pair"], m.get("reason", "нет допустимого результата"))
                break
            scores.append((idx, float(score), m["utility"] is not None))
        out[p_idx] = (failure, scores)
    return out


def _worker_report_cohort(
    cohort_id: str,
    point_by_strategy: dict[str, _Obj],
    best_is_none: bool,
    settings: ResearchSettings,
) -> list[_ReportRow]:
    """Build final member evaluations and report payloads in parallel."""
    bucket = _WORKER_COHORTS.get(cohort_id, {})
    rows: list[_ReportRow] = []
    for idx in sorted(bucket):
        target, case = bucket[idx]
        local = point_by_strategy[cast(str, case.request["strategy"])]
        full = case.full
        exec_ref = bool(full.get("execution_reference", False))
        selection: _Obj = {
            **cast(_Obj, full["selection"]),
            **{k: local[k] for k in case.execution_axes},
            "parameters": {k: local[k] for k in case.strategy_axes},
        }
        if best_is_none and any(not a.contains(local[k]) for k, a in case.axes.items()):
            rows.append(
                (
                    idx,
                    {
                        "target": target,
                        "reason": "Исходные параметры не входят в общий диапазон.",
                    },
                    exec_ref,
                    None,
                )
            )
            continue
        training_rate = case.fit.measure(local).get("rank_score")
        row: _Obj = {
            "target": target,
            "selection": selection,
            "input_run_id": full["run_id"],
            "training_annualized_excess_pp": training_rate,
        }
        try:
            single = case.report(local)
            rows.append(
                (
                    idx,
                    {**row, "run_id": single["run_id"], "evaluation": single["evaluation"]},
                    exec_ref,
                    single,
                )
            )
        except (W0Error, OSError) as exc:
            rows.append(
                (
                    idx,
                    {
                        **row,
                        "evaluation": {
                            **settings.metadata(),
                            **case.split.metadata(),
                            "execution_reference": exec_ref,
                            "result": {"reason": str(exc)},
                        },
                    },
                    exec_ref,
                    None,
                )
            )
    return rows


# Explicit aliases preserve the original spawn callables and shared state while
# the historical private attributes remain the worker and facade patch points.
WORKER_COHORTS = _WORKER_COHORTS
prepare_worker_case = _prepare_worker_case
worker_eval_indices = _worker_eval_indices
worker_eval_plateau_batch = _worker_eval_plateau_batch
worker_init_cohort = _worker_init_cohort
worker_release_cohort = _worker_release_cohort
worker_report_cohort = _worker_report_cohort


# Typed internal exports used by other proof modules.
