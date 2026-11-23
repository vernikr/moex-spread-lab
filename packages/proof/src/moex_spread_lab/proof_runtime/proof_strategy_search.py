"""Choose one strategy and one common vector on a fixed cohort's training periods.

Each declared family gets a full TPE budget, so no categorical branch is starved by
early pruning. Execution is not optimized: price strategies keep their chosen fill,
funding carry keeps its mandatory same-day settlement (explicit user decision).
"""

from __future__ import annotations

from typing import cast

import optuna

from moex_spread_lab.core.timeframes import get_timeframe
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import _run_id
from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData

from .proof_batch import (
    _call_with_optional_progress,
    _cohort_result,
    _prepare_cohort,
    _targets,
    _translate,
)
from .proof_kernel import _tune, resolve_seed
from .strategy_catalog import get_proof_adapter_ids, resolve_strategy_registry
from .strategy_search_prepare import (
    no_prepared_result,
    prepare_candidates,
    record_trials,
    winner_is_better,
)
from .trial_budget import budget

PRICE_FILLS = ("next_open", "next_close", "reference_same_bar")


def _families(request, targets, timeframe, registry):
    """One compatible adapter per registered family, in registry order.

    The allowlist is intentional: a plugin alone may lack the accounting adapter
    needed to replay it safely; a mixed cohort prefers the active editor's universe.
    """
    adapter_ids = get_proof_adapter_ids(registry)
    active = request.get("strategy", "zscore_mean_reversion")
    active_plugin = registry.get(active) if active in registry else None
    kinds = {target["universe"] for target in targets}
    preferred = (
        CALENDAR_UNIVERSE
        if kinds == {CALENDAR_UNIVERSE}
        or (
            len(kinds) > 1
            and active_plugin is not None
            and CALENDAR_UNIVERSE in active_plugin.supported_universes
        )
        else next((u for u in sorted(kinds) if u != CALENDAR_UNIVERSE), CALENDAR_UNIVERSE)
    )
    grouped = {}
    for plugin in registry:
        if plugin.strategy_id not in adapter_ids:
            continue
        if plugin.requires_daily_bars and not timeframe.supports_funding_strategy:
            continue
        grouped.setdefault(plugin.family_id, []).append(plugin)
    result = []
    for plugins in grouped.values():
        covered = set().union(*(p.supported_universes for p in plugins))
        if not kinds <= covered:
            continue
        match = next((p for p in plugins if preferred in p.supported_universes), None)
        if match is not None:
            result.append(match.strategy_id)
    return result


def _same_family(a, b, registry):
    families = [registry.get(x).family_id for x in (a, b) if x in registry]
    return a == b or (len(families) == 2 and families[0] == families[1])


def _candidate_request(request, strategy, targets, price_fill, registry):
    params = (
        _translate(request.get("parameters", {}), strategy)
        if _same_family(strategy, request.get("strategy"), registry)
        else {}
    )
    plugin = registry.get(strategy)
    fill = price_fill if price_fill in plugin.supported_fills else plugin.supported_fills[0]
    return {
        **{k: v for k, v in request.items() if k not in ("optimize_strategy", "price_fill")},
        "strategy": strategy,
        "parameters": params,
        "fill": fill,
        # Preflight each family on *all* members, even for a focused single window.
        "targets": targets,
    }


