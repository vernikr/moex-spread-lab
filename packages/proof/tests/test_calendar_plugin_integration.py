"""Registered calendar plugins retain native proof, conformance and search contracts."""

import json

from packages.strategies.tests.conformance_fixtures import calendar_fixture

from moex_spread_lab.core.settings import save_settings
from moex_spread_lab.proof.proof_search import research_run
from moex_spread_lab.proof.strategy_catalog import get_strategy_axes
from moex_spread_lab.strategies import discover_strategies
from moex_spread_lab.strategies.conformance import assert_causal_conformance
from perf_equivalence_fixtures import installed_plugin
from proof_fixtures import two_calendar_windows
from test_perf_equivalence import _build_h1_workspace, _write_h1_candles

PLUGIN_SOURCE = """
from moex_spread_lab.strategies.calendar_zscore.plugin import CalendarZscorePlugin

class Demo(CalendarZscorePlugin):
    strategy_id = "demo_calendar"
    family_id = "demo_calendar"
    proof_adapter = "calendar_price_intents.v1"

PLUGIN = Demo()
"""


def _calendar_hourly_parity(root, request, service_factory):
    h1_root = root.parent / "h1_root"
    _build_h1_workspace(h1_root)
    save_settings(
        h1_root,
        {
            "random_seed": 41,
            "timeframe": "1h",
            "validation_percent": 20,
            "allow_failed_volume": True,
            "allow_failed_capital": True,
            "initial_capital_rub": 100000,
        },
    )
    for mode in ("synthetic_leg_proxy", "native_h1_spread"):
        if mode == "native_h1_spread":
            near = json.loads((h1_root / "data/w1/candles/SRH3_1h.json").read_text())["rows"]
            far = {
                r["begin"]: r
                for r in json.loads((h1_root / "data/w1/candles/SRM3_1h.json").read_text())["rows"]
            }
            spread = [
                {
                    "begin": n["begin"],
                    "open": far[n["begin"]]["open"] - n["open"],
                    "close": far[n["begin"]]["close"] - n["close"],
                    "volume": 40,
                }
                for n in near
                if n["begin"] in far
            ]
            _write_h1_candles(h1_root, "candles", "SRH3SRM3", spread)
        h1_service = service_factory(h1_root)
        for fill in ("next_open", "next_close", "reference_same_bar"):
            h1_request = {
                **request,
                "fill": fill,
                "parameters": {"window": 10, "entry_z": 1.2, "exit_z": 0.3},
            }
            actual = research_run(h1_service, h1_request, optimize=False, include_plateau=False)
            reference = research_run(
                h1_service,
                {**h1_request, "strategy": "zscore_mean_reversion"},
                optimize=False,
                include_plateau=False,
            )
            assert actual["window"]["price_source"] == mode
            assert actual["trades"] == reference["trades"], (mode, fill)
            assert actual["ledger"] == reference["ledger"], (mode, fill)
            assert actual["summary"] == reference["summary"], (mode, fill)


def _exercise_plugin(root, monkeypatch, service_factory):
    two_calendar_windows(root)
    save_settings(root, {"random_seed": 41})
    assert "demo_calendar" in get_strategy_axes()
    fixture, chart = calendar_fixture()
    assert_causal_conformance(
        discover_strategies().get("demo_calendar"),
        fixture,
        chart,
        {"window": 6, "entry_z": 1.5, "exit_z": 0.5},
        cutoffs=(14, 20, 26),
    )
    request = {
        "universe": "calendar_native",
        "asset": "SR",
        "pair": "SRH3/SRM3",
        "strategy": "demo_calendar",
        "parameters": {"window": 10, "entry_z": 1.5, "exit_z": 0.5},
        "sizing": "max_affordable",
        "fill": "next_close",
    }
    service = service_factory(root)
    proof = research_run(service, request, optimize=False, include_plateau=False)
    assert proof["selection"]["strategy"] == "demo_calendar"
    assert set(proof["selection"]["parameters"]) == {"window", "entry_z", "exit_z"}
    assert proof["summary"]["ledger_reconciliation_rub"] == 0
    reference = research_run(
        service,
        {**request, "strategy": "zscore_mean_reversion"},
        optimize=False,
        include_plateau=False,
    )
    assert proof["trades"] == reference["trades"]
    assert proof["ledger"] == reference["ledger"]
    assert proof["summary"] == reference["summary"]
    result = research_run(service, {**request, "optimize_strategy": True}, optimize=True)
    alternatives = result["strategy_search"]["alternatives"]
    assert [a["strategy"] for a in alternatives] == [
        "zscore_mean_reversion",
        "convergence_to_ruonia",
        "demo_calendar",
    ]
    assert next(a for a in alternatives if a["strategy"] == "demo_calendar")["trial_count"] == 80
    _calendar_hourly_parity(root, request, service_factory)


def test_drop_in_calendar_plugin_proof_and_search(tmp_path, monkeypatch):
    with installed_plugin(
        tmp_path, monkeypatch, "demo_calendar", PLUGIN_SOURCE, {"demo_calendar": "PLUGIN"}
    ) as service_factory:
        _exercise_plugin(tmp_path / "root", monkeypatch, service_factory)


def test_plugin_discovery_is_restored_after_failure_and_reinstallation(tmp_path, monkeypatch):
    import os
    import sys

    import pytest

    from moex_spread_lab.proof.strategy_catalog import get_strategy_registry

    baseline = get_strategy_registry().ids()
    path = list(sys.path)
    environment = dict(os.environ)
    for strategy in ("demo_calendar", "reinstalled_calendar"):
        attempt = tmp_path / strategy
        attempt.mkdir()
        source = PLUGIN_SOURCE.replace("demo_calendar", strategy)
        with (
            pytest.raises(RuntimeError, match="abort"),
            installed_plugin(attempt, monkeypatch, "reused_calendar", source, {strategy: "PLUGIN"}),
        ):
            registry = get_strategy_registry()
            assert strategy in registry
            assert strategy in get_strategy_axes(registry)
            raise RuntimeError("abort")
        assert "reused_calendar" not in sys.modules
        assert get_strategy_registry().ids() == baseline
        assert sys.path == path
        assert dict(os.environ) == environment
