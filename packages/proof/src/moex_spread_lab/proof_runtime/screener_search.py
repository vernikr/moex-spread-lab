"""Percentage-only screener projections and bounded parameter search."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date
from itertools import product
from typing import Any

from moex_spread_lab.core.evaluation_periods import EvaluationSplit, read_settings
from moex_spread_lab.core.universe import PAIRS, STRATEGIES
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.pair_screener.pair_screener import (
    ScreenerError,
    ScreenerInputs,
    _parameters,
    build_result,
    catalog,
    execution_settings,
)

from .screener_math import _annualized, _require_finite

INITIAL_CAPITAL = 30_000.0
PLATEAU_RATIO = 0.7
MIN_COMMON_OBSERVATIONS = 80
MIN_TABLE_OBSERVATIONS = 60


def _projection(result: dict[str, Any]) -> dict[str, Any]:
    rows = result["daily"]
    first, last = rows[0], rows[-1]
    days = (date.fromisoformat(last["date"]) - date.fromisoformat(first["date"])).days
    annualized = (
        _annualized(rows, first["date"], last["date"])
        if len(rows) >= MIN_TABLE_OBSERVATIONS
        else None
    )
    evaluation = result["evaluation"]
    dates = evaluation["periods"]
    basis = evaluation["result_basis"]
    anchor = dates["training"][-1] if basis == "validation" else first["date"]
    scored = (
        _annualized(rows, anchor, last["date"])
        if evaluation["observations"][basis] >= MIN_TABLE_OBSERVATIONS
        else None
    )
    metrics = result["metrics"]
    return {
        "status": "exploratory" if scored is not None else "short_history",
        "reason": None
        if scored is not None
        else "Годовой результат скрыт: недостаточно наблюдений, календарного охвата или состоятельной траектории капитала.",
        "period": result["period"],
        "metrics_scope": "full_history",
        "evaluation": {**evaluation, "result": {"annualized_excess_pp": scored}},
        "metrics": {
            "closed_trades": metrics["closed_trades"],
            "open_positions": metrics["open_positions"],
            "annualized_excess_pp": annualized,
            "max_excess_drawdown_pct": round(
                metrics["max_excess_drawdown_rub"] / INITIAL_CAPITAL * 100, 2
            ),
            "worst_shortfall_pct": round(
                metrics["worst_lqdt_shortfall_rub"] / INITIAL_CAPITAL * 100, 2
            ),
            "current_margin_pct": round(
                metrics["current_pair_margin_rub"] / INITIAL_CAPITAL * 100, 2
            ),
            "stress_headroom_pct": round(
                metrics["stress_remaining_capital_rub"] / INITIAL_CAPITAL * 100, 2
            ),
        },
        "chart": [
            {
                "date": row["date"],
                "strategy_index": round(row["V"] / first["V"] * 100, 3),
                "lqdt_index": round(row["B"] / first["B"] * 100, 3),
                "excess_pct": round(row["X"] / INITIAL_CAPITAL * 100, 3),
            }
            for row in rows
        ],
        "price_role": result["price_role"],
        "run_id": result["run_id"],
        "notes": result["notes"],
        "calendar_days": days,
    }


def screen_table(
    inputs: ScreenerInputs,
    selection: dict[str, Any],
    *,
    evaluator: Callable = build_result,
) -> dict[str, Any]:
    share = read_settings(inputs.root)["validation_percent"]
    if not isinstance(selection, dict):
        raise ScreenerError("screen selection must be an object")
    strategy = selection.get("strategy")
    if not isinstance(strategy, str) or strategy not in STRATEGIES:
        raise ScreenerError("unknown strategy")
    parameters = _parameters(selection, strategy)
    execution = execution_settings(selection)
    pairs = []
    for pair in catalog(inputs)["pairs"]:
        row = {key: pair[key] for key in ("id", "family", "expiry", "current_margin_pct")}
        row["min_leg_daily_volume"] = pair.get("min_leg_daily_volume")
        if pair["status"] == "excluded_thin_leg":
            row.update(
                {
                    "status": "excluded",
                    "reason": f"Торговать ежедневно нельзя: объём по одной из ног достигал всего {pair['min_leg_daily_volume']} контрактов в день.",
                }
            )
        elif not pair["strategies"][strategy]:
            row.update(
                {
                    "status": "unavailable",
                    "reason": "Несовместимая стратегия или отсутствует локальный вход.",
                }
            )
        else:
            try:
                result = evaluator(
                    inputs,
                    {
                        "pair": pair["id"],
                        "strategy": strategy,
                        "parameters": parameters,
                        **execution,
                    },
                    validation_share=share,
                )
                row.update(_projection(result))
            except (W0Error, OSError, ValueError) as exc:
                row.update({"status": "blocked", "reason": str(exc)})
        pairs.append(row)
    identity = {
        "validation_percent": share,
        "strategy": strategy,
        "parameters": parameters,
        "execution": execution,
        "pairs": [(row["id"], row.get("run_id"), row["status"]) for row in pairs],
    }
    run_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    return {
        "schema_version": "screener-table.v2",
        "run_id": run_id,
        "selection": {
            "strategy": strategy,
            "parameters": parameters,
            "execution": execution,
            "validation_percent": share,
        },
        "pairs": pairs,
        "warning": "Независимые проекции по парам, а не корзина с общим капиталом. Годовой излишек сравнивает геометрические доходности стратегии и LQDT на собственных датах каждой строки; периоды могут различаться. Исторические прокси — не исполнения.",
    }


def _grid(strategy: str, seed: dict[str, Any]) -> tuple[list[str], list[tuple[Any, ...]]]:
    lookbacks = sorted(
        {value for delta in (-5, 0, 5) if 5 <= (value := seed["lookback"] + delta) <= 80}
    )
    if strategy == "funding_only":
        lookbacks = sorted(
            {
                value
                for delta in (-10, -5, 0, 5, 10)
                if 5 <= (value := seed["lookback"] + delta) <= 80
            }
        )
        return ["lookback"], [(value,) for value in lookbacks]
    entries = sorted(
        {
            round(value, 3)
            for delta in (-0.5, 0, 0.5)
            if 0.5 <= (value := seed["entry_z"] + delta) <= 5
        }
    )
    exits = sorted(
        {
            round(value, 3)
            for delta in (-0.25, 0, 0.25)
            if 0 <= (value := seed["exit_z"] + delta) < min(entries)
        }
    )
    if len(lookbacks) < 3 or len(entries) < 3 or len(exits) < 3:
        raise ScreenerError(
            "choose seed parameters away from the boundaries to search a complete neighborhood"
        )
    return ["lookback", "entry_z", "exit_z"], list(product(lookbacks, entries, exits))


def _neighbors(point: tuple[Any, ...], points: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    axes = [sorted({item[index] for item in points}) for index in range(len(point))]
    result = []
    for other in points:
        changed = [index for index in range(len(point)) if point[index] != other[index]]
        if len(changed) != 1:
            continue
        index = changed[0]
        if abs(axes[index].index(point[index]) - axes[index].index(other[index])) == 1:
            result.append(other)
    return result


def _segment(rows: list[dict[str, Any]], first: str, last: str) -> float | None:
    return _annualized(rows, first, last)


def optimize(
    inputs: ScreenerInputs,
    request: dict[str, Any],
    *,
    evaluator: Callable = build_result,
    available: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ScreenerError("search request must be an object")
    percent = read_settings(inputs.root)["validation_percent"]
    selected = request.get("pairs")
    if (
        not isinstance(selected, list)
        or not 1 <= len(selected) <= len(PAIRS)
        or len(set(map(str, selected))) != len(selected)
    ):
        raise ScreenerError("select unique registered pairs")
    ids = {pair["pair_id"] for pair in PAIRS}
    if any(not isinstance(pair, str) or pair not in ids for pair in selected):
        raise ScreenerError("search includes an unknown pair")
    strategy = request.get("strategy")
    if not isinstance(strategy, str) or strategy not in STRATEGIES:
        raise ScreenerError("unknown strategy")
    execution = execution_settings(request)
    seed = _parameters(request, strategy)
    catalog_by_id = {pair["id"]: pair for pair in (available or catalog(inputs))["pairs"]}
    invalid = [pair for pair in selected if not catalog_by_id[pair]["strategies"][strategy]]
    if invalid:
        raise ScreenerError("selected pairs lack compatible local inputs: " + ", ".join(invalid))
    axes, points = _grid(strategy, seed)
    initial = {}
    for pair in selected:
        try:
            initial[pair] = evaluator(
                inputs,
                {"pair": pair, "strategy": strategy, "parameters": seed, **execution},
            )
        except (W0Error, OSError, ValueError) as exc:
            raise ScreenerError(f"{pair} cannot be searched: {exc}") from exc
    dates = sorted(
        set.intersection(*({row["date"] for row in result["daily"]} for result in initial.values()))
    )
    if len(dates) < MIN_COMMON_OBSERVATIONS:
        raise ScreenerError(
            f"selected pairs have only {len(dates)} common dates; at least {MIN_COMMON_OBSERVATIONS} required"
        )
    split = EvaluationSplit.from_dates(dates, percent)
    train_dates = (split.training[0], split.training[-1])
    if (date.fromisoformat(train_dates[1]) - date.fromisoformat(train_dates[0])).days < 30:
        raise ScreenerError("training needs at least 30 calendar days for an annualized score")
    trials = []
    for point in points:
        params = dict(zip(axes, point))
        trial = {"parameters": params, "status": "evaluated", "reason": None}
        pair_reports = {}
        try:
            for pair in selected:
                settings = {
                    "pair": pair,
                    "strategy": strategy,
                    "parameters": params,
                    **execution,
                }
                pair_reports[pair] = evaluator(inputs, settings, through=train_dates[1])
            train = [_segment(pair_reports[pair]["daily"], *train_dates) for pair in selected]
            checked_train = [_require_finite(value) for value in train]
            visible_rows = [
                row
                for report in pair_reports.values()
                for row in report["daily"]
                if dates[0] <= row["date"] <= train_dates[1]
            ]
            max_dd = max(row["D"] for row in visible_rows) / INITIAL_CAPITAL * 100
            worst_shortfall = min(row["X"] for row in visible_rows) / INITIAL_CAPITAL * 100
            trial.update(
                {
                    "train_min_excess_pp": round(min(checked_train), 3),
                    "train_by_pair_pp": {
                        pair: round(value, 3) for pair, value in zip(selected, checked_train)
                    },
                    "max_excess_drawdown_pct": round(max_dd, 2),
                    "worst_shortfall_pct": round(worst_shortfall, 2),
                }
            )
            # The benchmark-relative drawdown/shortfall limits are reported, not
            # enforced: a candidate is no longer vetoed for crossing them.
        except (W0Error, OSError, ValueError) as exc:
            trial.update({"status": "rejected", "reason": str(exc)})
        trials.append(trial)
    by_point = {tuple(trial["parameters"][axis] for axis in axes): trial for trial in trials}
    plateaus = []
    for point, trial in by_point.items():
        if trial["status"] != "evaluated" or trial["train_min_excess_pp"] <= 0:
            continue
        neighbors = _neighbors(point, points)
        stable = [
            other
            for other in neighbors
            if by_point[other]["status"] == "evaluated"
            and by_point[other]["train_min_excess_pp"]
            >= PLATEAU_RATIO * trial["train_min_excess_pp"]
            and by_point[other]["train_min_excess_pp"] > 0
        ]
        trial["stable_neighbor_count"] = len(stable)
        required = 2 if strategy == "funding_only" else 3
        if len(stable) >= required:
            trial["plateau"] = True
            plateaus.append(
                (
                    min(by_point[other]["train_min_excess_pp"] for other in (*stable, point)),
                    point,
                    stable,
                )
            )
    plateaus.sort(reverse=True)
    chosen = plateaus[0] if plateaus else None
    selected_params = dict(zip(axes, chosen[1])) if chosen else None
    by_pair = None
    if chosen:
        by_pair = dict(by_point[chosen[1]]["train_by_pair_pp"])
        if split.validation:
            by_pair = {}
            for pair in selected:
                report = evaluator(
                    inputs,
                    {
                        "pair": pair,
                        "strategy": strategy,
                        "parameters": selected_params,
                        **execution,
                    },
                    through=split.validation[-1],
                )
                # Anchor at the last training mark to include the first validation session's change.
                by_pair[pair] = _segment(report["daily"], train_dates[1], split.validation[-1])
    identity = {
        "pairs": selected,
        "strategy": strategy,
        "execution": execution,
        "seed": seed,
        "dates": dates,
        "trials": trials,
        "split": split.metadata(),
        "result_by_pair_pp": by_pair,
        "input_run_ids": {pair: initial[pair].get("run_id") for pair in selected},
    }
    run_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    return {
        "schema_version": "screener-search.v2",
        "run_id": run_id,
        "status": "exploratory_plateau_found" if chosen else "no_stable_positive_plateau",
        "scope": "independent_pair_studies_not_a_shared_capital_basket",
        "input_run_ids": identity["input_run_ids"],
        "selection": {
            "pairs": selected,
            "strategy": strategy,
            "execution": execution,
            "seed_parameters": seed,
        },
        "method": {
            "objective": "maximize the weakest selected pair's annualized excess over LQDT, in percentage points",
            "search_axes": axes,
            "candidate_count": len(points),
            "plateau_ratio": PLATEAU_RATIO,
            "minimum_stable_neighbors": 2 if strategy == "funding_only" else 3,
            "risk_basis": "working 10k excess drawdown / 15k shortfall limits on 30k initial research allocation",
            "selection_data": "training only; validation is reporting-only when enabled",
        },
        **split.metadata(),
        "selected_parameters": selected_params,
        "stable_neighbors": [dict(zip(axes, point)) for point in chosen[2]] if chosen else [],
        "result_by_pair_pp": by_pair,
        "result_note": "Годовая оценка требует не менее 30 календарных дней в расчётном периоде."
        if by_pair and any(v is None for v in by_pair.values())
        else None,
        "trials": trials,
    }
