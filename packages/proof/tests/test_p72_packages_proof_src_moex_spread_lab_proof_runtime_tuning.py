"""Regression coverage for strict typing of ``proof_runtime.tuning`` (Issue #98)."""

from __future__ import annotations

import optuna
import pytest

from moex_spread_lab.proof import tuning
from moex_spread_lab.proof.axis import Axis
from moex_spread_lab.proof.landscape import Landscape
from moex_spread_lab.proof.tuning import TuningOptions, _suggest_point, run_tuning


def test_p72_tuning_public_and_private_exports_preserved() -> None:
    assert hasattr(tuning, "TuningOptions")
    assert hasattr(tuning, "_suggest_point")
    assert hasattr(tuning, "run_tuning")


def test_p72_suggest_point_clamps_exit_z_and_keeps_fixed_seed_keys() -> None:
    axes = {
        "entry_z": Axis(1.0, 2.0, 0.2),
        "exit_z": Axis(0.0, 1.8, 0.2),
    }
    seed = {"entry_z": 1.4, "exit_z": 0.4, "quantity": 3}
    bounds = {"entry_z": 2.0, "exit_z": 1.8}
    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=7, n_startup_trials=8),
    )
    study.enqueue_trial({"entry_z": 1.2, "exit_z": 1.0})
    point = _suggest_point(study.ask(), seed, axes, bounds)

    assert point["entry_z"] == 1.2
    assert point["exit_z"] == 1.0
    assert point["quantity"] == 3
    assert seed == {"entry_z": 1.4, "exit_z": 0.4, "quantity": 3}


def test_p72_run_tuning_skips_seed_enqueue_when_outside_train_bound_or_step_grid() -> None:
    axes = {
        "lookback": Axis(5, 30, 1),
        "entry_z": Axis(0.5, 2.0, 0.5),
    }
    # lookback=25 exceeds train_size - 2 (12 - 2 = 10), so seed must not be enqueued
    out_of_bound_seed = {"lookback": 25, "entry_z": 1.0}
    land = Landscape(axes, lambda p: {"utility": float(p["lookback"]) + p["entry_z"]})
    options = TuningOptions(trials=3, on_progress=None, random_seed=11, seed_label=None)

    best, count, attempts = run_tuning(land, out_of_bound_seed, axes, 12, options)
    assert count == 3
    assert len(attempts) == 3
    assert all(5 <= p["lookback"] <= 10 for p in attempts)
    assert best is not None
    assert best in attempts

    # off-step seed (0.7 is not on 0.5 + k * 0.5 grid) must also skip enqueue without error
    off_grid_seed = {"lookback": 6, "entry_z": 0.7}
    best_grid, count_grid, attempts_grid = run_tuning(land, off_grid_seed, axes, 12, options)
    assert count_grid == 3
    assert all(p["entry_z"] in (0.5, 1.0, 1.5, 2.0) for p in attempts_grid)
    assert best_grid is not None


@pytest.mark.parametrize(
    ("label_a", "label_b", "same_sequence"),
    [
        ("cohort", "cohort", True),
        ("cohort", "other_family", False),
    ],
)
def test_p72_run_tuning_seed_label_determinism(
    label_a: str, label_b: str, same_sequence: bool
) -> None:
    axes = {"window": Axis(3, 20, 1), "entry_z": Axis(0.5, 2.5, 0.1)}
    seed = {"window": 2, "entry_z": 1.0}  # window=2 outside Axis(3, 20) -> pure sampler draws
    land = Landscape(axes, lambda p: {"utility": p["entry_z"] - 0.01 * p["window"]})

    res_a = run_tuning(
        land,
        seed,
        axes,
        25,
        TuningOptions(trials=5, on_progress=None, random_seed=99, seed_label=label_a),
    )
    res_b = run_tuning(
        land,
        seed,
        axes,
        25,
        TuningOptions(trials=5, on_progress=None, random_seed=99, seed_label=label_b),
    )
    assert (res_a[2] == res_b[2]) is same_sequence
