"""Deterministic Optuna trials for proof strategy parameters."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

import optuna

from .axis import Axis
from .proof_seed import derive_seed, resolve_seed

type Numeric = int | float
type Point = dict[str, Numeric]


class _MeasuredLandscape(Protocol):
    def measure(self, values: Point) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class TuningOptions:
    trials: int
    on_progress: Callable[[int, int], None] | None
    random_seed: int | None
    seed_label: object | None


def _suggest_point(
    trial: optuna.trial.Trial,
    seed: Mapping[str, Numeric],
    strategy_axes: Mapping[str, Axis],
    bounds: Mapping[str, Numeric],
) -> Point:
    point: Point = dict(seed)
    for name, axis in strategy_axes.items():
        high = (
            min(bounds[name], round(point["entry_z"] - axis.step, 6))
            if name == "exit_z"
            else bounds[name]
        )
        point[name] = axis.suggest(trial, name, high)
    return point


def run_tuning(
    land: _MeasuredLandscape,
    seed: Mapping[str, Numeric],
    strategy_axes: Mapping[str, Axis],
    train_size: int,
    options: TuningOptions,
) -> tuple[Point | None, int, list[Point]]:
    base = resolve_seed(options.random_seed)
    if options.seed_label is not None:
        base = derive_seed(base, options.seed_label)
    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=base, n_startup_trials=8),
    )
    bounds: dict[str, Numeric] = {
        name: min(axis.high, train_size - 2) if name in ("lookback", "window") else axis.high
        for name, axis in strategy_axes.items()
    }
    if all(
        a.contains(seed[k])
        and seed[k] <= bounds[k]
        and math.isclose((seed[k] - a.low) / a.step, round((seed[k] - a.low) / a.step))
        for k, a in strategy_axes.items()
    ):
        study.enqueue_trial({k: seed[k] for k in strategy_axes})
    best: Point | None = None
    best_score = -math.inf
    attempts: list[Point] = []
    for idx in range(options.trials):
        trial = study.ask()
        point = _suggest_point(trial, seed, strategy_axes, bounds)
        measured = land.measure(point)
        raw_score = measured.get("rank_score", measured["utility"])
        if raw_score is None:
            study.tell(trial, state=optuna.trial.TrialState.PRUNED)
        else:
            assert isinstance(raw_score, (int, float))
            score = float(raw_score)
            study.tell(trial, score)
            if score > best_score or (score == best_score and point == seed):
                best, best_score = point, score
        attempts.append(point)
        if options.on_progress is not None:
            options.on_progress(idx + 1, options.trials)
    return best, len(study.trials), attempts
