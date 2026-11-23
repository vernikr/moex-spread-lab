"""One Optuna objective on a fixed, equally weighted cohort of independent proof windows."""

import inspect
from dataclasses import dataclass as dataclass
from statistics import fmean as fmean

import optuna as optuna

from moex_spread_lab.core.contracts import InstrumentContext as InstrumentContext
from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.core.w0 import _run_id as _run_id
from moex_spread_lab.strategies.funding_carry.carry import CarryStrategyError as CarryStrategyError

from .cohort_models import _Cohort as _Cohort
from .cohort_models import _RemoteCohort as _RemoteCohort
from .cohort_prepare import PreparationInputs, prepare_cohort
from .cohort_prepare_helpers import MAX_TARGETS as MAX_TARGETS
from .cohort_prepare_helpers import _active_seed as _active_seed
from .cohort_prepare_helpers import _confirmation as _confirmation
from .cohort_prepare_helpers import _filter_training as _filter_training
from .cohort_prepare_helpers import _intersect_axes as _intersect_axes
from .cohort_prepare_helpers import _native_request as _native_request
from .cohort_prepare_helpers import (
    _targets,
)
from .cohort_prepare_helpers import _translate as _translate
from .cohort_reporting import CohortReport, build_cohort_result
from .cohort_reporting import _unavailable as _unavailable
from .phase_metrics import measure_phase
from .proof_kernel import (
    TRIALS,
    StudyWindow,
    _tune,
    _validate,
    resolve_seed,
)
from .proof_kernel import (
    Axis as Axis,
)
from .proof_kernel import _lamps as _lamps
from .proof_kernel import _ordered as _ordered
from .proof_pool import _estimate_weight, get_worker_pool
from .remote_cohort_types import _RemoteCase as _RemoteCase
from .remote_cohort_types import _RemoteSplit as _RemoteSplit
from .remote_cohort_types import _TrainingSlice as _TrainingSlice
from .request_validation import _identity as _identity
from .service import _benchmark_marks
from .strategy_catalog import get_strategy_axes
from .strategy_catalog import resolve_strategy_registry as resolve_strategy_registry

_DEFAULT_STUDY_WINDOW = StudyWindow


