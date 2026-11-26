import http.client
import json
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

from moex_spread_lab.data.universe import HISTORICAL_PERPETUAL_PAIRS, HISTORY_WINDOWS
from moex_spread_lab.strategies.funding_carry.carry import HISTORY_FIELDS, _history_url
from moex_spread_lab.strategies.pair_screener.pair_screener import (
    ScreenerError,
    ScreenerInputs,
    _leg_volumes,
    _positive_leg_volume_dates,
    build_result,
    catalog,
)
from moex_spread_lab.web.server import ScreenerStore, make_handler


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
        days = []
        day = date(2026, 3, 23)
        while len(days) < 50:
            if day.weekday() < 5:
                days.append(day)
            day += timedelta(days=1)
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
            start, end = HISTORY_WINDOWS[pair]
            cache_path = self.inputs.history_cache(pair) / f"{near}-{start}-{end}-0.json"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            history_rows = [
                [
                    "RFUD",
                    d.isoformat(),
                    near,
                    10 + i * 0.01,
                    10 + i * 0.01,
                    0.015,
                    1000,
                    10000.0,
                    500,
                    10 + i * 0.01,
                    1,
                    None,
                ]
                for i, d in enumerate(days)
            ]
            request_url = _history_url(near, start, end, 0, HISTORY_FIELDS)
            cached_payload = {
                "history": {"columns": list(HISTORY_FIELDS), "data": history_rows},
                "history.cursor": {
                    "columns": ["INDEX", "TOTAL", "PAGESIZE"],
                    "data": [[0, len(history_rows), 100]],
                },
            }
            cache_path.write_text(
                json.dumps(
                    {
                        "url": request_url,
                        "payload": cached_payload,
                        "raw_response_sha256": "cd" * 32,
                    }
                )
            )
        self.days = days

    def test_hourly_price_pair_proof_does_not_require_daily_swaprate(self):
        pair = {"pair_id": "GLDRUBF/GLZ6", "near": "GLDRUBF", "far": "GLZ6"}
        for secid, offset in (("GLDRUBF", 0), ("GLZ6", 1)):
            path = self.inputs.candles(pair["pair_id"], secid, "1h")
            path.parent.mkdir(parents=True, exist_ok=True)
            rows = []
            for i, day in enumerate(self.days):
                for hour in (10, 11):
                    close = 10 + offset + i * 0.01 + (i % 8) * 0.003
                    rows.append(
                        {
                            "begin": f"{day.isoformat()} {hour:02d}:00:00",
                            "open": close,
                            "close": close,
                            "volume": 500,
                        }
                    )
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "iss-candles.v1",
                        "secid": secid,
                        "interval": 60,
                        "timeframe": "1h",
                        "rows": rows,
                    }
                )
            )

        entry = next(
            row
            for row in catalog(self.inputs, timeframe="1h")["pairs"]
            if row["id"] == pair["pair_id"]
        )
        self.assertEqual(entry["observations"], len(self.days) * 2)
        self.assertTrue(entry["proof_strategies"]["mean_reversion"])
        self.assertFalse(entry["proof_strategies"]["funding_only"])
        self.assertIn("carry требует дневных", entry["data_reasons"]["funding_only"])
        self.assertNotIn("SWAPRATE", entry["data_reasons"].get("mean_reversion", ""))

    def test_zero_volume_on_either_leg_is_excluded_from_gate_dates(self):
        pair = {"pair_id": "GLDRUBF/GLZ6", "near": "GLDRUBF", "far": "GLZ6"}
        near_path = self.inputs.candles(pair["pair_id"], pair["near"])
        payload = json.loads(near_path.read_text())
        zero_day = payload["rows"][5]["begin"][:10]
        payload["rows"][5]["volume"] = 0
        near_path.write_text(json.dumps(payload))
        self.assertEqual(_leg_volumes(self.inputs, pair), {"GLDRUBF": 500, "GLZ6": 500})
        self.assertNotIn(zero_day, _positive_leg_volume_dates(self.inputs, pair))

    def test_proof_ui_function_declarations_are_unique(self):
        js_dir = Path(__file__).resolve().parents[1] / "src/moex_spread_lab/web/static/js"
        js_files = sorted(js_dir.glob("*.js"))
        self.assertGreaterEqual(len(js_files), 9)
        declarations = []
        for path in js_files:
            declarations.extend(
                re.findall(
                    r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", path.read_text(encoding="utf-8")
                )
            )
        self.assertEqual(len(declarations), len(set(declarations)))

    def test_pair_registry_has_two_level_base_and_historical_gold_options(self):
        self.assertEqual(len(HISTORICAL_PERPETUAL_PAIRS), 85)
        pairs = {row["id"]: row for row in catalog(self.inputs)["pairs"]}
        self.assertEqual(pairs["GLDRUBF/GLZ6"]["near"], "GLDRUBF")
        self.assertEqual(pairs["GLDRUBF/GLU3"]["far"], "GLU3")
        self.assertIn("from", pairs["GLDRUBF/GLU3"])
        self.assertIn("to", pairs["GLDRUBF/GLU3"])

    def test_pair_choice_parameter_change_and_replay(self):
        self.assertTrue(catalog(self.inputs)["pairs"][0]["strategies"]["mean_reversion"])
        base = {"pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion", "slippage_ticks": 1.0}
        first = build_result(self.inputs, base)
        self.assertEqual(first, build_result(self.inputs, base))
        self.assertEqual(first["daily"][-1]["X"], first["metrics"]["excess_rub"])
        self.assertEqual(first["period"]["observations"], 50)
        self.assertIn("near_funding_history_sha256", first["provenance"])
        self.assertTrue(any("SWAPRATE" in note for note in first["notes"]))
        self.assertEqual(
            first["price_role"], "ISS: дневной candle, заполнение по открытию, отметки по закрытию"
        )
        self.assertNotEqual(
            first["run_id"],
            build_result(self.inputs, {**base, "parameters": {"lookback": 15}})["run_id"],
        )
        self.assertNotEqual(
            first["run_id"],
            build_result(self.inputs, {**base, "pair": "GLDRUBF/GLZ6"})["run_id"],
        )

    def test_pair_with_a_thin_day_is_excluded_and_quantity_scales_margin(self):
        path = self.inputs.candles("GLDRUBF/GLZ6", "GLZ6")
        payload = json.loads(path.read_text())
        payload["rows"][7]["volume"] = 10
        path.write_text(json.dumps(payload))
        entry = next(p for p in catalog(self.inputs)["pairs"] if p["id"] == "GLDRUBF/GLZ6")
        self.assertEqual(entry["status"], "excluded_thin_leg")
        self.assertEqual(entry["min_leg_daily_volume"], 10)
        self.assertFalse(any(entry["strategies"].values()))
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
        cache.mkdir(parents=True, exist_ok=True)
        for secid in ("CNYRUBF", "CRZ6"):
            (cache / f"{secid}-2025-06-17-2026-10-27-0.json").write_text("{}")
        result = build_result(
            self.inputs,
            {
                "pair": "CNYRUBF/CRZ6",
                "strategy": "funding_only",
                "parameters": {"lookback": 12},
            },
            fetch_fn=history,
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
            build_result(self.inputs, {"pair": "UNKNOWN/PAIR", "strategy": "mean_reversion"})

    def test_retired_s2_routes_answer_not_found(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ScreenerStore(self.inputs)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        origin = f"http://127.0.0.1:{server.server_port}"
        headers = {"Content-Type": "application/json", "Origin": origin}

        def call(method, path, body=None):
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())

        for path in ("/api/catalog", "/api/runs", "/api/searches"):
            self.assertEqual(call("GET", path), (404, {"error": "not found"}))
        for path in ("/api/run", "/api/screen", "/api/optimize"):
            self.assertEqual(call("POST", path, "{}"), (404, {"error": "not found"}))
        self.assertEqual(call("GET", "/api/timeframes")[0], 200)
        self.assertEqual(call("GET", "/api/settings")[0], 200)
        connection.close()

    def test_http_rejects_cross_origin_and_network_fallback(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ScreenerStore(self.inputs)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "POST",
            "/api/proof/run",
            "{}",
            {"Content-Type": "application/json", "Origin": "https://untrusted.example"},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        connection.close()
        with self.assertRaises(OSError):
            from moex_spread_lab.strategies.pair_screener.pair_screener import _offline_only

            _offline_only("https://iss.moex.com/")

    def test_web_modules_import_canonical_pair_screener_symbols(self):
        from moex_spread_lab.strategies.pair_screener import pair_screener
        from moex_spread_lab.web import dispatch, routes_proof, server, store

        web_src = Path(__file__).resolve().parents[1] / "src/moex_spread_lab/web"
        retired_refs = [
            path.name
            for path in sorted(web_src.rglob("*.py"))
            if "web_legacy" in path.read_text(encoding="utf-8")
            or "proof.screener_search" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(retired_refs, [])
        self.assertIs(dispatch.ScreenerError, pair_screener.ScreenerError)
        self.assertIs(routes_proof.ScreenerError, pair_screener.ScreenerError)
        self.assertIs(store.ScreenerInputs, pair_screener.ScreenerInputs)
        self.assertIs(server._pair_screener, pair_screener)
        for name in ("catalog", "build_result", "ScreenerInputs", "ScreenerError"):
            self.assertFalse(hasattr(server, name), name)

    def test_s2_compatibility_surface_is_retired(self):
        from moex_spread_lab.web.dispatch import ALL_POST_ROUTES

        self.assertEqual(ALL_POST_ROUTES & {"/api/run", "/api/screen", "/api/optimize"}, set())
        for module in ("moex_spread_lab.screener_search", "moex_spread_lab.proof.screener_search"):
            with self.assertRaises(ModuleNotFoundError):
                __import__(module)
        store_methods = {
            name
            for name in dir(ScreenerStore)
            if name in {"save", "list_runs", "list_searches", "save_screen", "save_search"}
        }
        self.assertEqual(store_methods, set())


if __name__ == "__main__":
    unittest.main()
