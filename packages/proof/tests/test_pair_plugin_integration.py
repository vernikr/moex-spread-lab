"""Registered pair plugins retain native proof, conformance and search contracts."""

import json
import math

import pytest

from funding_fixtures import PAIR, WEEKEND_PAIR, fixture, request, weekend
from moex_spread_lab.core.settings import ResearchSettings, save_settings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof.proof_search import research_run
from moex_spread_lab.proof.strategy_catalog import get_strategy_axes
from perf_equivalence_fixtures import assert_pool_parity, assert_same_fields, installed_plugin
from test_execution_proof import _far_close, _near_close

PLUGIN_SOURCE = """
from dataclasses import replace
from moex_spread_lab.strategies.pair_mean_reversion.plugin import PairMeanReversionPlugin
class Demo(PairMeanReversionPlugin):
    strategy_id = "demo_pair"
    family_id = "demo_pair"
    proof_adapter = "daily_pair_price_intents.v1"
    requires_daily_bars = True
    def evaluate_intents(self, window, params, fill):
        run = super().evaluate_intents(window, params, fill)
        days = [row["date"] for row in window.rows]
        delay = 0 if fill == "reference_same_bar" else 1
        return replace(run, intents=tuple({**item, "signal": {"kind": "z",
            "date": days[days.index(item["entry_date"][:10] if window.timeframe == "1d" else item["entry_date"][:19].replace(" ", "T")) - delay],
            "value": item["entry_signal_z"]}} for item in run.intents))
PLUGIN = Demo()
class HourlyDemo(Demo):
    strategy_id = "demo_pair_hourly"
    family_id = "demo_pair_hourly"
    proof_adapter = "pair_price_intents.v1"
    requires_daily_bars = False
HOURLY = HourlyDemo()
"""


def _pair_workspace(root):
    fixture(root)
    weekend(root)
    path = root / "data/w1/current-intake.json"
    contracts = json.loads(path.read_text())
    for old, new in (("CNYRUBF", "GLZ6"), ("CRZ6", "GLH7")):
        original = next(c for c in contracts["instruments"] if c["secid"] == old)
        contracts["instruments"].append({**original, "secid": new, "stable_id": new})
        source = root / "data/w1/candles" / (old + "_1d.json")
        bars = json.loads(source.read_text())
        bars["secid"] = new
        (source.parent / (new + "_1d.json")).write_text(json.dumps(bars))
    path.write_text(json.dumps(contracts))
    save_settings(root, {"random_seed": 41, "validation_percent": 20})


def _reject_invalid_pair_replays(service, candidate):
    with pytest.raises(W0Error):
        service.prepare({**candidate, "quantity": 2})
    with pytest.raises(W0Error):
        service.prepare(candidate, settings=ResearchSettings(timeframe="1h"))
    prepared = service.prepare(candidate, settings=ResearchSettings())
    with pytest.raises(W0Error):
        prepared({**candidate, "pair": "GLZ6/GLH7"})


def _hourly_pair_workspace(root):
    for secid, close_fn in (("CNYRUBF", _near_close), ("CRZ6", _far_close)):
        path = root / "data/w1/candles" / (secid + "_1d.json")
        daily = json.loads(path.read_text())
        hourly = []
        for i, day_row in enumerate(daily["rows"]):
            for j, hour in enumerate((9, 10, 11, 12)):
                close = close_fn(i) + 0.28 * math.sin(i * 0.65 + j * 1.1 + (secid == "CRZ6"))
                hourly.append(
                    {
                        "begin": day_row["begin"][:10] + f" {hour:02}:00:00",
                        "open": close - 0.025,
                        "high": close + 0.04,
                        "low": close - 0.04,
                        "close": close,
                        "volume": 200,
                        "price_proxy_kind": "candle_close",
                    }
                )
        (path.parent / (secid + "_1h.json")).write_text(
            json.dumps(
                {
                    "schema_version": "iss-candles.v1",
                    "secid": secid,
                    "interval": 60,
                    "timeframe": "1h",
                    "rows": hourly,
                }
            )
        )
    save_settings(root, {"timeframe": "1h", "validation_percent": 20, "random_seed": 41})


def _hourly_pair_parity(h1):
    h1_plugins = {p["id"]: p for p in h1.catalog()["pair_screen"]["proof_plugins"]}
    assert not h1_plugins["demo_pair"]["timeframe_supported"]
    assert h1_plugins["demo_pair_hourly"]["timeframe_supported"]
    for fill in ("next_open", "next_close", "reference_same_bar"):
        for auto in (False, True):
            base = {**request(), "fill": fill}
            if auto:
                base.pop("quantity")
                base["sizing"] = "max_affordable"
            custom = {**base, "strategy": "demo_pair_hourly"}
            actual = research_run(h1, custom, optimize=False, include_plateau=False)
            expected = research_run(h1, base, optimize=False, include_plateau=False)
            assert_same_fields(actual, expected, "trades", "ledger", "summary")
            assert actual["evaluation"]["result"] == expected["evaluation"]["result"]
            assert actual["provenance"]["funding_applicability"] == "intraday_not_included"
            cutoff = actual["evaluation"]["periods"]["training"][-1]
            settings = ResearchSettings(timeframe="1h", validation_percent=20)
            a = h1.prepare(custom, settings=settings)(custom, through=cutoff)
            e = h1.prepare(base, settings=settings)(base, through=cutoff)
            assert a["trades"] == e["trades"] and a["ledger"] == e["ledger"]
            day_cutoff = cutoff[:10]
            a_day = h1.prepare(custom, settings=settings)(custom, through=day_cutoff)
            e_day = h1.prepare(base, settings=settings)(base, through=day_cutoff)
            assert a_day["trades"] == e_day["trades"] and a_day["ledger"] == e_day["ledger"]
    h1_search = research_run(h1, {**custom, "optimize_strategy": True}, optimize=True)
    h1_row = next(
        row
        for row in h1_search["strategy_search"]["alternatives"]
        if row["strategy"] == "demo_pair_hourly"
    )
    assert h1_row["trial_count"] == 80, h1_row
    assert all(
        row["strategy"] != "demo_pair" for row in h1_search["strategy_search"]["alternatives"]
    )


