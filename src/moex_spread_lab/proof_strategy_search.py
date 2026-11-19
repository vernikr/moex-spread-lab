"""Choose one strategy and one common vector on a fixed cohort's training periods.

Enumerating the small declared strategy family gives each a full TPE budget and
prevents early pruning from starving a conditional categorical branch. Execution is
not an optimized axis: price strategies retain the chosen fill; funding carry keeps
its mandatory same-day-settlement convention (explicit user decision).
"""

from __future__ import annotations

import optuna

from .calendar_spreads import CalendarData
from .proof import CALENDAR_UNIVERSE, _run_id
from .proof_batch import (
    _cohort_result,
    _prepare_cohort,
    _targets,
    _translate,
)
from .proof_search import SEED, _tune
from .timeframes import get_timeframe

TRIALS_PER_STRATEGY = 80
PRICE_FILLS = ("next_open", "next_close", "reference_same_bar")
MR = ("mean_reversion", "zscore_mean_reversion")


def _families(request, targets, timeframe):
    active = request.get("strategy", "zscore_mean_reversion")
    native = active in ("zscore_mean_reversion", "convergence_to_ruonia")
    # The MR adapters are one strategy family, not two competing choices.
    kinds = {t["universe"] for t in targets}
    price = (
        "zscore_mean_reversion"
        if kinds == {CALENDAR_UNIVERSE}
        else "mean_reversion"
        if CALENDAR_UNIVERSE not in kinds
        else "zscore_mean_reversion"
        if native
        else "mean_reversion"
    )
    others = (
        ["convergence_to_ruonia"]
        if kinds == {CALENDAR_UNIVERSE}
        else ["funding_only"]
        if CALENDAR_UNIVERSE not in kinds
        else []
    )
    families = [price, *others]
    # A family the timeframe cannot execute is skipped upfront instead of being
    # prepared and excluded per trial. If no family remains, the existing
    # no-common-strategy semantics apply unchanged.
    supported = []
    for name in families:
        if name == "funding_only" and not timeframe.supports_funding_strategy:
            continue
        supported.append(name)
    return supported


def _same_family(a, b):
    return a == b or (a in MR and b in MR)


def _candidate_request(request, strategy, targets, price_fill):
    params = (
        _translate(request.get("parameters", {}), strategy)
        if _same_family(strategy, request.get("strategy"))
        else {}
    )
    return {
        **{
            k: v
            for k, v in request.items()
            if k not in ("optimize_strategy", "price_fill")
        },
        "strategy": strategy,
        "parameters": params,
        "fill": "same_day_settlement" if strategy == "funding_only" else price_fill,
        # Preflight each family on *all* members, even for a focused single window.
        "targets": targets,
    }


def strategy_search(service, request, settings, benchmark, check_cancelled):
    targets = _targets(request)
    spec = get_timeframe(settings.timeframe)
    price_fill = request.get("price_fill") or (
        request.get("fill") if request.get("fill") in PRICE_FILLS else "next_open"
    )
    active = request.get("strategy", "zscore_mean_reversion")
    calendar = service._calendar or CalendarData(service.root)
    prepared, alternatives, failures = [], [], []
    for name in _families(request, targets, spec):
        check_cancelled()
        proposed = _candidate_request(request, name, targets, price_fill)
        group = _prepare_cohort(
            service, proposed, settings, benchmark, check_cancelled, calendar=calendar
        )
        item = {
            "strategy": name,
            "fill": proposed["fill"],
            "trial_count": 0,
            "training_best_pp": None,
        }
        alternatives.append(item)
        if isinstance(group, dict):
            excluded = group["excluded"]
            item.update(status="incompatible", excluded=excluded)
            failures.append({tuple(sorted(x["target"].items())): x for x in excluded})
        else:
            item.update(
                status="ready",
                training_periods=[
                    {
                        "target": target,
                        "dates": [case.split.training[0], case.split.training[-1]],
                        "observations": len(case.split.training),
                    }
                    for target, case in group.members
                ],
            )
            prepared.append((proposed, group, item))

    policy = {
        "policy": "common-strategy.v1",
        "execution_policy": "native_rules",
        "targets": targets,
        "target_count": len(targets),
        "trials_per_strategy": TRIALS_PER_STRATEGY,
        "price_fill": price_fill,
        "funding_fill": "same_day_settlement",
        "alternatives": alternatives,
    }
    base = {
        "schema_version": "proof-search.v4",
        "method": "Optuna TPESampler per compatible strategy",
        "library_version": optuna.__version__,
        "seed": SEED,
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
        # Only universally inadmissible windows may be proposed for removal. If
        # different families fail different members, there is no common strategy;
        # never pick a smaller/favorable denominator or trim a strategy's cohort.
        common = set.intersection(*(set(f) for f in failures)) if failures else set()
        if common and request.get("targets"):
            excluded = []
            for key in sorted(common):
                messages = list(dict.fromkeys(f[key]["reason"] for f in failures))
                excluded.append(
                    {
                        "target": failures[0][key]["target"],
                        "reason": "Ни одна общая стратегия не проходит: "
                        + " ".join(messages),
                    }
                )
            return {
                **base,
                "status": "selection_requires_confirmation",
                "excluded": excluded,
                "remaining_count": len(targets) - len(excluded),
            }
        result = {
            **base,
            "status": "no_common_strategy",
            "reason": "Нет стратегии, допустимой сразу для всех выбранных окон. Отметки и параметры сохранены.",
        }
        return {**result, "run_id": _run_id(result)}

    winner, top, records = None, -float("inf"), []
    for proposed, group, item in prepared:
        check_cancelled()
        best, count, attempts = _tune(
            group.land,
            group.seed,
            group.axes,
            min(len(case.split.training) for _, case in group.members),
            trials=TRIALS_PER_STRATEGY,
        )
        item["trial_count"] = count
        score = group.land.measure(best)["rank_score"] if best is not None else None
        item.update(
            status="measured" if best is not None else "no_admissible_candidate",
            training_best_pp=score,
        )
        for i, point in enumerate(attempts):
            records.append(
                {
                    "trial": len(records),
                    "strategy_trial": i,
                    "strategy": group.active,
                    "fill": proposed["fill"],
                    "parameters": {k: point[k] for k in group.axes},
                    **group.land.measure(point),
                }
            )
        # On a cross-family exact tie retain the current family, then the first
        # declared family. Holdout, trade count and plateau never break ties.
        if best is not None and (
            score > top
            or (
                score == top
                and _same_family(group.active, active)
                and not _same_family(winner[1].active, active)
            )
        ):
            winner, top = (proposed, group, best, count, attempts), score

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
        proposed, settings, group, best, count, attempts, check_cancelled
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
