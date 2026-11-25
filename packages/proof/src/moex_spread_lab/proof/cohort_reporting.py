"""Build final cohort reports without rerunning the selected search."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from statistics import fmean
from typing import Any, Protocol, cast

import optuna

from moex_spread_lab.core.w0 import run_id as _run_id

from .axis import Axis
from .landscape import Landscape
from .landscape import lamps as _lamps
from .proof_kernel import resolve_seed

_Obj = dict[str, Any]
_Metrics = dict[str, Any]
_Rates = list[float | None]
_Progress = Callable[[int, int], None]
_OnProgress = Callable[[dict[str, Any]], None]
# `landscape` is not strict-typed yet; read its private helper through the module
# namespace so this strict module inherits a callable signature instead of Unknown.


class _CaseLike(Protocol):
    """A cohort member that can replay one parameter vector into a report."""

    request: dict[str, Any]
    full: dict[str, Any]

    def report(self, point: Mapping[str, float]) -> dict[str, Any]: ...


class _LampCaseLike(_CaseLike, Protocol):
    """A member that also carries the training landscape used by the lamps."""

    seed: dict[str, Any]
    fit: Landscape

    def report(self, point: Mapping[str, float]) -> dict[str, Any]: ...


class _SettingsLike(Protocol):
    validation_percent: float
    random_seed: int

    def metadata(self) -> dict[str, float | bool | str]: ...


class _GroupLike(Protocol):
    active: str
    axes: dict[str, Axis]
    seed: dict[str, Any]
    members: list[tuple[_Obj, Any]]
    land: Landscape

    def plateau(
        self, point: Mapping[str, float], *, on_progress: _Progress | None = None
    ) -> Any: ...

    def report_members(
        self,
        point: Mapping[str, float],
        best: _Obj | None,
        settings: _SettingsLike,
        check_cancelled: Callable[[], None],
        on_progress: _Progress | None = None,
    ) -> tuple[list[_Obj], bool, _Obj | None]: ...


@dataclass(frozen=True)
class CohortReport:
    request: dict[str, Any]
    settings: Any
    group: Any
    best: dict[str, Any] | None
    trials: int
    attempts: list[dict[str, Any]]
    check_cancelled: Callable[[], None]
    on_progress: Callable[[dict[str, Any]], None] | None


def _progress_event(stage: str, done: int, total: int, percent: float, label: str) -> _Obj:
    return {"stage": stage, "done": done, "total": total, "percent": percent, "label": label}


def _optional_progress(
    on_progress: _OnProgress | None, stage: str, base: float, span: float, template: str
) -> _Progress | None:
    """Scale one callback into the fixed percentage band of its stage."""
    if on_progress is None:
        return None

    def report(done: int, total: int) -> None:
        on_progress(
            _progress_event(
                stage,
                done,
                total,
                round(base + span * (done / max(total, 1)), 1),
                template.format(done=done, total=total),
            )
        )

    return report


def _report_plateau(
    group: _GroupLike,
    point: Mapping[str, float],
    enabled: bool,
    on_progress: _OnProgress | None,
) -> Any:
    if not enabled:
        return None
    if on_progress is not None:
        on_progress(_progress_event("plateau", 0, 1, 94.0, "Расчёт светофора устойчивости…"))
    return group.plateau(
        point,
        on_progress=_optional_progress(
            on_progress, "plateau", 94.0, 5.0, "Светофор устойчивости: {done}/{total}"
        ),
    )


def _member_rate(row: _Obj) -> float | None:
    """Per-member validation excess; a member without a result is not a zero."""
    evaluation = cast(_Obj, row.get("evaluation") or {})
    result = cast(_Obj, evaluation.get("result") or {})
    return cast(float | None, result.get("annualized_excess_pp"))


def build_cohort_result(report: CohortReport) -> _Obj:
    """Report the selected winner, preserving training and validation separation."""
    request, settings, group = report.request, report.settings, report.group
    best, trials, attempts = report.best, report.trials, report.attempts
    check_cancelled, on_progress = report.check_cancelled, report.on_progress
    axes, seed, members, land = group.axes, group.seed, group.members, group.land
    first = cast(_CaseLike, members[0][1])
    records = [
        {"trial": i, "parameters": {k: p[k] for k in axes}, **land.measure(p)}
        for i, p in enumerate(attempts)
    ]
    point = best or seed
    if on_progress is not None:
        on_progress(
            _progress_event("report", 0, len(members), 88.0, f"Финальный расчёт: 0/{len(members)}…")
        )
    rows, any_exec_ref, single = group.report_members(
        point,
        best,
        settings,
        check_cancelled,
        on_progress=_optional_progress(
            on_progress, "report", 88.0, 6.0, "Финальный расчёт: {done}/{total}"
        ),
    )
    rates: _Rates = [_member_rate(row) for row in rows]
    complete = [rate for rate in rates if rate is not None]
    summary = {
        "member_count": len(rows),
        "execution_reference": any_exec_ref,
        "unit": "annualized_excess_pp",
        "members": rows,
        **settings.metadata(),
        "result_basis": "validation" if settings.validation_percent else "training",
        "training_mean_pp": land.measure(best)["rank_score"] if best else None,
        "result_mean_pp": fmean(complete) if best and len(complete) == len(rates) else None,
    }
    native_single = len(members) == 1 and first.request["strategy"] == group.active
    plateau = _report_plateau(group, point, bool(best or native_single), on_progress)
    result = {
        "schema_version": "proof-search.v3",
        "method": "Optuna TPESampler",
        "library_version": optuna.__version__,
        "seed": settings.random_seed,
        "seed_used": resolve_seed(settings.random_seed),
        "trial_count": trials,
        "candidates": records,
        "selection_basis": "training",
        "objective": "arithmetic_mean_of_per_window_strategy_CAGR_minus_LQDT_CAGR_pp",
        "status": "maximum_found" if best else "no_admissible_candidate",
        "selected_parameters": {k: best[k] for k in axes} if best else None,
        "selection": {k: v for k, v in request.items() if k != "targets"},
        "group": summary,
        "plateau": plateau,
        "reason": None if best else "Нет допустимого результата сразу для всех выбранных окон.",
    }
    if len(rows) == 1 and single:
        result.update(
            input_run_id=first.full["run_id"],
            result_run_id=single["run_id"],
            evaluation=single["evaluation"],
        )
    return {**result, "run_id": _run_id(result)}


def unavailable(case: _LampCaseLike, reason: str) -> _Obj:
    """Report an unchecked cohort whose window was not admissible."""
    candidates: list[_Obj] = []
    result = {
        "schema_version": "proof-search.v3",
        "method": "Optuna TPESampler",
        "status": "unavailable",
        "trial_count": 0,
        "candidates": candidates,
        "selected_parameters": None,
        "reason": reason,
        "input_run_id": case.full["run_id"],
        "selection": case.request,
        "evaluation": case.report(case.seed)["evaluation"],
        "plateau": _lamps(case.fit, case.seed, reason),
    }
    return {**result, "run_id": _run_id(result)}


# Compatibility facade: `proof_batch` re-exports the private report hook by name.
_unavailable = unavailable


# Typed internal exports used by other proof modules.
shared_unavailable = _unavailable
