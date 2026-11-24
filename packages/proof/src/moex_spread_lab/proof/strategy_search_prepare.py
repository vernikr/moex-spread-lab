"""Prepare all strategy families and report universally inadmissible targets."""

from collections.abc import Callable
from typing import Any, cast

TargetKey = tuple[tuple[str, Any], ...]
Failures = list[dict[TargetKey, dict[str, Any]]]
Prepared = list[tuple[dict[str, Any], Any, dict[str, Any]]]


def prepare_candidates(
    context: dict[str, Any],
    families: list[str],
    candidate_request: Callable[..., dict[str, Any]],
    prepare_cohort: Callable[..., Any],
    call_with_progress: Callable[..., Any],
) -> tuple[Prepared, list[dict[str, Any]], Failures]:
    service = context["service"]
    request = context["request"]
    settings = context["settings"]
    benchmark = context["benchmark"]
    check_cancelled = context["check_cancelled"]
    calendar = context["calendar"]
    registry = context["registry"]
    on_progress = context["on_progress"]
    labels = context["labels"]
    targets = context["targets"]
    price_fill = context["price_fill"]
    total_families = max(len(families), 1)
    prepared: Prepared = []
    alternatives: list[dict[str, Any]] = []
    failures: Failures = []
    for f_idx, name in enumerate(families):
        check_cancelled()
        proposed = candidate_request(request, name, targets, price_fill, registry)
        s_label = cast(str, labels.get(name, name))
        if on_progress is not None:
            on_progress(
                {
                    "stage": "prepare",
                    "done": f_idx * len(targets),
                    "total": total_families * len(targets),
                    "percent": round(2.0 + 10.0 * (f_idx / total_families), 1),
                    "label": f"Подготовка окон ({s_label}, {f_idx + 1}/{total_families}): 0/{len(targets)}…",
                }
            )

        def progress(done: int, total: int, fi: int = f_idx, sl: str = s_label) -> None:
            callback = cast(Callable[[dict[str, Any]], None], on_progress)
            callback(
                {
                    "stage": "prepare",
                    "done": fi * total + done,
                    "total": total_families * total,
                    "percent": round(
                        2.0 + 10.0 * ((fi + done / max(total, 1)) / total_families),
                        1,
                    ),
                    "label": f"Подготовка окон ({sl}, {fi + 1}/{total_families}): {done}/{total}",
                }
            )

        group = call_with_progress(
            prepare_cohort,
            service,
            proposed,
            settings,
            benchmark,
            check_cancelled,
            calendar=calendar,
            registry=registry,
            on_progress=None if on_progress is None else progress,
        )
        item: dict[str, Any] = {
            "strategy": name,
            "fill": proposed["fill"],
            "trial_count": 0,
            "training_best_pp": None,
        }
        alternatives.append(item)
        if isinstance(group, dict):
            excluded = cast(list[dict[str, Any]], group["excluded"])
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

    return prepared, alternatives, failures


def no_prepared_result(
    base: dict[str, Any],
    failures: Failures,
    request: dict[str, Any],
    targets: list[dict[str, Any]],
    run_id: Callable[[dict[str, Any]], str],
) -> dict[str, Any]:
    # Only universally inadmissible windows may be proposed for removal. If
    # different families fail different members, there is no common strategy;
    # never pick a smaller/favorable denominator or trim a strategy's cohort.
    common: set[TargetKey] = (
        set(failures[0]).intersection(*(set(f) for f in failures[1:])) if failures else set()
    )
    if common and request.get("targets"):
        excluded: list[dict[str, Any]] = []
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


def record_trials(
    records: list[dict[str, Any]],
    group: Any,
    proposed: dict[str, Any],
    attempts: list[dict[str, Any]],
) -> None:
    """Append trial diagnostics in original order, measuring each once."""
    for i, point in enumerate(attempts):
        records.append(
            {
                "trial": len(records),
                "strategy_trial": i,
                "strategy": group.active,
                "fill": proposed["fill"],
                "parameters": {k: point[k] for k in cast(dict[str, Any], group.axes)},
                **group.land.measure(point),
            }
        )


def winner_is_better(
    best: Any,
    score: Any,
    current: tuple[float, Any],
    preferred: tuple[Any, Any, Any],
    same_family: Callable[..., bool],
) -> bool:
    """On equal scores retain the active family, then the first declared family."""
    top, winner = current
    group, active, registry = preferred
    return best is not None and (
        score > top
        or (
            score == top
            and same_family(group.active, active, registry)
            and not same_family(winner[1].active, active, registry)
        )
    )
