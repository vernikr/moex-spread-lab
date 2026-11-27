"""Registered carry plugins retain native proof, conformance and search contracts."""

import json
from itertools import product
from typing import Any

import pytest

from funding_fixtures import PAIR, WEEKEND_PAIR, change, fixture, page, request, weekend
from moex_spread_lab.core.settings import ResearchSettings, save_settings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.data.universe import HISTORY_WINDOWS
from moex_spread_lab.proof.pair_route import _validate_carry_result
from moex_spread_lab.proof.proof_search import research_run
from moex_spread_lab.proof.strategy_catalog import get_proof_adapter_ids
from moex_spread_lab.strategies.base import StrategyRegistry
from moex_spread_lab.strategies.funding_carry.carry import _history_url
from moex_spread_lab.strategies.funding_carry.plugin import FundingCarryPlugin
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs
from perf_equivalence_fixtures import assert_pool_parity, installed_plugin

PLUGIN_SOURCE = """
from moex_spread_lab.strategies.funding_carry.plugin import FundingCarryPlugin
class Demo(FundingCarryPlugin):
    strategy_id = "demo_carry"
    family_id = "demo_carry"
    proof_adapter = "carry_settlement_intents.v1"
PLUGIN = Demo()
from dataclasses import replace
class Mutating(Demo):
    strategy_id = "mutating_carry"
    family_id = "mutating_carry"
    def check_compatibility(self, window, settings=None):
        verdict = super().check_compatibility(window, settings)
        window.carry_study["rows"][0]["near_settle"] = 999999
        return verdict
    def evaluate_intents(self, window, params, fill):
        run = super().evaluate_intents(window, params, fill)
        window.carry_study["rows"][0]["near_settle"] = 999999
        return replace(run, intents=tuple({**t, "gross_pnl": 99999999, "funding_rub":99999999} for t in run.intents))
MUTATING = Mutating()
"""


def _carry_case(service, days, validation, auto, slip):
    settings = ResearchSettings(validation_percent=validation, random_seed=41, slippage_ticks=slip)
    native = {**request("funding_only"), "fill": "same_day_settlement", "slippage_ticks": slip}
    if auto:
        native.pop("quantity")
        native["sizing"] = "max_affordable"
    plugin = {**native, "strategy": "demo_carry"}
    n = service.prepare(native, settings=settings)
    p = service.prepare(plugin, settings=settings)
    for through in (None, days[38]):
        a, b = (p(plugin, through=through), n(native, through=through))
        assert a["selection"]["strategy"] == "demo_carry"
        assert a["trades"] == b["trades"], (auto, slip, through, a["trades"], b["trades"])
        assert a["ledger"] == b["ledger"], (auto, slip, through)
        assert a["summary"] == b["summary"], (auto, slip, through, a["summary"], b["summary"])
        assert a["series"] == b["series"], (auto, slip, through)
        fast_a, fast_b = (
            p(plugin, through=through, metrics_only=True),
            n(native, through=through, metrics_only=True),
        )
        assert fast_a["trades"] == fast_b["trades"], (
            auto,
            slip,
            through,
            fast_a["trades"],
            fast_b["trades"],
        )
        assert fast_a["ledger"] == fast_b["ledger"], (
            auto,
            slip,
            through,
            fast_a["ledger"],
            fast_b["ledger"],
        )
        if auto:
            from unittest.mock import patch

            with patch("moex_spread_lab.core._fastcore_loader.load_fastcore", return_value=None):
                python_a = p(plugin, through=through, metrics_only=True)
                python_b = n(native, through=through, metrics_only=True)
            assert python_a["ledger"] == python_b["ledger"]
            assert python_a["ledger"] == fast_a["ledger"]
    invalid_requests = [{**plugin, "pair": "GLZ6/GLH7"}, {**plugin, "universe": "calendar_native"}]
    if auto:
        invalid_requests += [{**plugin, "quantity": 2}, {**plugin, "sizing": None}]
    else:
        invalid_requests += [{**plugin, "sizing": "max_affordable"}]
    for invalid in invalid_requests:
        with pytest.raises(W0Error):
            p(invalid)


def _carry_fail_closed_and_mutation(service):
    with pytest.raises(W0Error):
        service.prepare(
            {**request("funding_only"), "strategy": "demo_carry"},
            settings=ResearchSettings(timeframe="1h"),
        )
    low = ResearchSettings(initial_capital_rub=1000, allow_failed_capital=True)
    small_native = {**request("funding_only"), "sizing": "max_affordable"}
    small_native.pop("quantity")
    small_plugin = {**small_native, "strategy": "demo_carry"}
    sa = service.prepare(small_plugin, settings=low)(small_plugin)
    sb = service.prepare(small_native, settings=low)(small_native)
    assert sa["skipped_entries"] and sa["skipped_entries"] == sb["skipped_entries"]
    assert not sa["trades"] and all(not row["funding_rub"] for row in sa["ledger"])
    native = request("funding_only")
    mutating = {**native, "strategy": "mutating_carry"}
    a, b = (
        service.prepare(mutating, settings=ResearchSettings())(mutating),
        service.prepare(native, settings=ResearchSettings())(native),
    )
    assert (
        a["trades"] == b["trades"] and a["ledger"] == b["ledger"] and (a["series"] == b["series"])
    )


