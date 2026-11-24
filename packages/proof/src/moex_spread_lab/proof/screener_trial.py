"""Evaluate screener candidates over a fixed training window."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from moex_spread_lab.core.evaluation_periods import EvaluationSplit
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs


@dataclass
class TrialContext:
    inputs: ScreenerInputs
    evaluator: Callable[..., dict[str, Any]]
    selected: list[str]
    strategy: str
    execution: dict[str, Any]
    axes: list[str]
    dates: list[str]
    train_dates: tuple[str, str]
    segment: Callable[[list[dict[str, Any]], str, str], float | None]
    require_finite: Callable[[float | None], float]
    initial_capital: float


def evaluate_trials(points: list[tuple[Any, ...]], ctx: TrialContext) -> list[dict[str, Any]]:
    trials: list[dict[str, Any]] = []
    for point in points:
        params = dict(zip(ctx.axes, point, strict=False))
        trial: dict[str, Any] = {"parameters": params, "status": "evaluated", "reason": None}
        pair_reports: dict[str, dict[str, Any]] = {}
        try:
            for pair in ctx.selected:
                settings = {
                    "pair": pair,
                    "strategy": ctx.strategy,
                    "parameters": params,
                    **ctx.execution,
                }
                pair_reports[pair] = ctx.evaluator(ctx.inputs, settings, through=ctx.train_dates[1])
            train = [
                ctx.segment(pair_reports[pair]["daily"], *ctx.train_dates) for pair in ctx.selected
            ]
            checked_train = [ctx.require_finite(value) for value in train]
            visible_rows = [
                row
                for report in pair_reports.values()
                for row in report["daily"]
                if ctx.dates[0] <= row["date"] <= ctx.train_dates[1]
            ]
            max_dd = max(row["D"] for row in visible_rows) / ctx.initial_capital * 100
            worst_shortfall = min(row["X"] for row in visible_rows) / ctx.initial_capital * 100
            trial.update(
                {
                    "train_min_excess_pp": round(min(checked_train), 3),
                    "train_by_pair_pp": {
                        pair: round(value, 3)
                        for pair, value in zip(ctx.selected, checked_train, strict=False)
                    },
                    "max_excess_drawdown_pct": round(max_dd, 2),
                    "worst_shortfall_pct": round(worst_shortfall, 2),
                }
            )
            # Benchmark-relative limits are reported, not enforced.
        except (W0Error, OSError, ValueError) as exc:
            trial.update({"status": "rejected", "reason": str(exc)})
        trials.append(trial)
    return trials


def validation_scores(
    ctx: TrialContext,
    chosen: tuple[float, tuple[Any, ...], list[tuple[Any, ...]]] | None,
    by_point: dict[tuple[Any, ...], dict[str, Any]],
    selected_params: dict[str, Any] | None,
    split: EvaluationSplit,
) -> dict[str, Any] | None:
    if not chosen:
        return None
    by_pair = dict(by_point[chosen[1]]["train_by_pair_pp"])
    if split.validation:
        by_pair = {}
        for pair in ctx.selected:
            report = ctx.evaluator(
                ctx.inputs,
                {
                    "pair": pair,
                    "strategy": ctx.strategy,
                    "parameters": selected_params,
                    **ctx.execution,
                },
                through=split.validation[-1],
            )
            # Include the first validation session's change by anchoring in training.
            by_pair[pair] = ctx.segment(report["daily"], ctx.train_dates[1], split.validation[-1])
    return by_pair
