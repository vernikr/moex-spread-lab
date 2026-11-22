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
from unittest import mock

import moex_spread_lab.web.routes_proof as routes_proof
import moex_spread_lab.web.server as screener_app
import moex_spread_lab.web.server as web_server
from moex_spread_lab.core.contracts import InstrumentContext, ParameterSpec
from moex_spread_lab.strategies.base import StrategyRegistry
from moex_spread_lab.strategies.calendar_zscore.plugin import CalendarZscorePlugin
from moex_spread_lab.strategies.pair_mean_reversion.plugin import PairMeanReversionPlugin


class WebPackageArchitectureTests(unittest.TestCase):
    def test_screener_app_is_direct_module_alias_of_web_server(self) -> None:
        self.assertIs(screener_app, web_server)
        sentinel = object()
        with mock.patch.object(screener_app, "fetch_history", sentinel):
            self.assertIs(web_server.fetch_history, sentinel)

    def test_vendored_lightweight_charts_and_apache_license_are_intact(self) -> None:
        vendor_dir = Path(web_server.__file__).resolve().parent / "vendor"
        bundle = vendor_dir / "lightweight-charts.standalone.production.js"
        license_file = vendor_dir / "LICENSE.lightweight-charts"
        readme_file = vendor_dir / "README.md"
        self.assertTrue(bundle.is_file())
        self.assertGreater(bundle.stat().st_size, 100_000)
        self.assertTrue(license_file.is_file())
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
        self.store = web_server.ScreenerStore(web_server.ScreenerInputs(self.root))
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

        for css_name in ("layout.css", "picker.css", "charts.css", "ledger.css"):
            status, headers, raw = self._request("GET", f"/static/css/{css_name}")
            self.assertEqual(status, 200, css_name)
            self.assertEqual(headers["content-type"], "text/css; charset=utf-8")
            self.assertGreater(len(raw), 100)

        for js_name in (
            "app.js",
            "dom.js",
            "api.js",
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

        # Switch timeframe to 1h and confirm daily-only strategies report timeframe_supported == False
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

        merged = routes_proof.discover_strategies().as_dict()
        merged["custom_pair_alpha"] = CustomPairPlugin()
        merged["custom_cal_spread"] = CustomCalendarPlugin()
        custom_registry = StrategyRegistry(merged)

        with mock.patch.object(routes_proof, "discover_strategies", return_value=custom_registry):
            status, _, raw = self._request("GET", "/api/strategies")
            self.assertEqual(status, 200)
            by_id = {entry["id"]: entry for entry in json.loads(raw.decode("utf-8"))["strategies"]}
            self.assertIn("custom_pair_alpha", by_id)
            self.assertFalse(by_id["custom_pair_alpha"]["is_builtin"])
            self.assertTrue(by_id["custom_pair_alpha"]["proof_enabled"])
            self.assertEqual(by_id["custom_pair_alpha"]["parameters"][0]["name"], "alpha_z")

            self.assertIn("custom_cal_spread", by_id)
            self.assertFalse(by_id["custom_cal_spread"]["is_builtin"])
            self.assertTrue(by_id["custom_cal_spread"]["proof_enabled"])
            cal_ctx = by_id["custom_cal_spread"]["context_parameters"]["calendar_native"]["1d"]
            self.assertEqual(cal_ctx["quarterly"][0]["default"], 2.2)
            self.assertEqual(cal_ctx["monthly"][0]["default"], 1.6)


if __name__ == "__main__":
    unittest.main()
