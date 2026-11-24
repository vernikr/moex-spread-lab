"""Calendar proof characterization tests.

The synthetic fixture builders live in
``calendar_proof_fixtures_p72_packages_proof_tests_test_calendar_proof`` and
the named assertion helpers live in
``calendar_proof_expectations_p72_packages_proof_tests_test_calendar_proof``;
both were extracted under P7.2 test-health work (Issue #121) so this module
meets the code-health limits without its size baseline entry.
"""

import http.client
import json
import tempfile
import unittest
from pathlib import Path

from calendar_proof_expectations_p72_packages_proof_tests_test_calendar_proof import (
    assert_h1_convergence_to_ruonia,
    assert_h1_proxy_window,
    assert_instrument_provenance,
    assert_ledger_reconciliation,
    assert_spread_relative_to_far_price,
    assert_zero_volume_day_isolated,
)
from calendar_proof_fixtures_p72_packages_proof_tests_test_calendar_proof import (
    TILL,
    _build_root,
    _h1_rows,
    _proof_headers,
    _serve_proof_ui,
    _write_h1_candles,
    _write_h1_http_prereqs,
    _zscore_request,
)
from calendar_proof_fixtures_p72_packages_proof_tests_test_calendar_proof import (
    _write_history as _write_history,  # re-export for read-only consumers (tests/proof_fixtures.py)
)
from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData


class CalendarProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        _build_root(self.root)
        self.data = CalendarData(self.root)

    def test_catalog_lists_window_with_volume_and_capital_flags(self):
        catalog = self.data.catalog()
        self.assertEqual(catalog["till"], TILL)
        asset = next(a for a in catalog["assets"] if a["asset"] == "SR")
        window = asset["windows"][0]
        self.assertEqual(window["pair"], "SRH3/SRM3")
        self.assertTrue(window["tradable_every_day"])
        self.assertTrue(window["passes_capital_screen"])

    def test_series_and_fills_spread_is_relative_to_far_price(self):
        proof = self.data.proof(_zscore_request())
        assert_spread_relative_to_far_price(self, proof)

    def test_zero_volume_spread_date_stays_in_journal_but_is_not_an_analysis_or_fill_day(self):
        path = self.root / "data/spread-history-cache/SRH3SRM3-2022-06-01-2023-06-15-0.json"
        payload = json.loads(path.read_text())
        block = payload["payload"]["history"]
        volume_index = block["columns"].index("VOLUME")
        date_index = block["columns"].index("TRADEDATE")
        zero_day = block["data"][30][date_index]
        block["data"][30][volume_index] = 0
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.data._loaded = None
        proof = self.data.proof(_zscore_request(quantity=1))
        assert_zero_volume_day_isolated(self, proof, zero_day)

    def test_proof_reconciles_ledger_to_trades_and_is_strict_json(self):
        proof = self.data.proof(_zscore_request())
        json.dumps(proof, allow_nan=False)
        assert_ledger_reconciliation(self, proof)

    def test_invalid_selections_are_rejected(self):
        base = {"asset": "SR", "pair": "SRH3/SRM3"}
        for bad in (
            {**base, "quantity": 0},
            {**base, "asset": "XX"},
            {**base, "pair": "SRM3/SRU3"},
            {**base, "strategy": "x"},
            {**base, "parameters": {"unknown": 1}},
            {**base, "parameters": {"entry_z": -1}},
        ):
            with self.assertRaises(W0Error):
                self.data.proof(bad)

    def test_http_routes_serve_page_catalog_and_proof(self):
        server = _serve_proof_ui(self, self.root)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/proof")
        page = connection.getresponse()
        self.assertEqual(page.status, 200)
        self.assertIn("Доказательство исполнения".encode(), page.read())
        connection.request("GET", "/api/proof/catalog")
        catalog = connection.getresponse()
        self.assertEqual(catalog.status, 200)
        catalog.read()
        headers = _proof_headers(server)
        connection.request(
            "POST", "/api/proof/run", json.dumps({"asset": "SR", "pair": "SRH3/SRM3"}), headers
        )
        run = connection.getresponse()
        self.assertEqual(run.status, 200)
        self.assertEqual(json.loads(run.read())["selection"]["pair"], "SRH3/SRM3")
        connection.request(
            "POST", "/api/proof/run", json.dumps({"asset": "SR", "pair": "nope"}), headers
        )
        bad = connection.getresponse()
        self.assertEqual(bad.status, 400)
        bad.read()
        connection.close()

    def test_h1_native_spread_has_priority_and_uses_its_own_session_volume_gate(self):
        _write_h1_candles(self.root, "SRH3", _h1_rows("near", 100))
        _write_h1_candles(self.root, "SRM3", _h1_rows("far", 100))
        _write_h1_candles(self.root, "SRH3SRM3", _h1_rows("spread", 5))

        window = self.data.window("SR", "SRH3/SRM3", "1h")
        self.assertEqual(window["price_source"], "native_h1_spread")
        self.assertTrue(window["has_native_spread_volume"])
        self.assertTrue(window["native_spread_order"])
        self.assertEqual(window["liquidity_source"], "native_spread_session_volume")
        self.assertEqual(window["liquidity_gate"], 100)
        self.assertEqual(window["min_liquidity_volume"], 60)
        self.assertFalse(window["tradable_every_session"])
        self.assertEqual(window["rows"][0]["spread_volume"], 5)
        self.assertNotEqual(window["rows"][0]["spread_price"], 1000.0)

    def test_h1_proxy_is_synchronized_causal_and_reconciles(self):
        _write_h1_candles(self.root, "SRH3", _h1_rows("near", 20))
        _write_h1_candles(self.root, "SRM3", _h1_rows("far", 20))

        proof = self.data.proof(_zscore_request(quantity=1, window=3, timeframe="1h"))
        assert_h1_proxy_window(self, proof)
        json.dumps(proof, allow_nan=False)
        convergence = self.data.proof(
            {
                "asset": "SR",
                "pair": "SRH3/SRM3",
                "timeframe": "1h",
                "strategy": "convergence_to_ruonia",
                "fill": "next_open",
                "quantity": 1,
                "slippage_ticks": 1.0,
                "parameters": {"threshold_pp": 0.1},
            }
        )
        assert_h1_convergence_to_ruonia(self, convergence)

    def test_h1_training_gate_uses_only_the_training_prefix(self):
        _write_h1_candles(self.root, "SRH3", _h1_rows("near", 20, first_session_volume=5))
        _write_h1_candles(self.root, "SRM3", _h1_rows("far", 20, first_session_volume=5))
        strict = ResearchSettings(
            validation_percent=25,
            allow_failed_volume=False,
            allow_failed_capital=True,
            initial_capital_rub=100000.0,
            timeframe="1h",
        )
        with self.assertRaisesRegex(W0Error, "ликвидности на обучении"):
            self.data.check_training_window("SR", "SRH3/SRM3", 1, strict)
        permissive = ResearchSettings(
            validation_percent=25,
            allow_failed_volume=True,
            allow_failed_capital=True,
            initial_capital_rub=100000.0,
            timeframe="1h",
        )
        self.data.check_training_window("SR", "SRH3/SRM3", 1, permissive)

    def test_http_h1_catalog_and_proof_route(self):
        _write_h1_candles(self.root, "SRH3", _h1_rows("near", 20))
        _write_h1_candles(self.root, "SRM3", _h1_rows("far", 20))
        _write_h1_http_prereqs(self.root)

        server = _serve_proof_ui(self, self.root)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/api/proof/catalog")
        catalog_response = connection.getresponse()
        self.assertEqual(catalog_response.status, 200)
        catalog = json.loads(catalog_response.read())
        self.assertEqual(catalog["timeframe"], "1h")
        by_id = {item["id"]: item for item in catalog["timeframes"]}
        self.assertTrue(by_id["1h"]["supports_calendar"])
        self.assertEqual([item["id"] for item in catalog["timeframes"]], ["1h", "1d"])
        catalog_window = next(
            window
            for asset in catalog["assets"]
            if asset["asset"] == "SR"
            for window in asset["windows"]
            if window["pair"] == "SRH3/SRM3"
        )
        self.assertEqual(catalog_window["price_source"], "synthetic_leg_proxy")
        self.assertEqual(catalog_window["liquidity_source"], "matched_near_far_session_volume")

        headers = _proof_headers(server)
        request = {
            "universe": "calendar_native",
            "asset": "SR",
            "pair": "SRH3/SRM3",
            "strategy": "zscore_mean_reversion",
            "fill": "next_open",
            "sizing": "max_affordable",
            "parameters": {"entry_z": 1.5, "exit_z": 0.5, "window": 3},
        }
        connection.request("POST", "/api/proof/run", json.dumps(request), headers)
        response = connection.getresponse()
        body = response.read()
        self.assertEqual(response.status, 200, body.decode())
        proof = json.loads(body)
        self.assertEqual(proof["selection"]["timeframe"], "1h")
        self.assertEqual(proof["window"]["price_source"], "synthetic_leg_proxy")
        self.assertEqual(proof["window"]["liquidity_gate_unit"], "контр./сессию")
        self.assertEqual(proof["summary"]["ledger_reconciliation_rub"], 0.0)
        self.assertEqual(
            proof["summary"]["closed_trades"],
            sum(not trade["open_at_end"] for trade in proof["trades"]),
        )
        self.assertEqual(proof["sizing"]["policy"], "max-affordable.v1")
        self.assertGreater(proof["summary"]["max_quantity"], 0)
        self.assertEqual(proof["evaluation"]["failed_screens"], [])
        self.assertGreater(proof["summary"]["cost_totals"]["exchange_clearing"], 0)
        self.assertGreater(proof["summary"]["cost_totals"]["slippage"], 0)
        json.dumps(proof, allow_nan=False)
        connection.close()

    def test_service_calendar_payload_adds_instrument_provenance_and_run_id(self):
        from moex_spread_lab.proof.service import ProofService

        proof = ProofService(self.root).proof({"universe": "calendar_native", **_zscore_request()})
        assert_instrument_provenance(self, proof)


if __name__ == "__main__":
    unittest.main()
