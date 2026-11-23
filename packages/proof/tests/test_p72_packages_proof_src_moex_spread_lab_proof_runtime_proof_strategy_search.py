"""Isolated regressions for strict-typed proof_strategy_search (Issue #93)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from moex_spread_lab.core.timeframes import get_timeframe
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE, PAIR_UNIVERSE
from moex_spread_lab.core.w0 import run_id
from moex_spread_lab.proof_runtime import (
    cohort_prepare_helpers,
    proof_batch,
    proof_kernel,
    strategy_search_prepare,
)
from moex_spread_lab.proof_runtime import (
    proof_strategy_search as search,
)
from moex_spread_lab.proof_runtime.axis import Axis
from moex_spread_lab.proof_runtime.strategy_catalog import get_strategy_registry

TARGET = {"universe": PAIR_UNIVERSE, "pair": "AAA/BBB"}


def _settings(**overrides: Any) -> Any:
    base = {
        "timeframe": "1d",
        "random_seed": 7,
        "price_fill": "reference_same_bar",
        "metadata": lambda: {"timeframe": "1d", "validation_percent": 25.0},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _Land:
    """Every point of one family measures the same, as a fixed cohort would."""

    def __init__(self, score: float | None) -> None:
        self.score = score
        self.seen: list[Any] = []

    def measure(self, values: Any) -> dict[str, Any]:
        self.seen.append(values)
        return {"rank_score": self.score, "utility": self.score, "reason": "measured"}


class _Service:
    """Minimal service facade: the search only reads the root and the calendar."""

    def __init__(self, calendar: Any = None) -> None:
        self.root = Path("/tmp")
        self._calendar = calendar


class _Cohort:
    def __init__(self, active: str, land: _Land, score: float | None) -> None:
        self.active = active
        self.axes = {"lookback": Axis(5, 20, 1)}
        self.seed = {"lookback": 5}
        self.land = land
        self.members: list[tuple[dict[str, Any], Any]] = [
            (TARGET, SimpleNamespace(split=SimpleNamespace(training=list(range(20)))))
        ]
        self.score = score
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _prepared(name: str, score: float | None) -> tuple[dict[str, Any], _Cohort, dict[str, Any]]:
    cohort = _Cohort(name, _Land(score), score)
    return (
        {"strategy": name, "fill": "reference_same_bar"},
        cohort,
        {
            "strategy": name,
            "fill": "reference_same_bar",
            "trial_count": 0,
            "training_best_pp": None,
        },
    )


def _run(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "request": {"strategy": "mean_reversion"},
        "settings": _settings(),
        "check_cancelled": lambda: None,
        "registry": get_strategy_registry(),
        "labels": {},
        "active": "mean_reversion",
        "base_seed": 7,
        "on_progress": None,
        "base": {"schema_version": "proof-search.v4", "method": "m", "selection": {}},
    }
    values.update(overrides)
    return search._Run(**values)


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_strategy_search_bindings() -> (
    None
):
    assert search._run_id is run_id
    assert search._tune is proof_kernel._tune
    assert search.resolve_seed is proof_kernel.resolve_seed
    assert search._targets is cohort_prepare_helpers._targets
    assert search._translate is cohort_prepare_helpers._translate
    assert search._prepare_cohort is proof_batch._prepare_cohort
    assert search._cohort_result is proof_batch._cohort_result
    assert search._call_with_optional_progress is proof_batch._call_with_optional_progress
    assert (
        search._prepare_candidates is strategy_search_prepare.prepare_candidates
        and search._record_trials is strategy_search_prepare.record_trials
        and search._winner_is_better is strategy_search_prepare.winner_is_better
    )


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_strategy_search_price_fill() -> (
    None
):
    settings = _settings(price_fill="next_open")
    assert search._price_fill({"price_fill": "next_close"}, settings) == "next_close"
    assert search._price_fill({"fill": "next_close"}, settings) == "next_close"
    # Non-price fills (carry's settlement) never become a snapshot fill.
    assert search._price_fill({"fill": "same_day_settlement"}, settings) == "next_open"
    assert search._price_fill({}, settings) == "next_open"


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_strategy_search_candidate() -> (
    None
):
    registry = get_strategy_registry()
    request = {
        "strategy": "mean_reversion",
        "parameters": {"lookback": 10, "entry_z": 2.0},
        "optimize_strategy": True,
        "price_fill": "reference_same_bar",
        "universe": PAIR_UNIVERSE,
    }
    same = search._candidate_request(request, "funding_only", [TARGET], "next_close", registry)
    assert same["strategy"] == "funding_only"
    # Funding carry is replayable only with its mandatory same-day settlement.
    assert same["fill"] == "same_day_settlement"
    assert same["parameters"] == {}
    assert [t["pair"] for t in same["targets"]] == ["AAA/BBB"]
    assert "optimize_strategy" not in same and "price_fill" not in same

    translated = search._candidate_request(
        {"strategy": "mean_reversion", "parameters": {"lookback": 10, "entry_z": 2.0}},
        "mean_reversion",
        [TARGET],
        "next_close",
        registry,
    )
    assert translated["fill"] == "next_close"
    assert isinstance(translated["parameters"], dict)
    assert "lookback" in translated["parameters"]


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_strategy_search_progress() -> (
    None
):
    assert search._progress_callback(_run(), 0, "lbl", 2, 100) is None
    seen: list[dict[str, Any]] = []
    report = search._progress_callback(_run(on_progress=seen.append), 1, "lbl", 2, 60)
    assert report is not None
    report(10, 30)
    payload = seen[0]
    assert payload["stage"] == "search"
    assert payload["done"] == 1 * 80 + 10
    assert payload["total"] == 60
    assert payload["percent"] == round(12.0 + 76.0 * (90 / 60), 1)
    assert payload["label"].startswith("Optuna (lbl, 2/2)")


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_strategy_search_family() -> (
    None
):
    run = _run()
    weak, strong = _prepared("mean_reversion", 1.0), _prepared("funding_only", 2.5)
    prepared = [weak, strong]
    with patch.object(search, "_tune", return_value=({"lookback": 5}, 4, [{"lookback": 5}])):
        search._search_family(run, 0, prepared, 160)
        search._search_family(run, 1, prepared, 160)
    assert run.winner is not None
    assert run.winner[0]["strategy"] == "funding_only"
    assert run.top == 2.5
    assert [item["status"] for _, _, item in prepared] == ["measured", "measured"]
    assert [item["trial_count"] for _, _, item in prepared] == [4, 4]
    assert len(run.records) == 2
    assert run.records[0]["strategy"] == "mean_reversion"


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_strategy_search_result() -> (
    None
):
    proposed, cohort, item = _prepared("mean_reversion", 2.0)
    item["trial_count"] = 3
    run = _run(base={"schema_version": "v4", "method": "m", "selection": {"strategy": "x"}})
    run.records.append({"trial": 0})
    run.winner = (proposed, cohort, {"lookback": 5}, 3, [{"lookback": 5}])
    with patch.object(
        search, "_cohort_result", return_value={"group": {"member_count": 1}, "run_id": "stale"}
    ):
        result = search._final_result(run, run.winner, {"policy": "p"}, _settings())
    assert result["selected_strategy"] == "mean_reversion"
    assert result["selected_fill"] == "reference_same_bar"
    assert result["trial_count"] == 1
    assert result["strategy_search"] == {"policy": "p"}
    assert result["run_id"] == run_id({k: v for k, v in result.items() if k != "run_id"})


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_strategy_search_no_winner() -> (
    None
):
    run = _run()
    result = search._no_winner(run)
    assert result["status"] == "no_admissible_candidate"
    assert result["trial_count"] == 0
    assert result["run_id"] == run_id({k: v for k, v in result.items() if k != "run_id"})


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_strategy_search_families() -> (
    None
):
    registry = get_strategy_registry()
    spec = get_timeframe("1d")
    assert search._families(
        {"strategy": "mean_reversion"}, [{"universe": PAIR_UNIVERSE}], spec, registry
    ) == ["mean_reversion", "funding_only"]
    assert search._families(
        {"strategy": "zscore_mean_reversion"},
        [{"universe": CALENDAR_UNIVERSE}],
        spec,
        registry,
    ) == ["zscore_mean_reversion", "convergence_to_ruonia"]
    # A non-string declared strategy falls back to the documented default family.
    assert search._families({}, [{"universe": PAIR_UNIVERSE}], spec, registry) == [
        "mean_reversion",
        "funding_only",
    ]


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_strategy_search_closes() -> (
    None
):
    prepared = [_prepared("mean_reversion", 1.5)]
    proposals: list[dict[str, Any]] = []

    def prepare_candidates(
        context: dict[str, Any], families: Any, *_rest: Any
    ) -> tuple[list[Any], list[Any], list[Any]]:
        proposals.extend(context["targets"])
        return prepared, [], []

    with (
        patch.object(search, "_prepare_candidates", side_effect=prepare_candidates),
        patch.object(search, "_tune", return_value=({"lookback": 5}, 2, [{"lookback": 5}])),
        patch.object(search, "_cohort_result", return_value={"group": {}}),
    ):
        result = search.strategy_search(
            _Service(),
            {"targets": [TARGET], "strategy": "mean_reversion"},
            _settings(),
            ([], {}),
            lambda: None,
        )
    assert proposals == [TARGET]
    # Every prepared cohort is released even on the success path.
    assert prepared[0][1].closed is True
    assert result["selected_strategy"] == "mean_reversion"
    assert result["candidates"][0]["strategy"] == "mean_reversion"
    assert result["strategy_search"]["policy"] == "common-strategy.v1"


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_strategy_search_confirmation() -> (
    None
):
    def prepare_candidates(
        _context: dict[str, Any], _families: Any, *_rest: Any
    ) -> tuple[list[Any], list[Any], list[Any]]:
        return [], [], [{}]

    with (
        patch.object(search, "_prepare_candidates", side_effect=prepare_candidates),
        patch.object(
            search,
            "_no_prepared_result",
            return_value={"status": "selection_requires_confirmation"},
        ) as no_prepared,
    ):
        result = search.strategy_search(
            _Service("calendar"),
            {"targets": [TARGET], "strategy": "mean_reversion"},
            _settings(),
            ([], {}),
            lambda: None,
        )
    assert result == {"status": "selection_requires_confirmation"}
    assert no_prepared.call_args.args[1] == [{}]


@pytest.mark.parametrize("timeframe", ["1d", "1h"])
def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_strategy_search_timeframe(
    timeframe: str,
) -> None:
    spec = get_timeframe(timeframe)
    families = search._families(
        {"strategy": "mean_reversion"}, [{"universe": PAIR_UNIVERSE}], spec, get_strategy_registry()
    )
    assert families == (
        ["mean_reversion", "funding_only"] if timeframe == "1d" else ["mean_reversion"]
    )
