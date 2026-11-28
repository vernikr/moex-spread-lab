"""Choose one strategy and one common vector on a fixed cohort's training periods.

Each declared family gets a full TPE budget; inadmissible targets are reported before any tuning starts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import optuna

from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.core.timeframes import TimeframeSpec, get_timeframe
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE as CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import run_id
from moex_spread_lab.strategies.base import StrategyPlugin, StrategyRegistry
from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData

from .cohort_models import Cohort, RemoteCohort
from .cohort_prepare import RemoteCase
from .cohort_prepare import targets as _targets
from .cohort_prepare import translate as _translate
from .pair_payload import budget
from .proof_batch import call_with_optional_progress as _call_with_optional_progress
from .proof_batch import cohort_result as _cohort_result
from .proof_batch import shared_prepare_cohort as _prepare_cohort
from .proof_kernel import StudyWindow, TuningOptions
from .proof_kernel import resolve_seed as resolve_seed
from .proof_kernel import tune as _tune
from .service import ProofService
from .strategy_catalog import get_proof_adapter_ids as get_proof_adapter_ids
from .strategy_catalog import resolve_strategy_registry

PRICE_FILLS = ("next_open", "next_close", "reference_same_bar")


Obj = dict[str, Any]
Target = dict[str, Any]
Progress = Callable[[dict[str, Any]], None]
Attempts = list[dict[str, Any]]
Prepared = tuple[Obj, Any, Obj]
LabelMap = dict[str, str]
TargetKey = tuple[tuple[str, Any], ...]
Failures = list[dict[TargetKey, dict[str, Any]]]
Winner = tuple[Obj, Cohort | RemoteCohort, "Obj | None", int, Attempts]


@dataclass
class Run:
    """Mutable per-request search state shared by the family loop."""

    request: Obj
    settings: ResearchSettings
    check_cancelled: Callable[[], None]
    registry: StrategyRegistry
    labels: LabelMap
    active: str
    base_seed: int
    on_progress: Progress | None
    base: Obj = field(default_factory=dict)
    top: float = -float("inf")
    winner: Winner | None = None
    records: list[Obj] = field(default_factory=list)


def _preferred_universe(active: Any, kinds: set[str], registry: StrategyRegistry) -> str:
    """A mixed cohort follows the active editor when it can span both universes."""
    plugin = registry.get(active) if isinstance(active, str) and active in registry else None
    selected = CALENDAR_UNIVERSE
    if kinds == {selected}:
        return selected
    spans = plugin is not None and selected in plugin.supported_universes
    if len(kinds) > 1 and spans:
        return selected
    return next((u for u in sorted(kinds) if u != selected), selected)


def families(
    request: Mapping[str, Any],
    targets: list[Target],
    timeframe: TimeframeSpec,
    registry: StrategyRegistry,
) -> list[str]:
    """One compatible adapter per family in registry order; the allowlist excludes plugins that lack their accounting adapter."""
    adapter_ids = get_proof_adapter_ids(registry)
    kinds = {str(target["universe"]) for target in targets}
    preferred = _preferred_universe(request.get("strategy"), kinds, registry)
    # Compatible adapters by family, in registry order, funding carry last on intraday.
    grouped: dict[str, list[StrategyPlugin]] = {}
    for plugin in registry.values():
        if plugin.strategy_id not in adapter_ids or (
            plugin.requires_daily_bars and not timeframe.supports_funding_strategy
        ):
            continue
        grouped.setdefault(plugin.family_id, []).append(plugin)
    result: list[str] = []
    for group in grouped.values():
        covered: set[str] = set[str]().union(*(p.supported_universes for p in group))
        if not kinds <= covered:
            continue
        match = next((p for p in group if preferred in p.supported_universes), None)
        if match is not None:
            result.append(match.strategy_id)
    return result


_families = families


def _same_family(a: Any, b: Any, registry: StrategyRegistry) -> bool:
    families = [registry.get(x).family_id for x in (a, b) if isinstance(x, str) and x in registry]
    return a == b or (len(families) == 2 and families[0] == families[1])


def _candidate_request(
    request: Mapping[str, Any],
    strategy: str,
    targets: list[Target],
    price_fill: str,
    registry: StrategyRegistry,
) -> Obj:
    shared = request.get("parameters", {})
    shared_ok = isinstance(shared, Mapping) and _same_family(
        strategy, request.get("strategy"), registry
    )
    params = _translate(cast(Mapping[str, Any], shared), strategy) if shared_ok else {}
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


def _price_fill(request: Mapping[str, Any], settings: ResearchSettings) -> str:
    """An explicit price fill wins; otherwise the legacy fill or the settings default."""
    if request.get("price_fill"):
        return cast(str, request["price_fill"])
    requested = request.get("fill")
    return cast(str, requested) if requested in PRICE_FILLS else settings.price_fill


def _progress_callback(
    run: Run, p_idx: int, label: str, total_prepared: int, total_budget: int
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


def no_prepared_result(base: Obj, failures: Failures, request: Obj, targets: list[Obj]) -> Obj:
    # Only universally inadmissible windows may be proposed for removal: if
    # different families fail different members, no common strategy exists.
    common: set[TargetKey] = (
        set(failures[0]).intersection(*(set(f) for f in failures[1:])) if failures else set()
    )
    if common and request.get("targets"):
        excluded: list[Obj] = []
        for key in sorted(common):
            messages = list(dict.fromkeys(cast(str, f[key]["reason"]) for f in failures))
            excluded.append(
                {
                    "target": failures[0][key]["target"],
                    "reason": "Ни одна общая стратегия не проходит: " + " ".join(messages),
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
    return {**result, "run_id": run_id(result)}


def _search_family(run: Run, p_idx: int, prepared: list[Prepared], total_budget: int) -> None:
    """Tune one prepared family, then keep it only if it beats the running winner."""
    proposed, group, item = prepared[p_idx]
    cohort = cast(Cohort | RemoteCohort, group)
    run.check_cancelled()
    label = run.labels.get(cohort.active, cohort.active)
    best, count, attempts = cast(
        tuple[Obj | None, int, Attempts],
        _tune(
            cohort.land,
            cohort.seed,
            cohort.axes,
            min(
                len(case.split.training)
                for _, case in cast(list[tuple[Target, StudyWindow | RemoteCase]], cohort.members)
            ),
            TuningOptions(
                budget(),
                _progress_callback(run, p_idx, label, len(prepared), total_budget),
                run.base_seed,
                # Own derived seed per family: two families never share a sequence.
                cohort.active,
            ),
        ),
    )
    item["trial_count"] = count
    score: Any = cohort.land.measure(best)["rank_score"] if best is not None else None
    item.update(
        status="measured" if best is not None else "no_admissible_candidate",
        training_best_pp=score,
    )
    # Measure each attempt exactly once, in original order.
    for i, point in enumerate(attempts):
        run.records.append(
            {
                "trial": len(run.records),
                "strategy_trial": i,
                "strategy": group.active,
                "fill": proposed["fill"],
                "parameters": {k: point[k] for k in cast(dict[str, Any], group.axes)},
                **group.land.measure(point),
            }
        )
    # Ties: holdout/trade count/plateau never decide; keep active then first family.
    prior = run.winner
    better = best is not None and (
        score > run.top
        or (
            score == run.top
            and _same_family(group.active, run.active, run.registry)
            and prior is not None
            and not _same_family(prior[1].active, run.active, run.registry)
        )
    )
    if better:
        run.winner = (proposed, cohort, best, count, attempts)
        run.top = cast(float, score)


def _no_winner(run: Run) -> Obj:
    result = {
        **run.base,
        "status": "no_admissible_candidate",
        "trial_count": len(run.records),
        "candidates": run.records,
        "reason": "Нет допустимого результата ни у одной общей стратегии. Стратегия и параметры сохранены.",
    }
    return {**result, "run_id": run_id(result)}


def _final_result(run: Run, winner: Winner, policy: Obj, settings: ResearchSettings) -> Obj:
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
        base_seed=run.base_seed,
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
    return {**result, "run_id": run_id(result)}


def _base_payload(run: Run, targets: list[Target], policy: Obj) -> Obj:
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
    service: ProofService,
    request: dict[str, Any],
    settings: ResearchSettings,
    benchmark: Any,
    check_cancelled: Callable[[], None],
    *,
    on_progress: Progress | None = None,
    registry: StrategyRegistry | None = None,
) -> Obj:
    resolved = resolve_strategy_registry(registry)
    labels: LabelMap = {p.strategy_id: p.short_label_ru for p in resolved}
    targets = _targets(request)
    price_fill = _price_fill(request, settings)
    calendar = getattr(service, "_calendar", None) or CalendarData(service.root)
    # Resolve the run seed first: an invalid setting must fail before replays start.
    run = Run(
        request=request,
        settings=settings,
        check_cancelled=check_cancelled,
        registry=resolved,
        labels=labels,
        active=cast(str, request.get("strategy", "zscore_mean_reversion")),
        # One base seed per run; each family derives its own child seed from it.
        base_seed=resolve_seed(settings.random_seed),
        on_progress=on_progress,
    )
    prepared: list[Prepared] = []
    alternatives: list[Obj] = []
    failures: Failures = []
    family_names = _families(request, targets, get_timeframe(settings.timeframe), resolved)
    total_families = max(len(family_names), 1)
    for f_idx, name in enumerate(family_names):
        check_cancelled()
        proposed = _candidate_request(request, name, targets, price_fill, resolved)
        s_label = labels.get(name, name)

        def progress(done: int, total: int, fi: int = f_idx, sl: str = s_label) -> None:
            callback = cast(Callable[[dict[str, Any]], None], on_progress)
            callback(
                {
                    "stage": "prepare",
                    "done": fi * total + done,
                    "total": total_families * total,
                    "percent": round(
                        2.0 + 10.0 * ((fi + done / max(total, 1)) / total_families), 1
                    ),
                    "label": f"Подготовка окон ({sl}, {fi + 1}/{total_families}): {done}/{total}",
                }
            )

        if on_progress is not None:
            progress(0, len(targets))

        group = _call_with_optional_progress(
            _prepare_cohort,
            service,
            proposed,
            settings,
            benchmark,
            check_cancelled,
            calendar=calendar,
            registry=resolved,
            on_progress=None if on_progress is None else progress,
        )
        item: Obj = {
            "strategy": name,
            "fill": proposed["fill"],
            "trial_count": 0,
            "training_best_pp": None,
        }
        alternatives.append(item)
        if isinstance(group, dict):
            excluded = cast(list[Obj], group["excluded"])
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
                    for target, case in cast(list[tuple[Any, Any]], group.members)
                ],
            )
            prepared.append((proposed, group, item))

    policy: Obj = {
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
        return no_prepared_result(run.base, failures, request, targets)

    total_budget = len(prepared) * budget()
    try:
        for p_idx in range(len(prepared)):
            _search_family(run, p_idx, prepared, total_budget)
        if run.winner is None:
            return _no_winner(run)
        return _final_result(run, run.winner, policy, settings)
    finally:
        for _, group, _ in prepared:
            group.close()
