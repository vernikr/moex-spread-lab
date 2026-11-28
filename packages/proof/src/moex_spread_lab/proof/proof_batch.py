"""One Optuna objective on a fixed, equally weighted cohort of independent proof windows."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping, Sequence, Sized
from dataclasses import dataclass as dataclass
from pathlib import Path
from statistics import fmean as fmean
from typing import Any, Protocol, cast

import optuna as optuna

from moex_spread_lab.core.contracts import InstrumentContext as InstrumentContext
from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies import StrategyRegistry
from moex_spread_lab.strategies.funding_carry.study import (
    CarryStrategyError as CarryStrategyError,
)

from .cohort_models import Cohort, RemoteCohort
from .cohort_prepare import MAX_TARGETS as MAX_TARGETS
from .cohort_prepare import PreparationInputs, prepare_cohort
from .cohort_prepare import targets as _targets
from .cohort_reporting import CohortReport, build_cohort_result
from .cohort_reporting import shared_unavailable as _unavailable
from .proof_kernel import TRIALS, StudyWindow, TuningOptions, resolve_seed
from .proof_kernel import Axis as Axis
from .proof_kernel import tune as _tune
from .proof_kernel import validate as _validate
from .proof_pool import service_pool_factory
from .proof_pool import shared_estimate_weight as _estimate_weight
from .request_validation import identity
from .service import shared_benchmark_marks as _benchmark_marks
from .strategy_catalog import get_strategy_axes
from .strategy_catalog import resolve_strategy_registry as resolve_strategy_registry
from .worker_metrics import measure_phase

_Obj = dict[str, Any]
_Progress = Callable[[_Obj], Any]
_Step = Callable[[int, int], Any]
_Cancel = Callable[[], None]
_ValidSpec = tuple[int, _Obj, _Obj]

_CALENDAR_ATTR = "_calendar"
_PREPARE_OPTS = frozenset({"calendar", "registry"})
_RESULT_OPTS = frozenset({"base_seed", "on_progress"})


class _ServiceLike(Protocol):
    root: Path


class _SplitLike(Protocol):
    training: Sized


class _MemberCaseLike(Protocol):
    split: _SplitLike


class _CohortLike(Protocol):
    active: str
    axes: dict[str, Axis]
    seed: _Obj
    members: list[tuple[_Obj, _MemberCaseLike]]
    land: Any

    def close(self) -> None: ...
    def plateau(self, point: _Obj, *, on_progress: _Step | None = None) -> Any: ...


_Cohort = Cohort
_RemoteCohort = RemoteCohort
_identity = identity

_DEFAULT_STUDY_WINDOW = StudyWindow


def _noop_cancel() -> None:
    return None


def _parse_options(options: Mapping[str, Any], allowed: frozenset[str]) -> None:
    unknown = set(options) - allowed
    if unknown:
        raise TypeError(f"unexpected keyword argument(s): {sorted(unknown)}")


def _emit_stage(
    on_progress: _Progress | None, stage: str, total: int, percent: float, label: str
) -> None:
    if on_progress is not None:
        on_progress({"stage": stage, "done": 0, "total": total, "percent": percent, "label": label})


def _step_progress(
    on_progress: _Progress | None,
    stage: str,
    start_pct: float,
    span_pct: float,
    label_prefix: str,
    *,
    default_total: int | None = None,
) -> _Step | None:
    if on_progress is None:
        return None

    def report(done: int, total: int) -> None:
        effective = (total or default_total) if default_total is not None else total
        on_progress(
            {
                "stage": stage,
                "done": done,
                "total": effective,
                "percent": round(start_pct + span_pct * (done / max(effective, 1)), 1),
                "label": f"{label_prefix} {done}/{total}",
            }
        )

    return report


def _call_with_optional_progress[R](
    fn: Callable[..., R],
    *args: Any,
    on_progress: Any = None,
    **kwargs: Any,
) -> R:
    if on_progress is not None:
        try:
            params: Iterable[inspect.Parameter] = inspect.signature(fn).parameters.values()
        except (TypeError, ValueError):
            params = ()
        if any(p.name == "on_progress" or p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
            kwargs["on_progress"] = on_progress
    return fn(*args, **kwargs)


def _can_use_pool(
    service: _ServiceLike,
    calendar: Any,
    valid_specs: Sequence[_ValidSpec],
    timeframe: str,
) -> bool:
    from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData

    if StudyWindow is not _DEFAULT_STUDY_WINDOW or type(calendar) is not CalendarData:
        return False
    pool = service_pool_factory(service)(service)
    est = sum(_estimate_weight(service.root, t, timeframe) for _, t, _ in valid_specs)
    return pool.should_use(len(valid_specs), est)


@measure_phase("prepare_cohort")
def _prepare_cohort(
    service: _ServiceLike,
    request: Mapping[str, Any],
    settings: ResearchSettings,
    benchmark: Any,
    check_cancelled: _Cancel,
    *,
    on_progress: _Step | None = None,
    **options: Any,
) -> _CohortLike | _Obj:
    _parse_options(options, _PREPARE_OPTS)
    return cast(
        _CohortLike | _Obj,
        prepare_cohort(
            PreparationInputs(
                service,
                request,
                settings,
                benchmark,
                check_cancelled,
                options.get("calendar"),
                on_progress,
                options.get("registry"),
                StudyWindow,
                _can_use_pool,
                service_pool_factory(service),
                _unavailable,
            )
        ),
    )


def checked_plateau(
    service: _ServiceLike,
    request: Mapping[str, Any],
    *,
    settings_override: ResearchSettings | None = None,
    check_cancelled: _Cancel = _noop_cancel,
    on_progress: _Progress | None = None,
    registry: StrategyRegistry | None = None,
) -> _Obj:
    """Read-only probes at one shared point. No focus, Optuna trials, logs or implicit exclusions."""
    filtered: _Obj = {
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
    _validate(filtered, registry=registry)
    if not filtered.get("targets"):
        raise W0Error("Для группового светофора нужны отмеченные окна.")
    targets = _targets(filtered)
    strategy_axes = get_strategy_axes(registry)
    active = filtered.get("strategy")
    if (
        not isinstance(active, str)
        or active not in strategy_axes
        or set(cast(Iterable[Any], filtered.get("parameters", {}))) != set(strategy_axes[active])
    ):
        raise W0Error("Для светофора укажите стратегию и все её общие параметры.")
    settings = settings_override or ResearchSettings.load(service.root)
    benchmark = _benchmark_marks(service.root / "data/benchmark/lqdt-lineage.json")
    _emit_stage(
        on_progress, "prepare", len(targets), 5.0, f"Светофор группы: подготовка 0/{len(targets)}…"
    )
    group = _call_with_optional_progress(
        _prepare_cohort,
        service,
        filtered,
        settings,
        benchmark,
        check_cancelled,
        calendar=getattr(service, _CALENDAR_ATTR),
        registry=registry,
        on_progress=_step_progress(
            on_progress, "prepare", 5.0, 40.0, "Светофор группы: подготовка"
        ),
    )
    base: _Obj = {
        "scope": "checked",
        "member_count": len(targets),
        **settings.metadata(),
        "execution_reference": (
            filtered.get("strategy") != "funding_only"
            and filtered.get("fill", settings.price_fill) == "reference_same_bar"
        ),
        "basis": "training",
    }
    if isinstance(group, dict):
        return {**base, "status": "unavailable", "excluded": group["excluded"], "plateau": None}
    try:
        _emit_stage(on_progress, "plateau", 1, 45.0, "Светофор группы: расчёт плато…")
        plateau = group.plateau(
            group.seed,
            on_progress=_step_progress(
                on_progress, "plateau", 45.0, 53.0, "Светофор группы: проверка плато"
            ),
        )
        return {**base, "status": "measured", "plateau": plateau}
    finally:
        group.close()


def batch_search(
    service: _ServiceLike,
    request: Mapping[str, Any],
    settings: ResearchSettings,
    benchmark: Any,
    check_cancelled: _Cancel,
    *,
    on_progress: _Progress | None = None,
    registry: StrategyRegistry | None = None,
) -> _Obj:
    targets = _targets(request)
    _emit_stage(on_progress, "prepare", len(targets), 2.0, f"Подготовка окон: 0/{len(targets)}…")
    group = _call_with_optional_progress(
        _prepare_cohort,
        service,
        request,
        settings,
        benchmark,
        check_cancelled,
        calendar=getattr(service, _CALENDAR_ATTR),
        on_progress=_step_progress(on_progress, "prepare", 2.0, 10.0, "Подготовка окон:"),
    )
    if isinstance(group, dict):
        return group
    try:
        axes, seed, members, land = group.axes, group.seed, group.members, group.land
        # The base seed is drawn once per run, so seed 0 makes every run
        # different while one run stays internally consistent; the drawn value
        # is both used by the sampler and reported as seed_used.
        base_seed = resolve_seed(settings.random_seed)
        best, trials, attempts = _tune(
            land,
            seed,
            axes,
            min(len(case.split.training) for _, case in members),
            TuningOptions(
                TRIALS,
                _step_progress(
                    on_progress, "search", 12.0, 76.0, "Optuna: испытание", default_total=TRIALS
                ),
                base_seed,
                "cohort",
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
            base_seed=base_seed,
            on_progress=on_progress,
        )
    finally:
        group.close()


def _cohort_result(
    request: Mapping[str, Any],
    settings: Any,
    group: Any,
    best: _Obj | None,
    trials: int,
    attempts: list[_Obj],
    check_cancelled: _Cancel,
    **options: Any,
) -> _Obj:
    """Compatibility facade for the final cohort report."""
    _parse_options(options, _RESULT_OPTS)
    on_progress = cast(Callable[[_Obj], None] | None, options.get("on_progress"))
    return build_cohort_result(
        CohortReport(
            cast(_Obj, request),
            settings,
            group,
            best,
            trials,
            attempts,
            check_cancelled,
            on_progress,
            cast(int, options["base_seed"]),
        )
    )


# Typed internal exports used by other proof modules.
call_with_optional_progress = _call_with_optional_progress
cohort_result = _cohort_result
shared_prepare_cohort = _prepare_cohort
