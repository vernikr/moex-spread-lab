"""Choose one strategy and one common vector on a fixed cohort's training periods.

Each declared family gets a full TPE budget, so no categorical branch is starved by
early pruning. Execution is not optimized: price strategies keep their chosen fill,
funding carry keeps its mandatory same-day settlement (explicit user decision).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

import optuna

from moex_spread_lab.core.timeframes import get_timeframe
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE as CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import run_id as _run_id
from moex_spread_lab.strategies.base import StrategyRegistry
from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData

from . import cohort_prepare_helpers as _cohort_prepare_helpers
from . import (
    families_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_strategy_search as _surface,
)
from . import proof_batch as _proof_batch
from . import proof_kernel as _proof_kernel
from . import strategy_search_prepare as _strategy_search_prepare
from .compat_hooks_p72_138 import choose_hook as _choose_hook
from .strategy_catalog import get_proof_adapter_ids as get_proof_adapter_ids
from .strategy_catalog import resolve_strategy_registry
from .trial_budget import budget

PRICE_FILLS = ("next_open", "next_close", "reference_same_bar")

# Shared type surface and the family allowlist of this leaf (Issue #93).
_Obj = _surface.Obj
_Target = _surface.Target
_Progress = _surface.Progress
_Axes = _surface.Axes
_Attempts = _surface.Attempts
_Prepared = _surface.Prepared
_LabelMap = _surface.LabelMap
_Winner = _surface.Winner
_CohortLike = _surface.CohortLike
_ServiceLike = _surface.ServiceLike
_SettingsLike = _surface.SettingsLike
_Run = _surface.Run
_families = _surface.families

# Preserve facade hook identities across module boundaries.
_call_with_optional_progress = cast(
    Callable[..., Any], _proof_batch.__dict__["_call_with_optional_progress"]
)
_cohort_result = cast(Callable[..., _Obj], _proof_batch.__dict__["_cohort_result"])
_prepare_cohort = cast(Callable[..., Any], _proof_batch.__dict__["_prepare_cohort"])
_tune = cast(Callable[..., Any], _proof_kernel.__dict__["_tune"])
resolve_seed = cast(Callable[[int | None], int], _proof_kernel.__dict__["resolve_seed"])
_targets = cast(
    Callable[[Mapping[str, Any]], list[_Target]], _cohort_prepare_helpers.__dict__["_targets"]
)
_translate = cast(
    Callable[[Mapping[str, Any], str], _Obj], _cohort_prepare_helpers.__dict__["_translate"]
)
_prepare_candidates = cast(
    Callable[..., tuple[list[_Prepared], list[_Obj], list[_Obj]]],
    _strategy_search_prepare.__dict__["prepare_candidates"],
)
_record_trials = cast(Callable[..., None], _strategy_search_prepare.__dict__["record_trials"])
_winner_is_better = cast(Callable[..., bool], _strategy_search_prepare.__dict__["winner_is_better"])
_no_prepared_result = cast(
    Callable[..., _Obj], _strategy_search_prepare.__dict__["no_prepared_result"]
)


# Public pre-P7.2 names and current private patch points share their originals.
prepare_candidates = _original_prepare = _prepare_candidates
record_trials = _original_record = _record_trials
winner_is_better = _original_winner = _winner_is_better
no_prepared_result = _original_empty = _no_prepared_result


def _family_hooks() -> tuple[Callable[..., frozenset[str]], str]:
    return get_proof_adapter_ids, CALENDAR_UNIVERSE


_surface.set_family_hook_provider(_family_hooks)


def _same_family(a: Any, b: Any, registry: StrategyRegistry) -> bool:
    families = [registry.get(x).family_id for x in (a, b) if isinstance(x, str) and x in registry]
    return a == b or (len(families) == 2 and families[0] == families[1])


def _candidate_request(
    request: Mapping[str, Any],
    strategy: str,
    targets: list[_Target],
    price_fill: str,
    registry: StrategyRegistry,
) -> _Obj:
    shared = request.get("parameters", {})
    params = (
        _translate(cast(Mapping[str, Any], shared), strategy)
        if isinstance(shared, Mapping) and _same_family(strategy, request.get("strategy"), registry)
        else {}
    )
    fills = registry.get(strategy).supported_fills
    fill = price_fill if price_fill in fills else (fills[0] if fills else price_fill)
    return {
        **{k: v for k, v in request.items() if k not in ("optimize_strategy", "price_fill")},
        "strategy": strategy,
        "parameters": params,
        "fill": fill,
        # Preflight each family on *all* members, even for a focused single window.
        "targets": targets,
    }


def _price_fill(request: Mapping[str, Any], settings: _SettingsLike) -> str:
    """An explicit price fill wins; otherwise the legacy fill or the settings default."""
    if request.get("price_fill"):
        return cast(str, request["price_fill"])
    requested = request.get("fill")
    return cast(str, requested) if requested in PRICE_FILLS else settings.price_fill


def _progress_callback(
    run: _Run, p_idx: int, label: str, total_prepared: int, total_budget: int
) -> Callable[[int, int], None] | None:
    callback = run.on_progress
    if callback is None:
        return None

    def report(done: int, total: int, pi: int = p_idx, sl: str = label) -> None:
        overall = pi * budget() + done
        callback(
            {
                "stage": "search",
                "done": overall,
                "total": total_budget,
                "percent": round(12.0 + 76.0 * (overall / max(total_budget, 1)), 1),
                "label": f"Optuna ({sl}, {pi + 1}/{total_prepared}): испытание {done}/{total}",
            }
        )

    return report


def _search_family(run: _Run, p_idx: int, prepared: list[_Prepared], total_budget: int) -> None:
    """Tune one prepared family, then keep it only if it beats the running winner."""
    proposed, group, item = prepared[p_idx]
    cohort = cast(_CohortLike, group)
    run.check_cancelled()
    label = run.labels.get(cohort.active, cohort.active)
    best, count, attempts = cast(
        tuple[_Obj | None, int, _Attempts],
        _call_with_optional_progress(
            _tune,
            cohort.land,
            cohort.seed,
            cohort.axes,
            min(len(case.split.training) for _, case in cohort.members),
            trials=budget(),
            random_seed=run.base_seed,
            # Own derived seed per family: two families never share a sequence.
            seed_label=cohort.active,
            on_progress=_progress_callback(run, p_idx, label, len(prepared), total_budget),
        ),
    )
    item["trial_count"] = count
    score = cohort.land.measure(best)["rank_score"] if best is not None else None
    item.update(
        status="measured" if best is not None else "no_admissible_candidate",
        training_best_pp=score,
    )
    _choose_hook(record_trials, _record_trials, _original_record)(
        run.records, group, proposed, attempts
    )
    # Holdout, trade count and plateau never break cross-family ties.
    if _choose_hook(winner_is_better, _winner_is_better, _original_winner)(
        best,
        score,
        (run.top, run.winner),
        (group, run.active, run.registry),
        _same_family,
    ):
        run.winner = (proposed, cohort, best, count, attempts)
        run.top = cast(float, score)


def _no_winner(run: _Run) -> _Obj:
    result = {
        **run.base,
        "status": "no_admissible_candidate",
        "trial_count": len(run.records),
        "candidates": run.records,
        "reason": "Нет допустимого результата ни у одной общей стратегии. Стратегия и параметры сохранены.",
    }
    return {**result, "run_id": _run_id(result)}


def _final_result(run: _Run, winner: _Winner, policy: _Obj, settings: _SettingsLike) -> _Obj:
    """Report only the training-selected family on full and validation data."""
    proposed, group, best, count, attempts = winner
    result = _cohort_result(
        proposed,
        settings,
        group,
        best,
        count,
        attempts,
        run.check_cancelled,
        on_progress=run.on_progress,
    )
    result.update(
        schema_version=run.base["schema_version"],
        method=run.base["method"],
        selection=run.base["selection"],
        selected_strategy=group.active,
        selected_fill=proposed["fill"],
        trial_count=len(run.records),
        candidates=run.records,
        strategy_search=policy,
    )
    result.pop("run_id", None)
    return {**result, "run_id": _run_id(result)}


def _base_payload(run: _Run, targets: list[_Target], policy: _Obj) -> _Obj:
    return {
        "schema_version": "proof-search.v4",
        "method": "Optuna TPESampler per compatible strategy",
        "library_version": optuna.__version__,
        "seed": run.settings.random_seed,
        "seed_used": run.base_seed,
        **run.settings.metadata(),
        "selection_basis": "training",
        "objective": "arithmetic_mean_of_per_window_strategy_CAGR_minus_LQDT_CAGR_pp",
        "selection": {k: v for k, v in run.request.items() if k != "targets"},
        "selected_strategy": None,
        "selected_parameters": None,
        "trial_count": 0,
        "candidates": [],
        "plateau": None,
        "strategy_search": policy,
    }


def strategy_search(
    service: _ServiceLike,
    request: dict[str, Any],
    settings: _SettingsLike,
    benchmark: Any,
    check_cancelled: Callable[[], None],
    *,
    on_progress: _Progress | None = None,
    registry: StrategyRegistry | None = None,
) -> _Obj:
    resolved = resolve_strategy_registry(registry)
    labels: _LabelMap = {p.strategy_id: p.short_label_ru for p in resolved}
    targets = _targets(request)
    spec = get_timeframe(settings.timeframe)
    price_fill = _price_fill(request, settings)
    active = cast(str, request.get("strategy", "zscore_mean_reversion"))
    calendar = getattr(service, "_calendar", None) or CalendarData(service.root)
    # Resolve the run seed before any cohort is prepared: an invalid setting must
    # fail before the expensive replays start.
    run = _Run(
        request=request,
        settings=settings,
        check_cancelled=check_cancelled,
        registry=resolved,
        labels=labels,
        active=active,
        # One base seed per run; each family derives its own child seed from it.
        base_seed=resolve_seed(settings.random_seed),
        on_progress=on_progress,
    )
    prepared, alternatives, failures = _choose_hook(
        prepare_candidates, _prepare_candidates, _original_prepare
    )(
        {
            "service": service,
            "request": request,
            "settings": settings,
            "benchmark": benchmark,
            "check_cancelled": check_cancelled,
            "calendar": calendar,
            "registry": resolved,
            "on_progress": on_progress,
            "labels": labels,
            "targets": targets,
            "price_fill": price_fill,
        },
        _families(request, targets, spec, resolved),
        _candidate_request,
        _prepare_cohort,
        _call_with_optional_progress,
    )
    policy: _Obj = {
        "policy": "common-strategy.v1",
        "execution_policy": "native_rules",
        "targets": targets,
        "target_count": len(targets),
        "trials_per_strategy": budget(),
        "price_fill": price_fill,
        "funding_fill": "same_day_settlement",
        "alternatives": alternatives,
    }
    run.base = _base_payload(run, targets, policy)
    if not prepared:
        return _choose_hook(no_prepared_result, _no_prepared_result, _original_empty)(
            run.base, failures, request, targets, _run_id
        )

    total_budget = len(prepared) * budget()
    try:
        for p_idx in range(len(prepared)):
            _search_family(run, p_idx, prepared, total_budget)
        if run.winner is None:
            return _no_winner(run)
        return _final_result(run, run.winner, policy, settings)
    finally:
        for _, group, _ in prepared:
            cast(_CohortLike, group).close()
