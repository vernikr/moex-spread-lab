"""Top-level entry point and public facade for bounded execution-proof research runs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from moex_spread_lab.core.settings import (
    DEFAULT_INITIAL_CAPITAL_RUB,
    EvaluationSplit,
    ResearchSettings,
)
from moex_spread_lab.core.w0 import W0Error

from . import proof_batch as _proof_batch
from . import proof_strategy_search as _proof_strategy_search
from .evaluation import account_checks as account_checks
from .evaluation import annualized_returns as _annualized_returns
from .evaluation import excess_curve as _excess_curve
from .landscape import AMBER, GREEN, Axis, Landscape
from .landscape import lamps as _lamps
from .landscape import ordered as _ordered
from .proof_kernel import MIN_SESSIONS, SEED, TRIALS, derive_seed, resolve_seed
from .proof_kernel import StudyWindow as StudyWindow
from .proof_kernel import measure as _measure
from .proof_kernel import run_id as _run_id
from .proof_kernel import tune as _tune
from .request_validation import EXECUTION_AXES
from .request_validation import compact_result as compact_result
from .request_validation import execution_axes as execution_axes
from .request_validation import identity as _identity
from .request_validation import validate as _validate
from .service import ProofService as ProofService
from .service import reference_eval as reference_eval
from .service import shared_benchmark_marks as _benchmark_marks
from .service import shared_mark as _mark
from .strategy_catalog import get_proof_adapter_ids as get_proof_adapter_ids
from .strategy_catalog import get_strategy_axes as get_strategy_axes
from .strategy_catalog import get_strategy_registry as get_strategy_registry

_Obj = dict[str, Any]
_Request = Mapping[str, Any]
_Marks = dict[str, float]
_Progress = dict[str, Any]
_Report = dict[str, Any]
_Check = Callable[[], None]
_Measure = Callable[[Any, _Marks, EvaluationSplit, ResearchSettings], _Obj]
_Benchmark = tuple[list[str], dict[str, float]]
_ProgressFn = Callable[[_Progress], None]
_PlateauFn = Callable[[int, int], None]


def _noop() -> None:
    """Default cancellation probe: a run that is never asked to stop."""


__all__ = [
    "AMBER",
    "DEFAULT_INITIAL_CAPITAL_RUB",
    "EXECUTION_AXES",
    "GREEN",
    "MIN_SESSIONS",
    "SEED",
    "TRIALS",
    "Axis",
    "EvaluationSplit",
    "Landscape",
    "ProofService",
    "ResearchSettings",
    "StudyWindow",
    "W0Error",
    "_annualized_returns",
    "_benchmark_marks",
    "_excess_curve",
    "_identity",
    "_lamps",
    "_mark",
    "_measure",
    "_ordered",
    "_run_id",
    "_tune",
    "_validate",
    "account_checks",
    "compact_result",
    "derive_seed",
    "execution_axes",
    "get_proof_adapter_ids",
    "get_strategy_axes",
    "get_strategy_registry",
    "reference_eval",
    "research_run",
    "resolve_seed",
]


def research_run(
    service: Any,
    request: _Request,
    *,
    optimize: bool,
    settings_override: ResearchSettings | None = None,
    include_plateau: bool = True,
    check_cancelled: _Check = _noop,
    on_progress: _ProgressFn | None = None,
) -> _Report:
    _validate(request)
    settings = settings_override or ResearchSettings.load(Path(service.root))
    benchmark = _benchmark_marks(Path(service.root) / "data/benchmark/lqdt-lineage.json")
    if optimize:
        search = (
            _proof_strategy_search.strategy_search
            if request.get("optimize_strategy")
            else _proof_batch.batch_search
        )
        return search(
            service,
            cast(dict[str, Any], request),
            settings,
            benchmark,
            check_cancelled,
            on_progress=on_progress,
        )
    if request.get("optimize_strategy"):
        raise W0Error("Выбор стратегии выполняется только запросом подбора.")
    if "targets" in request:
        raise W0Error("Журнал исполнения относится к одному открытому окну.")
    case = StudyWindow(service, cast(dict[str, Any], request), settings, benchmark)
    _emit(on_progress, "prepare", 0, 1, 15.0, "Загрузка и подготовка ряда…")
    _emit(
        on_progress,
        "run",
        1,
        2 if include_plateau else 1,
        35.0 if include_plateau else 60.0,
        "Симуляция счёта и расчёт доходности…",
    )
    report = case.report(case.seed)
    if include_plateau:
        report["plateau"] = _plateau(case, on_progress)
    return report


def _plateau(case: StudyWindow, on_progress: _ProgressFn | None) -> _Obj:
    """Descriptive training-time lamps; no study, no holdout and no trial log."""
    warn = case.admission() or case.lamp_warning()
    _emit(on_progress, "plateau", 0, 1, 58.0, "Оценка устойчивости параметров…")
    return _lamps(case.fit, case.seed, warn, on_progress=_plateau_reporter(on_progress))


def _emit(
    on_progress: _ProgressFn | None,
    stage: str,
    done: int,
    total: int,
    percent: float,
    label: str,
) -> None:
    if on_progress is None:
        return
    on_progress({"stage": stage, "done": done, "total": total, "percent": percent, "label": label})


def _plateau_reporter(on_progress: _ProgressFn | None) -> _PlateauFn | None:
    """Monotonic 58%→95% progress for the plateau probes, shared with the UI stream."""
    if on_progress is None:
        return None

    def report(done: int, total: int) -> None:
        on_progress(
            {
                "stage": "plateau",
                "done": done,
                "total": total,
                "percent": round(58.0 + 37.0 * (done / max(total, 1)), 1),
                "label": f"Оценка устойчивости: проверка {done}/{total}",
            }
        )

    return report
