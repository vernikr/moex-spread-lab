"""Regression contracts for the extracted seeded Optuna trial engine."""

import pytest

from moex_spread_lab.proof.landscape import Axis, Landscape
from moex_spread_lab.proof.proof_kernel import TuningOptions, _suggest_point, _tune, run_tuning
from moex_spread_lab.proof.proof_search import _tune as facade_tune


def test_seeded_trials_preserve_order_bounds_winner_and_progress():
    axes = {
        "window": Axis(3, 15, 1),
        "entry_z": Axis(0.5, 1.5, 0.1),
        "exit_z": Axis(0, 1.0, 0.1),
    }
    seed = {"window": 8, "entry_z": 1.0, "exit_z": 0.5, "quantity": 1}

    def objective(p):
        return {
            "utility": 100
            - abs(p["window"] - 9) * 3
            - abs(p["entry_z"] - 1.2) * 10
            - abs(p["exit_z"] - 0.4) * 10
        }

    progress = []
    best, count, attempts = _tune(
        Landscape(axes, objective),
        seed,
        axes,
        11,
        trials=6,
        random_seed=42,
        seed_label="regression",
        on_progress=lambda done, total: progress.append((done, total)),
    )
    expected = [
        (8, 1.0, 0.5),
        (3, 1.2, 0.1),
        (3, 1.3, 0.6),
        (4, 1.5, 0.6),
        (5, 0.5, 0.0),
        (9, 1.1, 0.2),
    ]
    assert [(p["window"], p["entry_z"], p["exit_z"]) for p in attempts] == expected
    assert best == attempts[-1]
    assert count == 6
    assert progress == [(i, 6) for i in range(1, 7)]
    assert all(
        p["window"] <= 9 and p["exit_z"] < p["entry_z"] and p["quantity"] == 1 for p in attempts
    )
    assert seed == {"window": 8, "entry_z": 1.0, "exit_z": 0.5, "quantity": 1}
    assert facade_tune is _tune


def test_pruned_trials_and_seed_tie_break_are_unchanged():
    axes = {"window": Axis(3, 10, 1)}
    seed = {"window": 5}
    best, count, attempts = _tune(
        Landscape(axes, lambda _: {"utility": None}),
        seed,
        axes,
        12,
        trials=4,
        random_seed=4,
    )
    assert best is None and count == len(attempts) == 4
    best, count, attempts = _tune(
        Landscape(axes, lambda _: {"utility": None, "rank_score": 1}),
        seed,
        axes,
        12,
        trials=4,
        random_seed=4,
    )
    assert best == seed and count == len(attempts) == 4


def test_suggest_point_clamps_to_the_step_grid_and_keeps_fixed_seed_keys():
    import optuna

    axes = {"entry_z": Axis(1.0, 2.0, 0.2), "exit_z": Axis(0.0, 1.8, 0.2)}
    seed = {"entry_z": 1.4, "exit_z": 0.4, "quantity": 3}
    bounds = {"entry_z": 2.0, "exit_z": 1.8}
    study = optuna.create_study(
        direction="maximize", sampler=optuna.samplers.TPESampler(seed=7, n_startup_trials=8)
    )
    study.enqueue_trial({"entry_z": 1.2, "exit_z": 1.0})
    point = _suggest_point(study.ask(), seed, axes, bounds)
    assert point == {"entry_z": 1.2, "exit_z": 1.0, "quantity": 3}
    assert seed == {"entry_z": 1.4, "exit_z": 0.4, "quantity": 3}


@pytest.mark.parametrize(
    ("seed", "check"),
    [
        ({"lookback": 25, "entry_z": 1.0}, lambda p: 5 <= p["lookback"] <= 10),
        ({"lookback": 6, "entry_z": 0.7}, lambda p: p["entry_z"] in (0.5, 1.0, 1.5, 2.0)),
    ],
)
def test_run_tuning_skips_a_seed_outside_the_train_bound_or_step_grid(seed, check):
    axes = {"lookback": Axis(5, 30, 1), "entry_z": Axis(0.5, 2.0, 0.5)}
    land = Landscape(axes, lambda p: {"utility": float(p["lookback"]) + p["entry_z"]})
    options = TuningOptions(trials=3, on_progress=None, random_seed=11, seed_label=None)
    best, count, attempts = run_tuning(land, seed, axes, 12, options)
    assert count == 3
    assert all(check(point) for point in attempts)
    assert best in attempts