def _automatic_pair_fills(service):
    for fill in ("next_open", "next_close", "reference_same_bar"):
        base = {**request(), "sizing": "max_affordable", "fill": fill}
        base.pop("quantity")
        candidate = {**base, "strategy": "demo_pair"}
        actual = research_run(service, candidate, optimize=False, include_plateau=False)
        expected = research_run(service, base, optimize=False, include_plateau=False)
        assert actual["selection"]["strategy"] == "demo_pair"
        assert_same_fields(actual, expected, "trades", "ledger")
        assert actual["summary"]["ledger_reconciliation_rub"] == 0
        assert actual["evaluation"]["result"] == expected["evaluation"]["result"]


def _fixed_pair_fills(service):
    for fill in ("next_open", "next_close", "reference_same_bar"):
        base = {**request(), "fill": fill}
        fixed = {**base, "strategy": "demo_pair"}
        actual = research_run(service, fixed, optimize=False, include_plateau=False)
        expected = research_run(service, base, optimize=False, include_plateau=False)
        assert_same_fields(actual, expected, "trades", "ledger", "summary")
        assert actual["evaluation"]["result"] == expected["evaluation"]["result"]
        cutoff = actual["evaluation"]["periods"]["training"][-1]
        actual_prefix = service.prepare(fixed, settings=ResearchSettings(validation_percent=20))(
            fixed, through=cutoff
        )
        expected_prefix = service.prepare(base, settings=ResearchSettings(validation_percent=20))(
            base, through=cutoff
        )
        assert_same_fields(actual_prefix, expected_prefix, "trades", "ledger", "summary")


def _weekend_and_dated_pair_fills(service):
    for fill in ("next_open", "next_close", "reference_same_bar"):
        weekend_request = {**request(pair=WEEKEND_PAIR), "fill": fill}
        actual_weekend = research_run(
            service,
            {**weekend_request, "strategy": "demo_pair"},
            optimize=False,
            include_plateau=False,
        )
        expected_weekend = research_run(
            service, weekend_request, optimize=False, include_plateau=False
        )
        assert_same_fields(actual_weekend, expected_weekend, "trades", "ledger", "summary")
        dated = {**request(pair="GLZ6/GLH7"), "fill": fill}
        actual = research_run(
            service, {**dated, "strategy": "demo_pair"}, optimize=False, include_plateau=False
        )
        expected = research_run(service, dated, optimize=False, include_plateau=False)
        assert_same_fields(actual, expected, "trades", "ledger", "summary")


def _pair_quantity_and_slippage(service):
    for qty in (1, 3):
        for slip in (0.0, 2.5):
            base = {**request(), "fill": "next_open", "quantity": qty, "slippage_ticks": slip}
            actual = research_run(
                service, {**base, "strategy": "demo_pair"}, optimize=False, include_plateau=False
            )
            expected = research_run(service, base, optimize=False, include_plateau=False)
            assert_same_fields(actual, expected, "trades", "ledger", "summary")


def test_drop_in_pair_plugin_proof_and_search(tmp_path, monkeypatch):
    with installed_plugin(
        tmp_path,
        monkeypatch,
        "demo_pair",
        PLUGIN_SOURCE,
        {"demo_pair": "PLUGIN", "demo_pair_hourly": "HOURLY"},
    ) as service_factory:
        root = tmp_path / "root"
        _pair_workspace(root)
        assert "demo_pair" in get_strategy_axes()
        service = service_factory(root)
        plugins = service.catalog()["pair_screen"]["proof_plugins"]
        assert [p["id"] for p in plugins] == ["demo_pair", "demo_pair_hourly"]
        assert all(p["timeframe_supported"] for p in plugins)
        assert all(
            {s["name"] for s in p["parameters"]} == {"lookback", "entry_z", "exit_z"}
            for p in plugins
        )
        _automatic_pair_fills(service)
        _fixed_pair_fills(service)
        _weekend_and_dated_pair_fills(service)
        _pair_quantity_and_slippage(service)
        candidate = {
            **request(),
            "sizing": "max_affordable",
            "fill": "reference_same_bar",
            "strategy": "demo_pair",
        }
        candidate.pop("quantity")
        _reject_invalid_pair_replays(service, candidate)
        search = research_run(service, {**candidate, "optimize_strategy": True}, optimize=True)
        row = next(
            item
            for item in search["strategy_search"]["alternatives"]
            if item["strategy"] == "demo_pair"
        )
        assert row["trial_count"] == 80, row
        assert_pool_parity(
            root, candidate, (PAIR, WEEKEND_PAIR, "GLZ6/GLH7"), service_factory, monkeypatch
        )
        _hourly_pair_workspace(root)
        _hourly_pair_parity(service_factory(root))