def strategy_search(
    service, request, settings, benchmark, check_cancelled, *, on_progress=None, registry=None
):
    registry = resolve_strategy_registry(registry)
    labels = {p.strategy_id: p.short_label_ru for p in registry}
    targets = _targets(request)
    spec = get_timeframe(settings.timeframe)
    price_fill = request.get("price_fill") or (
        request.get("fill") if request.get("fill") in PRICE_FILLS else settings.price_fill
    )
    active = request.get("strategy", "zscore_mean_reversion")
    calendar = service._calendar or CalendarData(service.root)
    # One base seed per run; each family derives its own child seed from it.
    base_seed = resolve_seed(settings.random_seed)
    families = _families(request, targets, spec, registry)
    prepared, alternatives, failures = prepare_candidates(
        {
            "service": service,
            "request": request,
            "settings": settings,
            "benchmark": benchmark,
            "check_cancelled": check_cancelled,
            "calendar": calendar,
            "registry": registry,
            "on_progress": on_progress,
            "labels": labels,
            "targets": targets,
            "price_fill": price_fill,
        },
        families,
        _candidate_request,
        _prepare_cohort,
        _call_with_optional_progress,
    )

    policy = {
        "policy": "common-strategy.v1",
        "execution_policy": "native_rules",
        "targets": targets,
        "target_count": len(targets),
        "trials_per_strategy": budget(),
        "price_fill": price_fill,
        "funding_fill": "same_day_settlement",
        "alternatives": alternatives,
    }
    base = {
        "schema_version": "proof-search.v4",
        "method": "Optuna TPESampler per compatible strategy",
        "library_version": optuna.__version__,
        "seed": settings.random_seed,
        "seed_used": base_seed,
        **settings.metadata(),
        "selection_basis": "training",
        "objective": "arithmetic_mean_of_per_window_strategy_CAGR_minus_LQDT_CAGR_pp",
        "selection": {k: v for k, v in request.items() if k != "targets"},
        "selected_strategy": None,
        "selected_parameters": None,
        "trial_count": 0,
        "candidates": [],
        "plateau": None,
        "strategy_search": policy,
    }
    if not prepared:
        return no_prepared_result(base, failures, request, targets, _run_id)

    winner, top, records = None, -float("inf"), []
    total_prepared = len(prepared)
    total_budget = total_prepared * budget()
    try:
        for p_idx, (proposed, group, item) in enumerate(prepared):
            check_cancelled()
            s_label = labels.get(group.active, group.active)
            best, count, attempts = _call_with_optional_progress(
                _tune,
                group.land,
                group.seed,
                group.axes,
                min(len(case.split.training) for _, case in group.members),
                trials=budget(),
                random_seed=base_seed,
                # Own derived seed per family: two families never share a sequence.
                seed_label=group.active,
                on_progress=(
                    None
                    if on_progress is None
                    else lambda done, total, pi=p_idx, sl=s_label: on_progress(
                        {
                            "stage": "search",
                            "done": pi * budget() + done,
                            "total": total_budget,
                            "percent": round(
                                12.0 + 76.0 * ((pi * budget() + done) / max(total_budget, 1)),
                                1,
                            ),
                            "label": f"Optuna ({sl}, {pi + 1}/{total_prepared}): испытание {done}/{total}",
                        }
                    )
                ),
            )
            item["trial_count"] = count
            score = group.land.measure(best)["rank_score"] if best is not None else None
            item.update(
                status="measured" if best is not None else "no_admissible_candidate",
                training_best_pp=score,
            )
            record_trials(records, group, proposed, attempts)
            # Holdout, trade count and plateau never break cross-family ties.
            if winner_is_better(
                best, score, (top, winner), (group, active, registry), _same_family
            ):
                winner, top = (proposed, group, best, count, attempts), cast(float, score)

        if winner is None:
            result = {
                **base,
                "status": "no_admissible_candidate",
                "trial_count": len(records),
                "candidates": records,
                "reason": "Нет допустимого результата ни у одной общей стратегии. Стратегия и параметры сохранены.",
            }
            return {**result, "run_id": _run_id(result)}

        proposed, group, best, count, attempts = winner
        # Only the final training-selected family is reported on full/validation data.
        result = _cohort_result(
            proposed,
            settings,
            group,
            best,
            count,
            attempts,
            check_cancelled,
            on_progress=on_progress,
        )
        result.update(
            schema_version=base["schema_version"],
            method=base["method"],
            selection=base["selection"],
            selected_strategy=group.active,
            selected_fill=proposed["fill"],
            trial_count=len(records),
            candidates=records,
            strategy_search=policy,
        )
        result.pop("run_id", None)
        return {**result, "run_id": _run_id(result)}
    finally:
        for _, group, _ in prepared:
            group.close()
