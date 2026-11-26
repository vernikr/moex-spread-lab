"""Layer 4 (`msl-web`) HTTP route, `/api/strategies` schema, and static asset tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import cast
from unittest import mock

import moex_spread_lab.web.routes_proof as strategy_registry
import moex_spread_lab.web.server as web_server
from moex_spread_lab.core.contracts import InstrumentContext, ParameterSpec, StrategyPlugin
from moex_spread_lab.strategies.base import StrategyRegistry
from moex_spread_lab.strategies.calendar_zscore.plugin import CalendarZscorePlugin
from moex_spread_lab.strategies.pair_mean_reversion.plugin import PairMeanReversionPlugin
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs

PREVIEW = "8765-x.e2b.app"


class WebPackageArchitectureTests(unittest.TestCase):
    def test_server_does_not_export_legacy_fetch_history(self) -> None:
        for name in ("fetch_history", "catalog", "build_result", "ScreenerInputs", "ScreenerError"):
            self.assertFalse(hasattr(web_server, name), name)

    def test_vendored_lightweight_charts_and_apache_license_are_intact(self) -> None:
        vendor_dir = Path(web_server.__file__).resolve().parent / "vendor"
        bundle = vendor_dir / "lightweight-charts.standalone.production.js"
        license_file = vendor_dir / "LICENSE.lightweight-charts"
        readme_file = vendor_dir / "README.md"
        self.assertTrue(bundle.is_file())
        self.assertGreater(bundle.stat().st_size, 100_000)
        self.assertIn("Apache License", license_file.read_text(encoding="utf-8"))
        self.assertTrue(readme_file.is_file())

    def test_cli_screener_subcommand_discovered_via_msl_web_entry_point(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "moex_spread_lab.cli", "screener", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("usage: moex-spread-lab screener", proc.stdout)
        self.assertIn("--port", proc.stdout)
        self.assertIn("--root", proc.stdout)


class WebHttpRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = web_server.ScreenerStore(ScreenerInputs(self.root))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_server.make_handler(self.store))
        self.port = self.server.server_port
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.store.close()
        self._tmp.cleanup()

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            req_headers = dict(headers or {})
            payload = None
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                req_headers.setdefault("Content-Type", "application/json")
            conn.request(method, path, body=payload, headers=req_headers)
            resp = conn.getresponse()
            raw = resp.read()
            hdrs = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, hdrs, raw
        finally:
            conn.close()

    def test_static_html_css_js_and_csp_headers(self) -> None:
        for html_route in ("/", "/proof", "/static/index.html"):
            status, headers, raw = self._request("GET", html_route)
            self.assertEqual(status, 200, html_route)
            self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
            csp = headers["content-security-policy"]
            self.assertIn("script-src 'self'", csp)
            self.assertNotIn("'unsafe-inline'", csp.split("style-src")[0])
            self.assertIn("object-src 'none'", csp)
            self.assertIn("frame-ancestors 'none'", csp)
            text = raw.decode("utf-8")
            self.assertIn('<script type="module" src="/static/js/app.js"></script>', text)
            self.assertNotIn("<style>", text)

        for css_name in ("layout.css",):
            status, headers, raw = self._request("GET", f"/static/css/{css_name}")
            self.assertEqual(status, 200, css_name)
            self.assertEqual(headers["content-type"], "text/css; charset=utf-8")
            self.assertGreater(len(raw), 100)

        for js_name in (
            "app.js",
            "dom.js",
            "state.js",
            "normalizations.js",
            "charts.js",
            "ledger.js",
            "plateau-lamps.js",
            "strategy-form.js",
            "search-tuning.js",
            "picker.js",
            "settings-modal.js",
        ):
            status, headers, raw = self._request("GET", f"/static/js/{js_name}")
            self.assertEqual(status, 200, js_name)
            self.assertEqual(headers["content-type"], "application/javascript; charset=utf-8")
            self.assertLessEqual(len(raw.decode("utf-8").splitlines()), 600, js_name)

        status, headers, raw = self._request(
            "GET", "/vendor/lightweight-charts.standalone.production.js"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/javascript; charset=utf-8")
        self.assertGreater(len(raw), 100_000)

        static_dir = Path(web_server.__file__).resolve().parent / "static"
        ui_bundle = "\n".join(
            p.read_text(encoding="utf-8") for p in sorted(static_dir.rglob("*")) if p.is_file()
        )
        self.assertNotIn("render-progress-run", ui_bundle)
        self.assertIn('aria-valuemin="0"', ui_bundle)
        self.assertIn('aria-valuemax="100"', ui_bundle)
        self.assertIn("apiStream", ui_bundle)
        self.assertIn("progress-pill", ui_bundle)
        self.assertIn('id="auto-tune"', ui_bundle)
        self.assertNotIn("К скринеру пар", ui_bundle)

    def test_static_path_traversal_and_missing_assets_rejected(self) -> None:
        for bad_path in (
            "/static/../server.py",
            "/static/%2e%2e/server.py",
            "/static/js/../../server.py",
            "/static/js/%2e%2e/%2e%2e/routes_proof.py",
            "/static/",
            "/static/js/nonexistent.js",
        ):
            status, _, _ = self._request("GET", bad_path)
            self.assertEqual(status, 404, bad_path)

    def test_loopback_host_and_origin_guards_enforced_across_routes(self) -> None:
        for path in (
            "/",
            "/api/strategies",
            "/static/js/app.js",
            "/vendor/lightweight-charts.standalone.production.js",
        ):
            status, _, _ = self._request("GET", path, headers={"Host": "evil.example:8899"})
            self.assertEqual(status, 403, f"Host check failed on {path}")
            status, _, _ = self._request("GET", path, headers={"Origin": "http://evil.example"})
            self.assertEqual(status, 403, f"Origin check failed on {path}")

    def test_api_strategies_returns_canonical_registry_and_respects_timeframe(self) -> None:
        status, headers, raw = self._request("GET", "/api/strategies")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(payload["schema_version"], "strategy-registry.v1")
        self.assertEqual(payload["timeframe"], "1d")

        by_id = {entry["id"]: entry for entry in payload["strategies"]}
        self.assertTrue(
            {
                "mean_reversion",
                "funding_only",
                "zscore_mean_reversion",
                "convergence_to_ruonia",
            }.issubset(by_id)
        )

        mr = by_id["mean_reversion"]
        self.assertEqual(mr["ui_order"], ["entry_z", "exit_z", "lookback"])
        self.assertIn("pair_screen", mr["supported_universes"])
        self.assertTrue(mr["timeframe_supported"])

        cal_mr = by_id["zscore_mean_reversion"]
        self.assertEqual(cal_mr["ui_order"], ["entry_z", "exit_z", "window"])
        q_window = next(
            p
            for p in cal_mr["context_parameters"]["calendar_native"]["1d"]["quarterly"]
            if p["name"] == "window"
        )
        m_window = next(
            p
            for p in cal_mr["context_parameters"]["calendar_native"]["1d"]["monthly"]
            if p["name"] == "window"
        )
        self.assertEqual(q_window["default"], 20)
        self.assertEqual(m_window["default"], 7)

        # 1h timeframe: daily-only strategies report timeframe_supported == False
        status, _, _ = self._request(
            "POST",
            "/api/settings",
            body={
                "validation_percent": 30,
                "allow_failed_volume": False,
                "allow_failed_capital": False,
                "slippage_ticks": 1.0,
                "initial_capital_rub": 1_000_000.0,
                "timeframe": "1h",
                "price_fill": "next_open",
                "random_seed": 0,
            },
        )
        self.assertEqual(status, 200)
        status, _, raw_h1 = self._request("GET", "/api/strategies")
        self.assertEqual(status, 200)
        by_id_h1 = {
            entry["id"]: entry for entry in json.loads(raw_h1.decode("utf-8"))["strategies"]
        }
        self.assertTrue(by_id_h1["mean_reversion"]["timeframe_supported"])
        self.assertTrue(by_id_h1["zscore_mean_reversion"]["timeframe_supported"])
        self.assertFalse(by_id_h1["funding_only"]["timeframe_supported"])
        self.assertTrue(by_id_h1["convergence_to_ruonia"]["timeframe_supported"])

    def test_drop_in_pair_and_calendar_plugins_surface_in_api_strategies(self) -> None:
        class CustomPairPlugin(PairMeanReversionPlugin):
            strategy_id = "custom_pair_alpha"
            family_id = "custom_pair_alpha"
            label_ru = "Пользовательская парная альфа"
            proof_adapter = "pair_price_intents.v1"

            def parameter_schema(
                self, context: InstrumentContext | None = None
            ) -> tuple[ParameterSpec, ...]:
                return (
                    ParameterSpec(
                        name="alpha_z",
                        label_ru="Порог альфа, σ",
                        kind="float",
                        default=1.8,
                        min_value=0.8,
                        max_value=3.2,
                        step=0.1,
                    ),
                )

        class CustomCalendarPlugin(CalendarZscorePlugin):
            strategy_id = "custom_cal_spread"
            family_id = "custom_cal_spread"
            label_ru = "Пользовательский календарь"
            proof_adapter = "calendar_price_intents.v1"

            def parameter_schema(
                self, context: InstrumentContext | None = None
            ) -> tuple[ParameterSpec, ...]:
                monthly = bool(context and context.monthly)
                return (
                    ParameterSpec(
                        name="band_z",
                        label_ru="Ширина канала, σ",
                        kind="float",
                        default=2.2 if not monthly else 1.6,
                        min_value=1.0,
                        max_value=4.0,
                        step=0.2,
                    ),
                )

        merged = strategy_registry.get_strategy_registry().as_dict()
        merged["custom_pair_alpha"] = cast(StrategyPlugin, CustomPairPlugin())
        merged["custom_cal_spread"] = cast(StrategyPlugin, CustomCalendarPlugin())
        custom_registry = StrategyRegistry(merged)

        with mock.patch.object(
            strategy_registry, "get_strategy_registry", return_value=custom_registry
        ):
            status, _, raw = self._request("GET", "/api/strategies")
            self.assertEqual(status, 200)
            by_id = {entry["id"]: entry for entry in json.loads(raw.decode("utf-8"))["strategies"]}
            self.assertIn("custom_pair_alpha", by_id)
            self.assertTrue(by_id["custom_pair_alpha"]["proof_enabled"])
            self.assertEqual(by_id["custom_pair_alpha"]["parameters"][0]["name"], "alpha_z")

            self.assertIn("custom_cal_spread", by_id)
            self.assertFalse(by_id["custom_cal_spread"]["is_builtin"])
            self.assertTrue(by_id["custom_cal_spread"]["proof_enabled"])
            cal_ctx = by_id["custom_cal_spread"]["context_parameters"]["calendar_native"]["1d"]
            self.assertEqual(cal_ctx["quarterly"][0]["default"], 2.2)
            self.assertEqual(cal_ctx["monthly"][0]["default"], 1.6)

    def test_proof_ndjson_streaming_search_lock_409_and_large_body_cap(self) -> None:
        import sys as _sys

        for extra in (
            Path(__file__).resolve().parents[3] / "packages/proof/tests",
            Path(__file__).resolve().parents[3] / "tests",
        ):
            if str(extra) not in _sys.path:
                _sys.path.insert(0, str(extra))
        from proof_fixtures import two_calendar_windows

        two_calendar_windows(self.root)
        # 1. Busy search lock returns 409; confirmation-only preview is not persisted.
        large_confirm = {
            "universe": "pair_screen",
            "pair": "CNYRUBF/CRZ6",
            "strategy": "funding_only",
            "targets": [
                {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6"},
                *[
                    {"universe": "calendar_native", "asset": "SR", "pair": f"W{i}/T{i}"}
                    for i in range(200)
                ],
            ],
        }
        with self.store.proof_search_lock:
            status, _, _ = self._request("POST", "/api/proof/optimize", body=large_confirm)
            self.assertEqual(status, 409)
        status, _, raw = self._request("POST", "/api/proof/optimize", body=large_confirm)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["remaining_count"], 1)
        self.assertFalse((self.root / "data/analytics/proof-searches.jsonl").exists())

        # 2. Successful optimize persists to proof-searches.jsonl; NDJSON stream matches JSON.
        opt_req = {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion"}
        from moex_spread_lab.core.settings import save_settings

        save_settings(self.root, {"random_seed": 41})
        status, _, plain_raw = self._request("POST", "/api/proof/optimize", body=opt_req)
        self.assertEqual(status, 200)
        plain = json.loads(plain_raw)
        logs = (self.root / "data/analytics/proof-searches.jsonl").read_text().splitlines()
        self.assertEqual(len(logs), 1)
        self.assertEqual(json.loads(logs[0])["run_id"], plain["run_id"])

        status, hdrs, stream_raw = self._request(
            "POST",
            "/api/proof/optimize",
            body=opt_req,
            headers={"X-Progress-Stream": "1"},
        )
        self.assertEqual(status, 200)
        self.assertIn("application/x-ndjson", hdrs["content-type"])
        frames = [json.loads(line) for line in stream_raw.decode("utf-8").splitlines() if line]
        self.assertTrue(any(f["type"] == "progress" for f in frames))
        self.assertEqual(frames[-1]["type"], "result")
        self.assertEqual(frames[-1]["data"], plain)

        # 3. Large body cap (>192 KiB) returns 400; cross-origin POST returns 403.
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            oversized = b'{"targets":[]}' + b" " * (193 * 1024)
            conn.request(
                "POST",
                "/api/proof/plateau",
                body=oversized,
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 400)
        finally:
            conn.close()
        status, _, _ = self._request(
            "POST",
            "/api/proof/windows",
            body={"requests": [opt_req]},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)

    def _status(self, host: str, origin: str | None = None) -> int:
        headers = {"Host": host}
        if origin is not None:
            headers["Origin"] = origin
        status, _, _ = self._request("GET", "/", headers=headers)
        return status

    def test_default_stays_loopback_only(self) -> None:
        self.assertEqual(self._status(f"127.0.0.1:{self.port}"), 200)
        self.assertEqual(self._status(PREVIEW), 403)
        self.assertEqual(self._status(PREVIEW, f"https://{PREVIEW}"), 403)

    def test_exact_preview_host_is_admitted_with_its_origin(self) -> None:
        with mock.patch.dict("os.environ", {"MSL_ALLOWED_HOSTS": PREVIEW}):
            self.assertEqual(self._status(PREVIEW), 200)
            self.assertEqual(self._status(PREVIEW, f"https://{PREVIEW}"), 200)
            self.assertEqual(self._status("evil.example:8899"), 403)
            self.assertEqual(self._status(PREVIEW, "https://evil.example"), 403)

    def test_wildcard_is_an_explicit_opt_in(self) -> None:
        with mock.patch.dict("os.environ", {"MSL_ALLOWED_HOSTS": "*"}):
            self.assertEqual(self._status(PREVIEW, f"https://{PREVIEW}"), 200)

    def test_retired_s2_routes_answer_not_found(self) -> None:
        for path in ("/api/catalog", "/api/runs", "/api/searches"):
            status, _, _ = self._request("GET", path)
            self.assertEqual(status, 404)
        for path in ("/api/run", "/api/screen", "/api/optimize"):
            status, _, _ = self._request("POST", path, body={})
            self.assertEqual(status, 404)
        status, _, _ = self._request("GET", "/api/timeframes")
        self.assertEqual(status, 200)
        status, _, _ = self._request("GET", "/api/settings")
        self.assertEqual(status, 200)

    def test_http_rejects_cross_origin_proof_run(self) -> None:
        status, _, _ = self._request(
            "POST",
            "/api/proof/run",
            body={},
            headers={"Content-Type": "application/json", "Origin": "https://untrusted.example"},
        )
        self.assertEqual(status, 403)

    def test_web_modules_import_canonical_pair_screener_symbols(self) -> None:
        from moex_spread_lab.strategies.pair_screener import pair_screener
        from moex_spread_lab.web import dispatch, routes_proof, server

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
        self.assertIs(server._pair_screener, pair_screener)
        for name in ("catalog", "build_result", "ScreenerInputs", "ScreenerError"):
            self.assertFalse(hasattr(server, name), name)

    def test_s2_compatibility_surface_is_retired(self) -> None:
        from moex_spread_lab.web.dispatch import ALL_POST_ROUTES

        self.assertEqual(ALL_POST_ROUTES & {"/api/run", "/api/screen", "/api/optimize"}, set())
        for module in ("moex_spread_lab.screener_search", "moex_spread_lab.proof.screener_search"):
            with self.assertRaises(ModuleNotFoundError):
                __import__(module)
        store_methods = {
            name
            for name in dir(web_server.ScreenerStore)
            if name in {"save", "list_runs", "list_searches", "save_screen", "save_search"}
        }
        self.assertEqual(store_methods, set())


if __name__ == "__main__":
    unittest.main()
