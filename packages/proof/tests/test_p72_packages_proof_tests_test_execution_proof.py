import http.client
import json
import math
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof.service import ProofService
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs
from moex_spread_lab.web.server import ScreenerStore, make_handler
from test_execution_proof import _build_root, _far_close, _near_close


class ExecutionProofHourlyFundingAndRoutesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        _build_root(self.root)
        self.proof = ProofService(self.root)

    def test_hourly_pair_proof_uses_unique_timestamps_and_prior_daily_benchmark(self):
        from moex_spread_lab.core.settings import ResearchSettings, save_settings

        for secid, close_fn in (("CNYRUBF", _near_close), ("CRZ6", _far_close)):
            daily = json.loads((self.root / f"data/w1/candles/{secid}_1d.json").read_text())
            hourly_rows = []
            for i, day_row in enumerate(daily["rows"]):
                day = day_row["begin"][:10]
                base = close_fn(i)
                for j, hour in enumerate((9, 10, 11, 12)):
                    close = base + 0.28 * math.sin(i * 0.65 + j * 1.1 + (secid == "CRZ6"))
                    hourly_rows.append(
                        {
                            "begin": f"{day} {hour:02}:00:00",
                            "open": close - 0.025,
                            "high": close + 0.04,
                            "low": close - 0.04,
                            "close": close,
                            "volume": 200,
                            "price_proxy_kind": "candle_close",
                        }
                    )
            (self.root / f"data/w1/candles/{secid}_1h.json").write_text(
                json.dumps(
                    {
                        "schema_version": "iss-candles.v1",
                        "secid": secid,
                        "interval": 60,
                        "timeframe": "1h",
                        "rows": hourly_rows,
                    }
                )
            )
        save_settings(self.root, {"timeframe": "1h", "validation_percent": 20})
        catalog = self.proof.catalog()
        self.assertEqual(catalog["timeframe"], "1h")
        pair = next(row for row in catalog["pair_screen"]["pairs"] if row["id"] == "CNYRUBF/CRZ6")
        self.assertTrue(pair["proof_strategies"]["mean_reversion"])
        self.assertFalse(pair["proof_strategies"]["funding_only"])
        self.assertFalse(catalog["assets"])

        selection = {
            "universe": "pair_screen",
            "pair": "CNYRUBF/CRZ6",
            "strategy": "mean_reversion",
            "sizing": "max_affordable",
            "parameters": {"lookback": 5, "entry_z": 0.8, "exit_z": 0.2},
        }
        result = self.proof.proof(selection)
        ids = [row["date"] for row in result["series"]]
        self.assertGreater(len(ids), 100)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(
            all(row["benchmark_mark_date"] < row["session_date"] for row in result["series"])
        )
        self.assertEqual(result["window"]["min_leg_volume"], 800)
        self.assertEqual(result["selection"]["timeframe"], "1h")
        from moex_spread_lab.proof.proof_search import research_run

        evaluated = research_run(self.proof, selection, optimize=False, include_plateau=False)
        self.assertIsNotNone(evaluated["evaluation"]["periods"]["validation"])
        self.assertGreater(evaluated["evaluation"]["observations"]["validation"], 0)
        self.assertAlmostEqual(result["summary"]["ledger_reconciliation_rub"], 0.0, places=2)
        self.assertTrue(any("SWAPRATE не включён" in note for note in result["notes"]))

        cutoff = daily["rows"][10]["begin"][:10]
        replay = self.proof.prepare(
            selection,
            settings=ResearchSettings(timeframe="1h", validation_percent=20),
        )
        partial = replay(selection, through=cutoff)
        partial_sessions = [row["session_date"] for row in partial["series"]]
        self.assertTrue(partial_sessions)
        self.assertEqual(partial_sessions[-1], cutoff)
        self.assertTrue(all(day <= cutoff for day in partial_sessions))

    def test_funding_only_proof_reconciles_and_flags_open_position(self):
        result = self.proof.proof(
            {
                "universe": "pair_screen",
                "pair": "CNYRUBF/CRZ6",
                "strategy": "funding_only",
                "quantity": 2,
                "slippage_ticks": 1.0,
            }
        )
        json.dumps(result, allow_nan=False)
        self.assertGreaterEqual(len(result["trades"]), 1)
        summary = result["summary"]
        self.assertEqual(summary["ledger_reconciliation_rub"], 0.0)
        self.assertEqual(summary["open_positions"], 1)
        trade = result["trades"][0]
        self.assertTrue(trade["open_at_end"])
        self.assertEqual(trade["side"], "short_spread")
        self.assertEqual(trade["signal"]["reason"], "positive_trailing_funding")
        self.assertEqual(
            trade["exit"]["costs"], {"exchange_clearing": 0.0, "broker": 0.0, "slippage": 0.0}
        )
        funding_rows = [row for row in result["series"] if (row["funding_rub"] or 0) != 0]
        self.assertTrue(funding_rows)
        self.assertTrue(
            all(row["funding_swaprate_rub_per_unit"] is not None for row in funding_rows)
        )
        self.assertTrue(
            all(
                row["funding_lot_size"] > 0 and row["funding_contracts"] > 0 for row in funding_rows
            )
        )
        funding_ledger = sum(row["funding_rub"] or 0.0 for row in result["ledger"])
        self.assertGreater(funding_ledger, 0.0)
        self.assertAlmostEqual(
            result["ledger"][-1]["cumulative_net_rub"], summary["net_rub"], delta=0.02
        )

    def test_unknown_universe_and_invalid_selection_rejected(self):
        with self.assertRaises(W0Error):
            self.proof.proof({"universe": "moon"})
        with self.assertRaises(W0Error):
            self.proof.proof(
                {"universe": "pair_screen", "pair": "NOPE", "strategy": "mean_reversion"}
            )
        with self.assertRaises(W0Error):
            self.proof.proof(
                {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6", "strategy": "moon"}
            )
        with self.assertRaises(W0Error):
            self.proof.proof(
                {
                    "universe": "pair_screen",
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "quantity": 0,
                }
            )

    def test_http_routes_pair_proof_vendor_and_legacy_default(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(ScreenerStore(ScreenerInputs(self.root)))
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/vendor/lightweight-charts.standalone.production.js")
        vendor = connection.getresponse()
        self.assertEqual(vendor.status, 200)
        self.assertEqual(vendor.getheader("Content-Type"), "application/javascript; charset=utf-8")
        self.assertIn(b"LightweightCharts", vendor.read())
        headers = {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{server.server_port}",
        }
        connection.request(
            "POST",
            "/api/proof/run",
            json.dumps(
                {
                    "universe": "pair_screen",
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "parameters": {"entry_z": 1.5, "exit_z": 0.3, "lookback": 20},
                }
            ),
            headers,
        )
        run = connection.getresponse()
        self.assertEqual(run.status, 200)
        self.assertEqual(json.loads(run.read())["universe"], "pair_screen")
        connection.request("POST", "/api/proof/run", json.dumps({"universe": "moon"}), headers)
        bad = connection.getresponse()
        self.assertEqual(bad.status, 400)
        bad.read()
        connection.request(
            "POST", "/api/proof/run", json.dumps({"asset": "SR", "pair": "nope"}), headers
        )
        bad = connection.getresponse()
        self.assertEqual(bad.status, 400)
        bad.read()
        connection.request("GET", "/api/proof/catalog")
        catalog = connection.getresponse()
        self.assertEqual(catalog.status, 200)
        body = json.loads(catalog.read())
        self.assertIn("pair_screen", body)
        connection.close()

    def test_routes_tolerate_query_strings(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(ScreenerStore(ScreenerInputs(self.root)))
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "GET", "/proof?universe=pair_screen&pair=CNYRUBF/CRZ6&strategy=mean_reversion"
        )
        page = connection.getresponse()
        self.assertEqual(page.status, 200)
        self.assertIn("Доказательство исполнения".encode(), page.read())
        connection.request("GET", "/api/proof/catalog?universe=pair_screen")
        catalog = connection.getresponse()
        self.assertEqual(catalog.status, 200)
        self.assertIn("pair_screen", json.loads(catalog.read()))
        connection.request("GET", "/vendor/lightweight-charts.standalone.production.js?v=1")
        vendor = connection.getresponse()
        self.assertEqual(vendor.status, 200)
        vendor.read()
        connection.close()


if __name__ == "__main__":
    unittest.main()
