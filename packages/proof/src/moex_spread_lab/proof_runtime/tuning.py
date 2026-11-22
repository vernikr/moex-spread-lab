"""Deterministic Optuna trials for proof strategy parameters."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import optuna

from .proof_seed import derive_seed, resolve_seed


@dataclass(frozen=True)
class TuningOptions:
    trials: int
    on_progress: Callable[[int, int], None] | None
    random_seed: int | None
    seed_label: object | None


def _suggest_point(trial, seed, strategy_axes, bounds):
    point = dict(seed)
    for name, axis in strategy_axes.items():
        high = (
            min(bounds[name], round(point["entry_z"] - axis.step, 6))
            if name == "exit_z"
            else bounds[name]
        )
        point[name] = axis.suggest(trial, name, high)
    return point


def run_tuning(land, seed, strategy_axes, train_size, options):
    base = resolve_seed(options.random_seed)
    if options.seed_label is not None:
        base = derive_seed(base, options.seed_label)
    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=base, n_startup_trials=8),
    )
    bounds = {
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
    best, best_score = None, -math.inf
    attempts = []
    for idx in range(options.trials):
        trial = study.ask()
        point = _suggest_point(trial, seed, strategy_axes, bounds)
        measured = land.measure(point)
        score = measured.get("rank_score", measured["utility"])
        if score is None:
            study.tell(trial, state=optuna.trial.TrialState.PRUNED)
        else:
            study.tell(trial, score)
            if score > best_score or (score == best_score and point == seed):
                best, best_score = point, score
        attempts.append(point)
        if options.on_progress is not None:
            options.on_progress(idx + 1, options.trials)
    return best, len(study.trials), attempts