def _estimated_funding_prefix(root, service_factory):
    gaproot = root.parent / "estimated"
    gapdays = fixture(gaproot)
    change(
        gaproot,
        "CNYRUBF",
        {
            gapdays[19]: 0.005,
            gapdays[20]: None,
            gapdays[21]: None,
            gapdays[22]: None,
            gapdays[23]: 0.035,
        },
    )
    gapsvc = service_factory(gaproot)
    ng = request("funding_only")
    pg = {**ng, "strategy": "demo_carry"}
    np = gapsvc.prepare(ng, settings=ResearchSettings())
    pp = gapsvc.prepare(pg, settings=ResearchSettings())
    na, pa = (np(ng), pp(pg))
    assert pa["trades"] == na["trades"] and pa["ledger"] == na["ledger"]
    assert pa["provenance"]["funding_estimated_dates"] == gapdays[20:23]
    for replay, req0 in ((np, ng), (pp, pg)):
        with pytest.raises(W0Error):
            replay(req0, through=gapdays[21])


def _carry_cohort_workspace(root):
    weekend(root)
    src = ScreenerInputs(root)
    third = "SBERF/SRZ6"
    contracts = json.loads(src.contracts.read_text())
    for old, new in zip(WEEKEND_PAIR.split("/"), third.split("/"), strict=True):
        contract = next(r for r in contracts["instruments"] if r["secid"] == old)
        contracts["instruments"].append({**contract, "secid": new, "stable_id": new})
        candles = json.loads(src.candles(WEEKEND_PAIR, old).read_text())
        candles["secid"] = new
        src.candles(third, new).write_text(json.dumps(candles))
        doc = json.loads(page(root, old, WEEKEND_PAIR).read_text())
        block = doc["payload"]["history"]
        idx = block["columns"].index("SECID")
        for row in block["data"]:
            row[idx] = new
        doc["url"] = _history_url(new, *HISTORY_WINDOWS[third], 0)
        out = page(root, new, third)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc))
    src.contracts.write_text(json.dumps(contracts))
    return third


def _exercise_plugin(root, monkeypatch, service_factory):
    days = fixture(root)
    change(root, "CNYRUBF", dict.fromkeys(days[20:32], -0.04))
    service = service_factory(root)
    meta = next(
        p for p in service.catalog()["pair_screen"]["proof_plugins"] if p["id"] == "demo_carry"
    )
    assert meta["adapter"] == "carry_settlement_intents.v1" and meta["timeframe_supported"]
    for validation, auto, slip in product((0, 20), (False, True), (0.0, 2.0)):
        _carry_case(service, days, validation, auto, slip)
    _carry_fail_closed_and_mutation(service)
    _estimated_funding_prefix(root, service_factory)
    save_settings(root, {"random_seed": 41, "validation_percent": 20})
    req = {
        **request("funding_only"),
        "strategy": "demo_carry",
        "sizing": "max_affordable",
        "fill": "same_day_settlement",
    }
    req.pop("quantity")
    search = research_run(service, {**req, "optimize_strategy": True}, optimize=True)
    row = next(
        x for x in search["strategy_search"]["alternatives"] if x["strategy"] == "demo_carry"
    )
    assert row["trial_count"] == 80, row
    third = _carry_cohort_workspace(root)
    assert_pool_parity(root, req, (PAIR, WEEKEND_PAIR, third), service_factory, monkeypatch)


def test_drop_in_carry_plugin_proof_and_search(tmp_path, monkeypatch):
    with installed_plugin(
        tmp_path,
        monkeypatch,
        "demo_carry",
        PLUGIN_SOURCE,
        {"demo_carry": "PLUGIN", "mutating_carry": "MUTATING"},
    ) as service_factory:
        _exercise_plugin(tmp_path / "root", monkeypatch, service_factory)


def _result() -> dict[str, Any]:
    return {"summary": {"ledger_reconciliation_rub": 0.0}}


def test_builtin_carry_is_discovered_as_an_adapted_strategy() -> None:
    from moex_spread_lab.strategies.base import carry_proof_plugin_ids

    plugins: dict[str, Any] = {"funding_only": FundingCarryPlugin()}
    registry = StrategyRegistry(plugins)
    assert carry_proof_plugin_ids(registry) == {"funding_only"}
    assert "funding_only" in get_proof_adapter_ids(registry)


def test_builtin_carry_keeps_its_native_payload_shape() -> None:
    result = _result()
    _validate_carry_result(result, FundingCarryPlugin(), metrics_only=False)
    assert "accounting_policy" not in result


def test_drop_in_carry_marks_the_settlement_accounting() -> None:
    class DropIn(FundingCarryPlugin):
        strategy_id = "drop_in_carry"
        family_id = "drop_in_carry"

    result = _result()
    _validate_carry_result(result, DropIn(), metrics_only=False)
    assert result["accounting_policy"] == "carry_settlement.v1"
