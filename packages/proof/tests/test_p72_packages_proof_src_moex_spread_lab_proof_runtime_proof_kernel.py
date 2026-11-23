"""Isolated regressions for the strict-typed proof kernel facade."""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import pytest

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING
from moex_spread_lab.core.evaluation_periods import EvaluationSplit, ResearchSettings
from moex_spread_lab.proof_runtime import (
    axis,
    evaluation,
    landscape,
    measure_full,
    proof_kernel,
    proof_search,
    proof_seed,
    request_validation,
    service,
    strategy_catalog,
    tuning,
)
from moex_spread_lab.proof_runtime.axis import Axis
from moex_spread_lab.proof_runtime.landscape import Landscape
from moex_spread_lab.proof_runtime.proof_kernel import _measure, _tune


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_kernel_bindings() -> None:
    assert proof_kernel.Landscape is landscape.Landscape
    assert proof_kernel._lamps is landscape._lamps
    assert proof_kernel._ordered is axis._ordered
    assert (proof_kernel.GREEN, proof_kernel.AMBER) == (landscape.GREEN, landscape.AMBER)
    assert proof_kernel._annualized_returns is evaluation._annualized_returns
    assert proof_kernel._excess_curve is evaluation._excess_curve
    assert proof_kernel._missing_exposure is evaluation._missing_exposure
    assert proof_kernel.account_checks is evaluation.account_checks
    assert proof_kernel._identity is request_validation._identity
    assert proof_kernel._validate is request_validation._validate
    assert proof_kernel.compact_result is request_validation.compact_result
    assert proof_kernel.execution_axes is request_validation.execution_axes
    assert proof_kernel.EXECUTION_AXES is request_validation.EXECUTION_AXES
    assert proof_kernel._mark is service._mark
    assert proof_kernel.reference_eval is service.reference_eval
    assert proof_kernel.get_strategy_axes is strategy_catalog.get_strategy_axes
    assert proof_kernel.get_strategy_registry is strategy_catalog.get_strategy_registry
    assert proof_kernel.get_proof_adapter_ids is strategy_catalog.get_proof_adapter_ids
    assert proof_kernel.TuningOptions is tuning.TuningOptions
    assert proof_kernel.run_tuning is tuning.run_tuning
    assert proof_kernel.admission is measure_full.admission
    assert proof_kernel.measure_full is measure_full.measure_full
    assert proof_kernel.SEED == proof_seed.SEED == 41
    assert proof_kernel.resolve_seed is proof_seed.resolve_seed
    assert proof_kernel.derive_seed is proof_seed.derive_seed
    assert proof_kernel._tune is proof_search._tune
    assert proof_kernel.__getattr__("STRATEGY_AXES") is proof_kernel.get_strategy_axes()
    with pytest.raises(AttributeError):
        proof_kernel.__getattr__("MISSING")


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_kernel_scalar_train(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    days = ["2021-01-01", "2021-07-01", "2022-01-01"]
    split = EvaluationSplit.from_dates(days, 0)
    proof: dict[str, Any] = {
        "selection": {"strategy": "mean_reversion", "sizing": AUTO_SIZING},
        "instrument": {"family": "pair_same_asset"},
        "window": {"passes_capital_screen": True, "tradable_every_day": True},
        "_scalar_train_summary": (100.0, 100.0, 1.0, 121.0, 21.0, False, True),
    }
    marks = {"2021-01-01": 100.0, "2021-07-01": 105.0, "2022-01-01": 110.0}
    measured = _measure(proof, marks, split, ResearchSettings())
    assert measured["net_rub"] == 21.0
    assert measured["rank_score"] == pytest.approx(
        measured["strategy_annualized_pct"] - measured["lqdt_annualized_pct"]
    )
    assert measured["utility"] == measured["rank_score"]
    assert "reason" not in measured

    # Missing exposure is reported but never turns a measured result into a zero.
    exposed = dict(proof, _scalar_train_summary=(100.0, 100.0, 1.0, 121.0, 21.0, False, False))
    missing = _measure(exposed, marks, split, ResearchSettings())
    assert missing["utility"] is None
    assert missing["reason"] == "Открытого спреда не было на обучении."

    # A cash shortfall keeps the descriptive metrics and blocks only the utility.
    short = dict(proof, _scalar_train_summary=(100.0, 100.0, 1.0, 121.0, 21.0, True, True))
    shorted = _measure(short, marks, split, ResearchSettings())
    assert shorted["rank_score"] is None and shorted["utility"] is None
    assert shorted["reason"] == "Свободные средства недостаточны для маржи."

    # Non-finite and non-positive marks stay "no finite estimate", never a zero.
    for bad_marks in ({**marks, "2021-01-01": math.inf}, {**marks, "2021-01-01": 0.0}):
        finite = _measure(proof, bad_marks, split, ResearchSettings())
        assert finite["utility"] is None and finite["rank_score"] is None
        assert finite["reason"] == "Непригодная отметка LQDT в расчётном периоде."

    no_elapsed = EvaluationSplit.from_dates(["2022-01-01", "2022-01-01"], 0)
    guard = _measure(proof, marks | {"2022-01-01": 110.0}, no_elapsed, ResearchSettings())
    assert guard["rank_score"] is None
    assert guard["reason"] == (
        "Нет положительного капитала или календарного интервала для годовой доходности."
    )

    # Admission short-circuits before any metric is computed.
    blocked = SimpleNamespace(admission=lambda *_: "blocked")
    monkeypatch.setattr(proof_kernel, "_admission", blocked.admission)
    assert _measure({}, {}, split, ResearchSettings()) == {"utility": None, "reason": "blocked"}


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_kernel_active_measure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    split = EvaluationSplit.from_dates(["2026-01-01", "2026-01-02"], 0)
    seen: list[tuple[Any, ...]] = []

    def provider_measure(proof: Any, marks: Any, split_: Any, settings: Any) -> dict[str, Any]:
        seen.append((proof, marks, split_, settings))
        return {"utility": 7.0}

    monkeypatch.setattr(proof_kernel, "_measure_provider", lambda: provider_measure)
    assert proof_kernel._active_measure({"p": 1}, {}, split, ResearchSettings()) == {"utility": 7.0}
    assert len(seen) == 1

    monkeypatch.setattr(proof_kernel, "_measure_provider", None)
    proof_kernel._set_measure_provider(lambda: provider_measure)
    assert proof_kernel._active_measure({"p": 2}, {}, split, ResearchSettings()) == {"utility": 7.0}
    proof_kernel._set_measure_provider(lambda: proof_kernel._measure)
    fallback = {
        "selection": {"strategy": "mean_reversion", "sizing": AUTO_SIZING},
        "instrument": {"family": "pair_same_asset"},
        "window": {"passes_capital_screen": True, "tradable_every_day": True},
        "_scalar_train_summary": (100.0, 100.0, 1.0, 121.0, 21.0, False, False),
    }
    assert proof_kernel._active_measure(fallback, {}, split, ResearchSettings())["utility"] is None


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_kernel_tune_delegates() -> None:
    axes = {"window": Axis(3, 9, 1)}
    land = Landscape(axes, lambda _: {"utility": 1.0})
    progress: list[tuple[int, int]] = []
    best, count, attempts = _tune(
        land,
        {"window": 5},
        axes,
        11,
        trials=3,
        random_seed=4,
        seed_label="kernel",
        on_progress=lambda done, total: progress.append((done, total)),
    )
    assert count == len(attempts) == 3
    assert progress == [(1, 3), (2, 3), (3, 3)]
    assert best is not None and set(best) == {"window"}
