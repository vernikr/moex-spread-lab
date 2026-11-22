"""Worker-local cohort initialization, evaluation and report tasks."""

from __future__ import annotations

from typing import Any, cast

from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData
from moex_spread_lab.strategies.funding_carry.carry import CarryStrategyError

from .proof_kernel import StudyWindow
from .strategy_catalog import get_strategy_axes
from .worker_window import _worker_service

_WORKER_COHORTS: dict[str, dict[int, tuple[dict[str, Any], StudyWindow]]] = {}


def _prepare_worker_case(svc, native, settings, benchmark, calendar):
    # Worker startup is lazy: discovery happens here, not at import time.
    strategy_axes = get_strategy_axes()
    initial = dict(native["parameters"])
    for name, axis in strategy_axes[native["strategy"]].items():
        if name in initial:
            initial[name] = min(axis.high, max(axis.low, initial[name]))
    try:
        case = StudyWindow(
            svc,
            {**native, "parameters": initial},
            settings,
            benchmark,
            calendar,
        )
    except CarryStrategyError:
        if native["strategy"] != "funding_only" or initial["lookback"] == 5:
            raise
        case = StudyWindow(
            svc,
            {**native, "parameters": {"lookback": 5}},
            settings,
            benchmark,
            calendar,
        )
    return case


def _worker_init_cohort(
    cohort_id: str,
    root_str: str,
    settings: ResearchSettings,
    benchmark: tuple[list[str], dict[str, float]],
    items: list[tuple[int, dict[str, Any], dict[str, Any], bool]],
) -> list[tuple[int, str, dict[str, Any]]]:
    """Initialize assigned StudyWindows inside the worker and keep them sticky."""
    svc = _worker_service(root_str)
    # The worker-service factory always installs CalendarData for this process.
    calendar = cast(CalendarData, svc._calendar)
    bucket = _WORKER_COHORTS.setdefault(cohort_id, {})
    results: list[tuple[int, str, dict[str, Any]]] = []
    for idx, target, native, checked in items:
        try:
            if checked and target["universe"] == CALENDAR_UNIVERSE:
                calendar.check_training_window(
                    target["asset"], target["pair"], native.get("quantity", 1), settings
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
    point_by_strategy: dict[str, dict[str, Any]],
    indices: list[int] | None = None,
    exclude_idx: int | None = None,
) -> tuple[tuple[int, str, str] | None, list[tuple[int, float, bool]]]:
    """Evaluate `indices` (or all shard indices) in canonical order; short-circuit on fail."""
    bucket = _WORKER_COHORTS.get(cohort_id, {})
    target_indices = sorted(indices) if indices is not None else sorted(bucket)
    scores: list[tuple[int, float, bool]] = []
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


def _worker_eval_plateau_batch(
    cohort_id: str,
    points: list[tuple[int, dict[str, dict[str, Any]]]],
) -> dict[int, tuple[tuple[int, str, str] | None, list[tuple[int, float, bool]]]]:
    """Evaluate all uncached plateau neighbor points across this shard's windows."""
    bucket = _WORKER_COHORTS.get(cohort_id, {})
    ordered_indices = sorted(bucket)
    out: dict[int, tuple[tuple[int, str, str] | None, list[tuple[int, float, bool]]]] = {}
    for p_idx, point_by_strategy in points:
        scores: list[tuple[int, float, bool]] = []
        failure = None
        for idx in ordered_indices:
            target, case = bucket[idx]
            m = case.fit.measure(point_by_strategy[case.request["strategy"]])
            score = m.get("rank_score")
            if score is None:
                failure = (idx, target["pair"], m.get("reason", "нет допустимого результата"))
                break
            scores.append((idx, float(score), m["utility"] is not None))
        out[p_idx] = (failure, scores)
    return out


def _worker_report_cohort(
    cohort_id: str,
    point_by_strategy: dict[str, dict[str, Any]],
    best_is_none: bool,
    settings: ResearchSettings,
) -> list[tuple[int, dict[str, Any], bool, dict[str, Any] | None]]:
    """Build final member evaluations and report payloads in parallel."""
    bucket = _WORKER_COHORTS.get(cohort_id, {})
    rows: list[tuple[int, dict[str, Any], bool, dict[str, Any] | None]] = []
    for idx in sorted(bucket):
        target, case = bucket[idx]
        local = point_by_strategy[case.request["strategy"]]
        full = case.full
        exec_ref = bool(full.get("execution_reference", False))
        selection = {
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
        row = {
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
