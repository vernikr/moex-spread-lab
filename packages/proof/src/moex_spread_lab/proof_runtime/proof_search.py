"""Top-level entry point and public facade for bounded execution-proof research runs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, cast

from moex_spread_lab.core.evaluation_periods import (
    DEFAULT_INITIAL_CAPITAL_RUB,
    EvaluationSplit,
    ResearchSettings,
)
from moex_spread_lab.core.w0 import W0Error

from . import evaluation as _evaluation
from . import proof_batch as _proof_batch
from . import proof_kernel as _proof_kernel
from . import proof_strategy_search as _proof_strategy_search
from . import request_validation as _request_validation
from . import service as _service
from .axis import Axis
from .landscape import AMBER, GREEN, Landscape
from .proof_kernel import ENTRY, MIN_SESSIONS, TRIALS
from .proof_seed import SEED, derive_seed, resolve_seed
from .request_validation import EXECUTION_AXES
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
_Search = Callable[..., _Obj | None]
_ProgressFn = Callable[[_Progress], None]
_PlateauFn = Callable[[int, int], None]


class _WindowLike(Protocol):
    seed: _Obj
    fit: Any

    def report(self, point: Any) -> _Report: ...

    def admission(self) -> str | None: ...

    def lamp_warning(self) -> str | None: ...


def _noop() -> None:
    """Default cancellation probe: a run that is never asked to stop."""


# The facade re-exports these helpers for out-of-tree readers and the web routes, so
# they stay private in their owning modules and are read from the module namespace.
_KERNEL = _proof_kernel.__dict__
_SERVICE = _service.__dict__

_run_id = cast(Callable[[_Obj], str], _KERNEL["_run_id"])
_set_measure_provider = cast(
    Callable[[Callable[[], _Measure]], None], _KERNEL["_set_measure_provider"]
)
_admission = cast(Callable[..., str | None], _KERNEL["_admission"])
_measure = cast(_Measure, _KERNEL["_measure"])
_measure_scalar_train = cast(_Measure, _KERNEL["_measure_scalar_train"])
_tune = cast(Callable[..., Any], _KERNEL["_tune"])
_ordered = cast(Callable[[Mapping[str, float]], bool], _KERNEL["_ordered"])
_lamps = cast(Callable[..., _Obj], _KERNEL["_lamps"])
_excess_curve = cast(Callable[[Any, _Marks], _Marks], _evaluation.__dict__["_excess_curve"])
account_checks = cast(Callable[[Any], _Obj], _evaluation.__dict__["account_checks"])
_annualized_returns = cast(
    Callable[[Any, Any, Any], _Obj], _evaluation.__dict__["_annualized_returns"]
)
_identity = cast(Callable[[_Request], _Obj], _request_validation.__dict__["_identity"])
_validate = cast(Callable[[_Request], None], _request_validation.__dict__["_validate"])
execution_axes = cast(
    Callable[[_Request], dict[str, Any]], _request_validation.__dict__["execution_axes"]
)
compact_result = cast(Callable[[Any], _Obj], _request_validation.__dict__["compact_result"])
_benchmark_marks = cast(Callable[[Path], _Benchmark], _SERVICE["_benchmark_marks"])
_mark = cast(Callable[[list[str], dict[str, float], str], float], _SERVICE["_mark"])
ProofService: Any = _SERVICE["ProofService"]
reference_eval: Callable[[], bool] = _SERVICE["reference_eval"]
StudyWindow = cast(Callable[..., _WindowLike], _KERNEL["StudyWindow"])

_set_measure_provider(lambda: _measure)


def __getattr__(name: str):
    """Deprecated aliases kept for out-of-tree readers; use the accessors."""
    if name == "STRATEGY_AXES":
        return get_strategy_axes()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AMBER",
    "DEFAULT_INITIAL_CAPITAL_RUB",
    "ENTRY",
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
        search = _strategy_search if request.get("optimize_strategy") else _batch_search
        return cast(
            _Report, search(service, request, settings, benchmark, check_cancelled, on_progress)
        )
    if request.get("optimize_strategy"):
        raise W0Error("Выбор стратегии выполняется только запросом подбора.")
    if "targets" in request:
        raise W0Error("Журнал исполнения относится к одному открытому окну.")
    case = StudyWindow(service, request, settings, benchmark)
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


def _batch_search(
    service: Any,
    request: _Request,
    settings: ResearchSettings,
    benchmark: _Benchmark,
    check_cancelled: _Check,
    on_progress: _ProgressFn | None,
) -> _Obj | None:
    search = cast(_Search, _proof_batch.__dict__["batch_search"])
    return search(service, request, settings, benchmark, check_cancelled, on_progress=on_progress)


def _strategy_search(
    service: Any,
    request: _Request,
    settings: ResearchSettings,
    benchmark: _Benchmark,
    check_cancelled: _Check,
    on_progress: _ProgressFn | None,
) -> _Obj | None:
    search = cast(_Search, _proof_strategy_search.__dict__["strategy_search"])
    return search(service, request, settings, benchmark, check_cancelled, on_progress=on_progress)


def _plateau(case: _WindowLike, on_progress: _ProgressFn | None) -> _Obj:
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
