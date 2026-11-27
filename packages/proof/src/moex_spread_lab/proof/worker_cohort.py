"""Worker-local cohort initialization, evaluation and report tasks."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData
from moex_spread_lab.strategies.funding_carry.carry import CarryStrategyError

from .proof_kernel import StudyWindow
from .strategy_catalog import get_strategy_axes

if TYPE_CHECKING:
    from moex_spread_lab.core.settings import ResearchSettings

    from .service import ProofService

type _Obj = dict[str, Any]
type _Failure = tuple[int, str, str]
type _Scores = list[tuple[int, float, bool]]
type _Evaluation = tuple[_Failure | None, _Scores]
type _ReportRow = tuple[int, _Obj, bool, _Obj | None]

_WORKER_SERVICES: dict[str, ProofService] = {}
_WORKER_COHORTS: dict[str, dict[int, tuple[_Obj, Any]]] = {}


def worker_service(root_str: str) -> ProofService:
    svc = _WORKER_SERVICES.get(root_str)
    if svc is None:
        from .service import ProofService

        root = Path(root_str).resolve()
        svc = ProofService(root, calendar=CalendarData(root))
        _WORKER_SERVICES[root_str] = svc
    return svc


def _prepare_worker_case(
    svc: Any,
    native: _Obj,
    settings: Any,
    benchmark: Any,
    calendar: CalendarData | None,
) -> StudyWindow:
    # Worker startup is lazy: discovery happens here, not at import time.
    strategy_axes = get_strategy_axes()
    initial: _Obj = dict(native["parameters"])
    for name, axis in strategy_axes[native["strategy"]].items():
        if name in initial:
            initial[name] = min(axis.high, max(axis.low, initial[name]))
    try:
        return StudyWindow(svc, {**native, "parameters": initial}, settings, benchmark, calendar)
    except CarryStrategyError:
        if native["strategy"] != "funding_only" or initial["lookback"] == 5:
            raise
        return StudyWindow(
            svc, {**native, "parameters": {"lookback": 5}}, settings, benchmark, calendar
        )


def worker_init_cohort(
    cohort_id: str,
    root_str: str,
    settings: ResearchSettings,
    benchmark: tuple[list[str], dict[str, float]],
    items: list[tuple[int, _Obj, _Obj, bool]],
) -> list[tuple[int, str, _Obj]]:
    """Initialize assigned StudyWindows inside the worker and keep them sticky."""
    svc = worker_service(root_str)
    calendar: CalendarData = svc.calendar_data
    bucket = _WORKER_COHORTS.setdefault(cohort_id, {})
    results: list[tuple[int, str, _Obj]] = []
    for idx, target, native, checked in items:
        try:
            if checked and target["universe"] == CALENDAR_UNIVERSE:
                calendar.check_training_window(
                    target["asset"],
                    target["pair"],
                    native.get("quantity", 1),
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


def worker_release_cohort(cohort_id: str) -> None:
    _WORKER_COHORTS.pop(cohort_id, None)


def worker_eval_indices(
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
        m = case.fit.measure(point_by_strategy[case.request["strategy"]])
        score = m.get("rank_score")
        if score is None:
            return (idx, target["pair"], m.get("reason", "нет допустимого результата")), scores
        scores.append((idx, float(score), m["utility"] is not None))
    return None, scores


def worker_eval_plateau_batch(
    cohort_id: str,
    points: list[tuple[int, dict[str, _Obj]]],
) -> dict[int, _Evaluation]:
    """Evaluate all uncached plateau neighbor points across this shard's windows."""
    ordered = sorted(_WORKER_COHORTS.get(cohort_id, {}))
    return {
        p_idx: worker_eval_indices(cohort_id, point_by_strategy, ordered)
        for p_idx, point_by_strategy in points
    }


def worker_report_cohort(
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
        local = point_by_strategy[case.request["strategy"]]
        full = case.full
        exec_ref = bool(full.get("execution_reference", False))
        selection: _Obj = {
            **full["selection"],
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
