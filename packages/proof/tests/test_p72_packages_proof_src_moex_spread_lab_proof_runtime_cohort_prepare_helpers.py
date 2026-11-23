"""Isolated regressions for strict-typed cohort_prepare_helpers (P7.2, #80)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE, PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof_runtime import cohort_prepare, proof_batch, request_validation
from moex_spread_lab.proof_runtime import cohort_prepare_helpers as helpers
from moex_spread_lab.proof_runtime.axis import Axis
from moex_spread_lab.proof_runtime.cohort_prepare_helpers import (
    _active_seed,
    _confirmation,
    _filter_training,
    _intersect_axes,
    _native_request,
    _targets,
    _translate,
)

FACADE = (
    "MAX_TARGETS",
    "_active_seed",
    "_confirmation",
    "_filter_training",
    "_intersect_axes",
    "_native_request",
    "_targets",
    "_translate",
)
PAIR: dict[str, Any] = {"universe": PAIR_UNIVERSE, "pair": "A/B"}
CAL: dict[str, Any] = {"universe": CALENDAR_UNIVERSE, "asset": "SR", "pair": "p1"}
TOO_MANY = "до 1024 окон"


def _case(training: int) -> Any:
    return SimpleNamespace(split=SimpleNamespace(training=list(range(training))))


def _settings(**metadata: Any) -> Any:
    return SimpleNamespace(metadata=lambda: metadata)


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_cohort_prepare_helpers_facade():
    assert helpers._identity is request_validation._identity
    assert helpers.execution_axes is request_validation.execution_axes
    assert helpers.MAX_TARGETS == 1024
    # The facade hooks stay patchable module attributes, but ``__all__`` is frozen to the
    # pre-P7.2 wildcard surface, so it must not be used to enumerate them.
    assert all(hasattr(helpers, name) for name in FACADE)
    for name in FACADE:
        assert getattr(proof_batch, name) is getattr(helpers, name)
    for name in FACADE[1:]:
        assert getattr(cohort_prepare, name) is getattr(helpers, name)


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_cohort_prepare_helpers_translate():
    params = {"window": 12, "entry_z": 1.5}
    # The rename moves the key to the end and never mutates the caller's mapping.
    assert list(_translate(params, "mean_reversion").items()) == [
        ("entry_z", 1.5),
        ("lookback", 12),
    ]
    assert params == {"window": 12, "entry_z": 1.5}
    assert _translate({"lookback": 9}, "zscore_mean_reversion") == {"window": 9}
    for strategy in ("funding_only", "convergence_to_ruonia", "unknown"):
        assert _translate({"window": 1, "lookback": 2}, strategy) == {"window": 1, "lookback": 2}


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_cohort_prepare_helpers_targets():
    assert _targets({"asset": "SR", "pair": "p"}) == [
        {"universe": CALENDAR_UNIVERSE, "pair": "p", "asset": "SR"}
    ]
    assert _targets({"targets": [], **PAIR}) == [PAIR]
    cal = [
        {"universe": CALENDAR_UNIVERSE, "asset": asset, "pair": pair}
        for asset, pair in (("SR", "p2"), ("BR", "p9"), ("SR", "p1"))
    ]
    got = _targets({"targets": cal})
    assert [(t["asset"], t["pair"]) for t in got] == [("BR", "p9"), ("SR", "p1"), ("SR", "p2")]
    at_cap = [{"universe": PAIR_UNIVERSE, "pair": f"p{i:04d}"} for i in range(1024)]
    assert _targets({"targets": at_cap}) == at_cap


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"targets": "A/B"}, TOO_MANY),
        ({"targets": dict(PAIR)}, TOO_MANY),
        ({"targets": [PAIR] * 1025}, TOO_MANY),
        ({"targets": [PAIR, "A/B"]}, "список идентификаторов"),
        ({"targets": [{**PAIR, "strategy": "x"}]}, "список идентификаторов"),
        ({"targets": [{"universe": "other", "pair": "A/B"}]}, "Некорректный"),
        ({"targets": [{"universe": CALENDAR_UNIVERSE, "pair": "p"}]}, "Некорректный"),
        ({"targets": [PAIR, dict(PAIR)]}, "дважды"),
    ],
)
def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_cohort_prepare_helpers_errors(
    payload: dict[str, Any], message: str
):
    with pytest.raises(W0Error, match=message):
        _targets(payload)


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_cohort_prepare_helpers_native():
    active = {"strategy": "mean_reversion", "parameters": {"window": 12, "entry_z": 1.5}}
    payload = _native_request(active, PAIR)
    assert payload == {
        **PAIR,
        "strategy": "mean_reversion",
        "fill": "reference_same_bar",
        "parameters": {"entry_z": 1.5, "lookback": 12},
    }
    assert list(payload) == ["universe", "pair", "strategy", "fill", "parameters"]
    # Funding-only requests never carry a price fill, even when one is supplied.
    funding = {"strategy": "funding_only", "parameters": {"lookback": 20}, "fill": "next_open"}
    assert _native_request(funding, PAIR) == {
        **PAIR,
        "strategy": "funding_only",
        "parameters": {"lookback": 20},
    }
    # The requested strategy resolves to the family adapter that supports the universe.
    assert _native_request({}, CAL)["strategy"] == "zscore_mean_reversion"
    assert _native_request({"strategy": "mean_reversion"}, CAL)["strategy"] == (
        "zscore_mean_reversion"
    )
    assert _native_request({"strategy": "zscore_mean_reversion"}, PAIR)["strategy"] == (
        "mean_reversion"
    )


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_cohort_prepare_helpers_axes_fill():
    manual = {"strategy": "zscore_mean_reversion", "parameters": {"lookback": 9}}
    manual |= {"fill": "next_close", "quantity": 3, "slippage_ticks": 2}
    assert _native_request(manual, CAL, default_price_fill="next_open") == {
        **CAL,
        "strategy": "zscore_mean_reversion",
        "fill": "next_close",
        "quantity": 3,
        "slippage_ticks": 2,
        "parameters": {"window": 9},
    }
    assert _native_request({}, CAL, default_price_fill="next_open")["fill"] == "next_open"
    # Automatic sizing owns quantity and slippage: they are not copied into the request.
    auto = {"sizing": AUTO_SIZING, "quantity": 3, "slippage_ticks": 2}
    assert _native_request(auto, CAL) == {
        **CAL,
        "strategy": "zscore_mean_reversion",
        "fill": "reference_same_bar",
        "sizing": AUTO_SIZING,
        "parameters": {},
    }


@pytest.mark.parametrize(
    ("strategy", "target", "error", "message"),
    [
        ("nope", PAIR, W0Error, "Неизвестная стратегия подбора"),
        ("funding_only", CAL, W0Error, "неприменима"),
        ("convergence_to_ruonia", PAIR, W0Error, "неприменима"),
        ("convergence_to_ruonia", {**CAL, "asset": "BR"}, W0Error, "RUONIA"),
        ("convergence_to_ruonia", {"universe": CALENDAR_UNIVERSE, "pair": "p"}, KeyError, "asset"),
    ],
)
def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_cohort_prepare_helpers_rejects(
    strategy: str, target: dict[str, Any], error: type[Exception], message: str
):
    with pytest.raises(error, match=message):
        _native_request({"strategy": strategy}, target)


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_cohort_prepare_helpers_seed():
    # Monthly assets get their own tunable defaults; unknown or absent assets are not monthly.
    windows = {
        a: _active_seed({"asset": a}, "zscore_mean_reversion")["window"] for a in ("BR", "SR")
    }
    assert windows == {"BR": 7, "SR": 20}
    assert _active_seed({"asset": None}, "zscore_mean_reversion")["window"] == 20
    assert _active_seed({"asset": "ZZ"}, "zscore_mean_reversion")["window"] == 20
    assert _active_seed({}, "funding_only") == {
        "lookback": 20,
        "quantity": 1,
        "slippage_ticks": 1.0,
    }
    request = {"universe": PAIR_UNIVERSE, "parameters": {"window": 33}, "quantity": 4}
    assert _active_seed({**request, "slippage_ticks": 0.5}, "mean_reversion") == {
        "lookback": 20,
        "entry_z": 2.0,
        "exit_z": 0.5,
        "window": 33,
        "quantity": 4,
        "slippage_ticks": 0.5,
    }
    auto = _active_seed({**PAIR, "sizing": AUTO_SIZING, "quantity": 4}, "mean_reversion")
    assert auto == {"lookback": 20, "entry_z": 2.0, "exit_z": 0.5}
    with pytest.raises(W0Error, match="nope"):
        _active_seed({}, "nope")


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_cohort_prepare_helpers_intersect():
    axes = {"lookback": Axis(5, 50, 1), "entry_z": Axis(0.5, 5.0, 0.1)}
    # In place; the existing step is kept and unrelated axes are untouched.
    assert _intersect_axes(axes, {"window": Axis(7, 40, 5)}, "mean_reversion") is None
    assert axes == {"lookback": Axis(7, 40, 1), "entry_z": Axis(0.5, 5.0, 0.1)}
    zscore = {"window": Axis(3, 60, 1)}
    _intersect_axes(zscore, {"lookback": Axis(10, 30, 1)}, "zscore_mean_reversion")
    assert zscore == {"window": Axis(10, 30, 1)}
    # An empty intersection is represented as-is, never repaired or widened here.
    empty = {"window": Axis(30, 40, 1)}
    _intersect_axes(empty, {"window": Axis(1, 10, 1)}, "funding_only")
    assert empty == {"window": Axis(30, 10, 1)}
    with pytest.raises(KeyError, match="entry_z"):
        _intersect_axes({}, {"entry_z": Axis(1, 2, 1)}, "funding_only")


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_cohort_prepare_helpers_training():
    members = [({"size": size}, _case(size)) for size in (1, 2, 3)]
    rejected: list[dict[str, Any]] = [{"target": {}, "reason": "earlier"}]
    kept = _filter_training(members, {"entry_z": Axis(0, 9, 1)}, rejected)
    assert kept == members[1:]
    assert [r["target"] for r in rejected] == [{}, {"size": 1}]
    assert rejected[1]["reason"].endswith("не менее 2 наблюдений на обучении.")
    # The strictest window/lookback domain decides; fractional lows are reported unrounded.
    both = {"window": Axis(5, 50, 1), "lookback": Axis(8, 50, 1)}
    rejected = []
    kept = _filter_training(iter([({"n": n}, _case(n)) for n in (9, 10, 11)]), both, rejected)
    assert [t["n"] for t, _ in kept] == [10, 11]
    assert "не менее 10 наблюдений" in rejected[0]["reason"]
    rejected = []
    pair = [({"n": n}, _case(n)) for n in (7, 8)]
    kept = _filter_training(pair, {"lookback": Axis(5.5, 9, 1)}, rejected)
    assert [t["n"] for t, _ in kept] == [8]
    assert "не менее 7.5 наблюдений" in rejected[0]["reason"]


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_cohort_prepare_helpers_confirm():
    rejected = [{"target": {"pair": "A/B"}, "reason": "r"}]
    payload = _confirmation(_settings(validation_percent=25, timeframe="1d"), rejected, [1, 2])
    assert list(payload.items()) == [
        ("status", "selection_requires_confirmation"),
        ("validation_percent", 25),
        ("timeframe", "1d"),
        ("excluded", rejected),
        ("remaining_count", 2),
        ("trial_count", 0),
        ("selected_parameters", None),
    ]
    assert payload["excluded"] is rejected
    # Settings metadata can never override the denominator-bearing fields.
    forced = _confirmation(
        _settings(excluded="x", remaining_count=-1, trial_count=9), rejected, [1]
    )
    assert (forced["excluded"], forced["remaining_count"], forced["trial_count"]) == (
        rejected,
        1,
        0,
    )
