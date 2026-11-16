import http.client
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from moex_spread_lab.screener_app import (
    ScreenerError,
    ScreenerInputs,
    ScreenerStore,
    build_result,
    catalog,
    make_handler,
)
from moex_spread_lab.screener_search import screen_table


class ScreenerAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = ScreenerInputs(self.root)
        self.inputs.contracts.parent.mkdir(parents=True)
        instruments = []
        for secid in ("CNYRUBF", "CRZ6", "GLDRUBF", "GLZ6"):
            instruments.append(
                {
                    "secid": secid,
                    "stable_id": secid,
                    "board": "RFUD",
                    "expiry": "2026-12-17",
                    "lot_size": 1000,
                    "tick_size": 0.001,
                    "tick_value": 1,
                    "initial_margin": 1000,
                    "source_vintage": "current_snapshot",
                }
            )
        self.inputs.contracts.write_text(
            json.dumps({"schema_version": "iss-intake.v1", "instruments": instruments})
        )
        self.inputs.benchmark.parent.mkdir(parents=True)
        days = [date(2026, 3, 23) + timedelta(days=i) for i in range(50)]
        self.inputs.benchmark.write_text(
            json.dumps(
                {
                    "schema_version": "lqdt-lineage.v1",
                    "rows": [
                        {
                            "date": day.isoformat(),
                            "price": 1 + i * 0.0003,
                            "source_kind": "observed",
                            "source_ticker": "LQDT",
                        }
                        for i, day in enumerate(days)
                    ],
                }
            )
        )
        for pair in ("CNYRUBF/CRZ6", "GLDRUBF/GLZ6"):
            near, far = pair.split("/")
            for secid, offset in ((near, 0), (far, 1)):
                path = self.inputs.candles(pair, secid)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        {
                            "schema_version": "iss-candles.v1",
                            "secid": secid,
                            "interval": 24,
                            "rows": [
                                {
                                    "begin": f"{day.isoformat()} 00:00:00",
                                    "open": 10 + offset + i * 0.01 + (i % 8) * 0.003 - 0.002,
                                    "close": 10 + offset + i * 0.01 + (i % 8) * 0.003,
                                    "volume": 500,
                                    "price_proxy_kind": "candle_close",
                                }
                                for i, day in enumerate(days)
                            ],
                        }
                    )
                )
        self.days = days

    def test_pair_choice_parameter_change_and_replay(self):
        self.assertTrue(
            catalog(self.inputs)["pairs"][0]["strategies"]["mean_reversion"]
        )
        base = {"pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion", "slippage_ticks": 1.0}
        first = build_result(self.inputs, base)
        self.assertEqual(first, build_result(self.inputs, base))
        self.assertEqual(first["daily"][-1]["X"], first["metrics"]["excess_rub"])
        self.assertEqual(first["period"]["observations"], 50)
        self.assertEqual(first["price_role"], "ISS: дневной candle, заполнение по открытию, отметки по закрытию")
        self.assertNotEqual(
            first["run_id"],
            build_result(self.inputs, {**base, "parameters": {"lookback": 15}})[
                "run_id"
            ],
        )
        self.assertNotEqual(
            first["run_id"],
            build_result(self.inputs, {**base, "pair": "GLDRUBF/GLZ6"})["run_id"],
        )

    def test_table_applies_one_rule_to_all_rows_and_withholds_short_history(self):
        table = screen_table(
            self.inputs,
            {
                "strategy": "mean_reversion",
                "parameters": {"lookback": 15, "entry_z": 1.5, "exit_z": 0.4},
                "slippage_ticks": 1.0,
            },
        )
        rows = {row["id"]: row for row in table["pairs"]}
        self.assertGreaterEqual(len(rows), 12)
        self.assertEqual(rows["CNYRUBF/CRZ6"]["status"], "short_history")
        self.assertEqual(rows["GLDRUBF/GLZ6"]["status"], "short_history")
        self.assertIsNone(rows["CNYRUBF/CRZ6"]["metrics"]["annualized_excess_pp"])
        self.assertEqual(rows["GLDRUBF/GLZ6"]["chart"][0]["strategy_index"], 100)
        self.assertTrue(
            all("rub" not in key.lower() for key in rows["GLDRUBF/GLZ6"]["metrics"])
        )
        self.assertEqual(rows["CRZ6/CRH7"]["status"], "unavailable")

    def test_pair_with_a_thin_day_is_excluded_and_quantity_scales_margin(self):
        path = self.inputs.candles("GLDRUBF/GLZ6", "GLZ6")
        payload = json.loads(path.read_text())
        payload["rows"][7]["volume"] = 10
        path.write_text(json.dumps(payload))
        entry = next(p for p in catalog(self.inputs)["pairs"] if p["id"] == "GLDRUBF/GLZ6")
        self.assertEqual(entry["status"], "excluded_thin_leg")
        self.assertEqual(entry["min_leg_daily_volume"], 10)
        self.assertFalse(any(entry["strategies"].values()))
        table = screen_table(self.inputs, {"strategy": "mean_reversion"})
        self.assertEqual({r["id"]: r for r in table["pairs"]}["GLDRUBF/GLZ6"]["status"], "excluded")
        base = {"pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion"}
        one = build_result(self.inputs, base)
        two = build_result(self.inputs, {**base, "quantity": 2})
        self.assertEqual(
            two["metrics"]["current_pair_margin_rub"], 2 * one["metrics"]["current_pair_margin_rub"]
        )
        with self.assertRaises(ScreenerError):
            build_result(self.inputs, {**base, "quantity": 0})

    def test_cli_and_view_share_the_same_result(self):
        selection = {
            "pair": "GLDRUBF/GLZ6",
            "strategy": "mean_reversion",
            "slippage_ticks": 0.5,
            "parameters": {"lookback": 15, "entry_z": 1.5, "exit_z": 0.4},
        }
        expected = build_result(self.inputs, selection)
        output = self.root / "output.json"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "moex_spread_lab.cli",
                "screener-run",
                "--root",
                str(self.root),
                "--pair",
                selection["pair"],
                "--strategy",
                selection["strategy"],
                "--slippage-ticks",
                str(selection["slippage_ticks"]),
                "--lookback",
                "15",
                "--entry-z",
                "1.5",
                "--exit-z",
                "0.4",
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        actual = json.loads(output.read_text())
        self.assertEqual(expected, actual)
        self.assertEqual(expected["run_id"], json.loads(completed.stdout)["run_id"])

    def test_funding_strategy_and_invalid_input(self):
        near_rows = [
            {
                "TRADEDATE": day.isoformat(),
                "SETTLEPRICE": 10 + i * 0.01,
                "SWAPRATE": 0.0001,
            }
            for i, day in enumerate(self.days)
        ]
        far_rows = [
            {"TRADEDATE": day.isoformat(), "SETTLEPRICE": 11 + i * 0.01, "SWAPRATE": 0}
            for i, day in enumerate(self.days)
        ]

        def history(secid, *_args, **_kwargs):
            return {
                "schema_version": "iss-history.v1",
                "secid": secid,
                "rows": near_rows if secid == "CNYRUBF" else far_rows,
                "source": {"raw_response_sha256": [secid]},
            }

        cache = self.inputs.history_cache("CNYRUBF/CRZ6")
        cache.mkdir(parents=True)
        for secid in ("CNYRUBF", "CRZ6"):
            (cache / f"{secid}-2025-06-17-2026-10-27-0.json").write_text("{}")
        with patch("moex_spread_lab.screener_app.fetch_history", side_effect=history):
            result = build_result(
                self.inputs,
                {
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "funding_only",
                    "parameters": {"lookback": 12},
                },
            )
        self.assertEqual(result["selection"]["parameters"]["lookback"], 12)
        self.assertEqual(result["period"]["observations"], 50)
        with self.assertRaises(ScreenerError):
            build_result(
                self.inputs,
                {
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "parameters": {"exit_z": 3},
                },
            )
        with self.assertRaises(ScreenerError):
            build_result(
                self.inputs,
                {
                    "pair": "CNYRUBF/CRZ6",
                    "strategy": "mean_reversion",
                    "parameters": {"quantity": 100},
                },
            )
        with self.assertRaises(ScreenerError):
            build_result(
                self.inputs, {"pair": "UNKNOWN/PAIR", "strategy": "mean_reversion"}
            )

    def test_loopback_http_and_saved_result(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(ScreenerStore(self.inputs))
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/api/catalog")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("pairs", json.loads(response.read()))
        payload = json.dumps({"pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion"})
        headers = {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{server.server_port}",
        }
        connection.request("POST", "/api/run", payload, headers)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        result = json.loads(response.read())
        self.assertEqual(len(ScreenerStore(self.inputs).list_runs()), 1)
        connection.request("POST", "/api/run", payload, headers)
        repeated = connection.getresponse()
        self.assertEqual(repeated.status, 200)
        repeated.read()
        screen_payload = json.dumps(
            {
                "strategy": "mean_reversion",
                "parameters": {"lookback": 20, "entry_z": 2, "exit_z": 0.5},
            }
        )
        connection.request("POST", "/api/screen", screen_payload, headers)
        screen_response = connection.getresponse()
        self.assertEqual(screen_response.status, 200)
        self.assertEqual(len(json.loads(screen_response.read())["pairs"]), 12)
        self.assertEqual(
            len(ScreenerStore(self.inputs)._recent(self.inputs.screen_history)), 1
        )
        search_payload = json.dumps(
            {"pairs": ["CNYRUBF/CRZ6"], "strategy": "mean_reversion"}
        )
        connection.request("POST", "/api/optimize", search_payload, headers)
        short_history = connection.getresponse()
        self.assertEqual(short_history.status, 400)
        self.assertIn("common dates", json.loads(short_history.read())["error"])
        connection.close()
        self.assertEqual(len(ScreenerStore(self.inputs).list_runs()), 1)
        self.assertEqual(
            result["run_id"], ScreenerStore(self.inputs).list_runs()[0]["run_id"]
        )

    def test_http_rejects_cross_origin_and_network_fallback(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(ScreenerStore(self.inputs))
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "POST",
            "/api/run",
            "{}",
            {"Content-Type": "application/json", "Origin": "https://untrusted.example"},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        connection.close()
        with self.assertRaises(OSError):
            from moex_spread_lab.screener_app import _offline_only

            _offline_only("https://iss.moex.com/")


if __name__ == "__main__":
    unittest.main()