def _call_with_optional_progress(fn, *args, on_progress=None, **kwargs):
    if on_progress is not None:
        try:
            params = inspect.signature(fn).parameters.values()
        except (TypeError, ValueError):
            params = ()
        if any(p.name == "on_progress" or p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
            kwargs["on_progress"] = on_progress
    return fn(*args, **kwargs)


def _can_use_pool(service, calendar, valid_specs, timeframe: str) -> bool:
    from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData

    if StudyWindow is not _DEFAULT_STUDY_WINDOW:
        return False
    if type(calendar) is not CalendarData:
        return False
    pool = get_worker_pool(service)
    est = sum(_estimate_weight(service.root, t, timeframe) for _, t, _ in valid_specs)
    return pool.should_use(len(valid_specs), est)


@measure_phase("prepare_cohort")
def _prepare_cohort(
    service,
    request,
    settings,
    benchmark,
    check_cancelled,
    *,
    calendar=None,
    on_progress=None,
    registry=None,
):
    return prepare_cohort(
        PreparationInputs(
            service,
            request,
            settings,
            benchmark,
            check_cancelled,
            calendar,
            on_progress,
            registry,
            StudyWindow,
            _can_use_pool,
            get_worker_pool,
            _unavailable,
        )
    )


def checked_plateau(
    service,
    request,
    *,
    settings_override=None,
    check_cancelled=lambda: None,
    on_progress=None,
    registry=None,
):
    """Read-only probes at one shared point. No focus, Optuna trials, logs or implicit exclusions."""
    request = {
        k: request[k]
        for k in (
            "targets",
            "strategy",
            "sizing",
            "quantity",
            "slippage_ticks",
            "parameters",
            "fill",
        )
        if k in request
    }
    _validate(request, registry=registry)
    if not request.get("targets"):
        raise W0Error("Для группового светофора нужны отмеченные окна.")
    targets = _targets(request)
    strategy_axes = get_strategy_axes(registry)
    active = request.get("strategy")
    if active not in strategy_axes or set(request.get("parameters", {})) != set(
        strategy_axes[active]
    ):
        raise W0Error("Для светофора укажите стратегию и все её общие параметры.")
    settings = settings_override or ResearchSettings.load(service.root)
    benchmark = _benchmark_marks(service.root / "data/benchmark/lqdt-lineage.json")
    if on_progress is not None:
        on_progress(
            {
                "stage": "prepare",
                "done": 0,
                "total": len(targets),
                "percent": 5.0,
                "label": f"Светофор группы: подготовка 0/{len(targets)}…",
            }
        )
    group = _call_with_optional_progress(
        _prepare_cohort,
        service,
        request,
        settings,
        benchmark,
        check_cancelled,
        calendar=service._calendar,
        registry=registry,
        on_progress=(
            None
            if on_progress is None
            else lambda done, total: on_progress(
                {
                    "stage": "prepare",
                    "done": done,
                    "total": total,
                    "percent": round(5.0 + 40.0 * (done / max(total, 1)), 1),
                    "label": f"Светофор группы: подготовка {done}/{total}",
                }
            )
        ),
    )
    base = {
        "scope": "checked",
        "member_count": len(targets),
        **settings.metadata(),
        "execution_reference": (
            request.get("strategy") != "funding_only"
            and request.get("fill", settings.price_fill) == "reference_same_bar"
        ),
        "basis": "training",
    }
    if isinstance(group, dict):
        return {
            **base,
            "status": "unavailable",
            "excluded": group["excluded"],
            "plateau": None,
        }
    try:
        if on_progress is not None:
            on_progress(
                {
                    "stage": "plateau",
                    "done": 0,
                    "total": 1,
                    "percent": 45.0,
                    "label": "Светофор группы: расчёт плато…",
                }
            )
        plateau = group.plateau(
            group.seed,
            on_progress=(
                None
                if on_progress is None
                else lambda done, total: on_progress(
                    {
                        "stage": "plateau",
                        "done": done,
                        "total": total,
                        "percent": round(45.0 + 53.0 * (done / max(total, 1)), 1),
                        "label": f"Светофор группы: проверка плато {done}/{total}",
                    }
                )
            ),
        )
        return {**base, "status": "measured", "plateau": plateau}
    finally:
        group.close()


def batch_search(
    service, request, settings, benchmark, check_cancelled, *, on_progress=None, registry=None
):
    targets = _targets(request)
    if on_progress is not None:
        on_progress(
            {
                "stage": "prepare",
                "done": 0,
                "total": len(targets),
                "percent": 2.0,
                "label": f"Подготовка окон: 0/{len(targets)}…",
            }
        )
    group = _call_with_optional_progress(
        _prepare_cohort,
        service,
        request,
        settings,
        benchmark,
        check_cancelled,
        calendar=service._calendar,
        on_progress=(
            None
            if on_progress is None
            else lambda done, total: on_progress(
                {
                    "stage": "prepare",
                    "done": done,
                    "total": total,
                    "percent": round(2.0 + 10.0 * (done / max(total, 1)), 1),
                    "label": f"Подготовка окон: {done}/{total}",
                }
            )
        ),
    )
    if isinstance(group, dict):
        return group
    try:
        axes, seed, members, land = group.axes, group.seed, group.members, group.land

        best, trials, attempts = _call_with_optional_progress(
            _tune,
            land,
            seed,
            axes,
            min(len(case.split.training) for _, case in members),
            # The base seed is drawn once per run, so seed 0 makes every run
            # different while one run stays internally consistent.
            random_seed=resolve_seed(settings.random_seed),
            seed_label="cohort",
            on_progress=(
                None
                if on_progress is None
                else lambda done, total: on_progress(
                    {
                        "stage": "search",
                        "done": done,
                        "total": total or TRIALS,
                        "percent": round(12.0 + 76.0 * (done / max(total or TRIALS, 1)), 1),
                        "label": f"Optuna: испытание {done}/{total}",
                    }
                )
            ),
        )
        return _cohort_result(
            request,
            settings,
            group,
            best,
            trials,
            attempts,
            check_cancelled,
            on_progress=on_progress,
        )
    finally:
        group.close()


def _cohort_result(
    request, settings, group, best, trials, attempts, check_cancelled, *, on_progress=None
):
    """Compatibility facade for the final cohort report."""
    return build_cohort_result(
        CohortReport(request, settings, group, best, trials, attempts, check_cancelled, on_progress)
    )
