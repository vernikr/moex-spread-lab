"""Top-level entry point and public facade for bounded execution-proof research runs."""

from __future__ import annotations

from typing import Any

from .evaluation_periods import (
    DEFAULT_INITIAL_CAPITAL_RUB,
    EvaluationSplit,
    ResearchSettings,
)
from .proof import ProofService, _benchmark_marks, _mark, _run_id, reference_eval
from .proof_batch import batch_search
from .proof_kernel import (
    AMBER,
    ENTRY,
    EXECUTION_AXES,
    GREEN,
    MIN_SESSIONS,
    SEED,
    STRATEGY_AXES,
    TRIALS,
    Axis,
    Landscape,
    StudyWindow,
    _admission,
    _annualized_returns,
    _excess_curve,
    _identity,
    _lamps,
    _measure,
    _measure_scalar_train,
    _ordered,
    _set_measure_provider,
    _tune,
    _validate,
    account_checks,
    compact_result,
    derive_seed,
    execution_axes,
    resolve_seed,
)
from .proof_strategy_search import strategy_search
from .w0 import W0Error

_set_measure_provider(lambda: _measure)

__all__ = [
    "AMBER",
    "Axis",
    "DEFAULT_INITIAL_CAPITAL_RUB",
    "ENTRY",
    "EXECUTION_AXES",
    "EvaluationSplit",
    "GREEN",
    "Landscape",
    "MIN_SESSIONS",
    "ProofService",
    "ResearchSettings",
    "SEED",
    "STRATEGY_AXES",
    "StudyWindow",
    "TRIALS",
    "W0Error",
    "_admission",
    "_annualized_returns",
    "_benchmark_marks",
    "_excess_curve",
    "_identity",
    "_lamps",
    "_mark",
    "_measure",
    "_measure_scalar_train",
    "_ordered",
    "_run_id",
    "_tune",
    "_validate",
    "account_checks",
    "compact_result",
    "derive_seed",
    "execution_axes",
    "reference_eval",
    "research_run",
    "resolve_seed",
]


def research_run(
    service: ProofService,
    request: dict[str, Any],
    *,
    optimize: bool,
    include_plateau: bool = True,
    check_cancelled=lambda: None,
    on_progress=None,
):
    _validate(request)
    settings = ResearchSettings.load(service.root)
    benchmark = _benchmark_marks(service.root / "data/benchmark/lqdt-lineage.json")
    if optimize:
        if request.get("optimize_strategy"):
            return strategy_search(
                service,
                request,
                settings,
                benchmark,
                check_cancelled,
                on_progress=on_progress,
            )
        return batch_search(
            service,
            request,
            settings,
            benchmark,
            check_cancelled,
            on_progress=on_progress,
        )
    if request.get("optimize_strategy"):
        raise W0Error("Выбор стратегии выполняется только запросом подбора.")
    if "targets" in request:
        raise W0Error("Журнал исполнения относится к одному открытому окну.")
    if on_progress is not None:
        on_progress(
            {
                "stage": "prepare",
                "done": 0,
                "total": 1,
                "percent": 15.0,
                "label": "Загрузка и подготовка ряда…",
            }
        )
    case = StudyWindow(service, request, settings, benchmark)
    if on_progress is not None:
        on_progress(
            {
                "stage": "run",
                "done": 1,
                "total": 2 if include_plateau else 1,
                "percent": 35.0 if include_plateau else 60.0,
                "label": "Симуляция счёта и расчёт доходности…",
            }
        )
    report = case.report(case.seed)
    if include_plateau:
        warn = case.admission() or case.lamp_warning()
        if on_progress is not None:
            on_progress(
                {
                    "stage": "plateau",
                    "done": 0,
                    "total": 1,
                    "percent": 58.0,
                    "label": "Оценка устойчивости параметров…",
                }
            )
        report["plateau"] = _lamps(
            case.fit,
            case.seed,
            warn,
            on_progress=(
                None
                if on_progress is None
                else lambda done, total: on_progress(
                    {
                        "stage": "plateau",
                        "done": done,
                        "total": total,
                        "percent": round(58.0 + 37.0 * (done / max(total, 1)), 1),
                        "label": f"Оценка устойчивости: проверка {done}/{total}",
                    }
                )
            ),
        )
    return report
