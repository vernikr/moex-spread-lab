"""Layer 4 (`msl-web`) HTTP route, `/api/strategies` schema, and static asset tests."""

from __future__ import annotations

import builtins
import json
import math
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[3]
for _extra in (_REPO_ROOT / "packages/proof/tests", _REPO_ROOT / "tests"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

import moex_spread_lab.web.http_handler as http_handler  # noqa: E402
import moex_spread_lab.web.routes_proof as strategy_registry  # noqa: E402
import moex_spread_lab.web.server as web_server  # noqa: E402
from moex_spread_lab.core.contracts import (  # noqa: E402
    InstrumentContext,
    ParameterSpec,
    StrategyPlugin,
)
from moex_spread_lab.core.settings import save_settings  # noqa: E402
from moex_spread_lab.strategies import pair_screener  # noqa: E402
from moex_spread_lab.strategies.base import StrategyRegistry  # noqa: E402
from moex_spread_lab.strategies.calendar_zscore.plugin import CalendarZscorePlugin  # noqa: E402
from moex_spread_lab.strategies.pair_mean_reversion.plugin import (  # noqa: E402
    PairMeanReversionPlugin,
)
from moex_spread_lab.strategies.pair_screener import ScreenerInputs  # noqa: E402
from moex_spread_lab.web import dispatch  # noqa: E402
from proof_fixtures import two_calendar_windows  # noqa: E402

PREVIEW = "8765-x.e2b.app"


class WebPackageArchitectureTests(unittest.TestCase):
    def test_web_modules_import_canonical_symbols_and_retire_s2_surface(self) -> None:
        for name in ("fetch_history", "catalog", "build_result", "ScreenerInputs", "ScreenerError"):
            self.assertFalse(hasattr(web_server, name), name)
        web_src = Path(__file__).resolve().parents[1] / "src/moex_spread_lab/web"
        retired_refs = [
            path.name
            for path in sorted(web_src.rglob("*.py"))
            if "web_legacy" in path.read_text(encoding="utf-8")
            or "proof.screener_search" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(retired_refs, [])
        self.assertIs(dispatch.ScreenerError, pair_screener.ScreenerError)
        self.assertIs(strategy_registry.ScreenerError, pair_screener.ScreenerError)
        self.assertIs(web_server._pair_screener, pair_screener)
        self.assertEqual(
            dispatch.ALL_POST_ROUTES & {"/api/run", "/api/screen", "/api/optimize"}, set()
        )
        for name in ("screener_app", "screener_search", "proof.screener_search"):
            with self.assertRaises(ModuleNotFoundError):
                __import__(f"moex_spread_lab.{name}")
        retired_methods = {"save", "list_runs", "list_searches", "save_screen", "save_search"}
        self.assertEqual(set(dir(web_server.ScreenerStore)) & retired_methods, set())

    def test_vendored_lightweight_charts_and_apache_license_are_intact(self) -> None:
        vendor_dir = Path(web_server.__file__).resolve().parent / "vendor"
        bundle = vendor_dir / "lightweight-charts.standalone.production.js"
        self.assertTrue(bundle.is_file())
        self.assertGreater(bundle.stat().st_size, 100_000)
        self.assertIn(
            "Apache License",
            (vendor_dir / "LICENSE.lightweight-charts").read_text(encoding="utf-8"),
        )
        self.assertTrue((vendor_dir / "README.md").is_file())

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

    def test_unused_store_close_does_not_initialize_proof_service(self) -> None:
        store = web_server.ScreenerStore(ScreenerInputs(Path.cwd()))
        with mock.patch.object(
            web_server,
            "ProofService",
            side_effect=AssertionError("ProofService initialized during close"),
        ):
            store.close()

    def test_malformed_proof_search_history_does_not_block_new_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "data/analytics/proof-searches.jsonl"
            history.parent.mkdir(parents=True)
            history.write_text('not-json\n{}\n{"run_id":"old"}\n', encoding="utf-8")
            store = web_server.ScreenerStore(ScreenerInputs(root))
            try:
                store.save_proof_search({"run_id": "new", "result": "ok"})
            finally:
                store.close()
            records = [
                json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()[2:]
            ]
            self.assertEqual([record["run_id"] for record in records], ["old", "new"])

    def test_concurrent_first_proof_access_builds_one_service(self) -> None:
        """#348: parallel first requests must not orphan a second `ProofService`."""
        created: list[object] = []

        class FakeProof:
            calendar_data = object()

            def close(self) -> None:
                pass

        def fake_init(*_args: object, **_kwargs: object) -> FakeProof:
            time.sleep(0.05)
            proof = FakeProof()
            created.append(proof)
            return proof

        store = web_server.ScreenerStore(ScreenerInputs(Path.cwd()))
        with mock.patch.object(web_server, "ProofService", side_effect=fake_init):
            with ThreadPoolExecutor(max_workers=8) as pool:
                services = [pool.submit(lambda: store.proof) for _ in range(8)]
                resolved = [f.result() for f in services]
            store.close()
        self.assertEqual(len(created), 1)
        self.assertTrue(all(service is resolved[0] for service in resolved))

    def test_torn_proof_search_history_stays_writable(self) -> None:
        """#339: a half-written UTF-8 byte must not fail the save, and a torn line must not merge."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "data/analytics/proof-searches.jsonl"
            history.parent.mkdir(parents=True)
            history.write_bytes(b'{"run_id":"old"}\n\xd0')
            store = web_server.ScreenerStore(ScreenerInputs(root))
            try:
                store.save_proof_search({"run_id": "new"})
            finally:
                store.close()
            self.assertIn(b'{"run_id":"new"', history.read_bytes())

            history.write_text('{"run_id":"torn"', encoding="utf-8")
            store = web_server.ScreenerStore(ScreenerInputs(root))
            try:
                store.save_proof_search({"run_id": "fresh"})
            finally:
                store.close()
            lines = history.read_text(encoding="utf-8").splitlines()
            self.assertEqual(json.loads(lines[-1])["run_id"], "fresh")

    def test_proof_search_history_rejects_non_finite_numbers(self) -> None:
        """#339: history JSONL must stay strict JSON (no `NaN`/`Infinity` tokens)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "data/analytics/proof-searches.jsonl"
            store = web_server.ScreenerStore(ScreenerInputs(root))
            try:
                store.save_proof_search({"run_id": "nan", "value": math.nan, "nested": [math.inf]})
            finally:
                store.close()
            record = json.loads(history.read_text(encoding="utf-8"))
            self.assertIsNone(record["value"])
            self.assertEqual(record["nested"], [None])

    def test_proof_search_history_deduplicates_beyond_the_last_twenty_rows(self) -> None:
        """#361: `run_id` uniqueness is checked against the whole file, not a 20-row window."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "data/analytics/proof-searches.jsonl"
            store = web_server.ScreenerStore(ScreenerInputs(root))
            try:
                for i in range(25):
                    store.save_proof_search({"run_id": f"r{i}"})
                store.save_proof_search({"run_id": "r0"})
            finally:
                store.close()
            run_ids = [json.loads(line)["run_id"] for line in history.read_text().splitlines()]
            self.assertEqual(len(run_ids), 25)
            self.assertEqual(run_ids.count("r0"), 1)

    def test_json_transport_replaces_non_finite_numbers_with_null(self) -> None:
        assets: list[tuple[int, bytes, str]] = []
        handler = SimpleNamespace(
            send_asset=lambda status, body, content_type: assets.append(
                (status, body, content_type)
            ),
            wfile=BytesIO(),
        )
        http_handler.LoopbackHandler.send_json(cast(Any, handler), 200, {"value": math.nan})
        http_handler.LoopbackHandler.emit_ndjson(cast(Any, handler), {"value": math.inf})

        self.assertEqual(assets, [(200, b'{"value": null}', "application/json; charset=utf-8")])
        self.assertEqual(handler.wfile.getvalue(), b'{"value": null}\n')

    def test_json_media_type_is_case_insensitive(self) -> None:
        handler = SimpleNamespace(
            headers={"Content-Type": "Application/JSON; charset=utf-8", "Content-Length": "2"},
            rfile=BytesIO(b"{}"),
            drain_body=lambda _size: None,
        )
        self.assertEqual(dispatch._read_selection(handler, "/api/settings"), {})

    def test_unsupported_media_type_drains_declared_body(self) -> None:
        handler = SimpleNamespace(
            headers={"Content-Type": "text/plain", "Content-Length": "7"},
            rfile=BytesIO(b"payload"),
        )

        def drain_body(size: int) -> None:
            handler.rfile.read(size)

        handler.drain_body = drain_body
        with self.assertRaises(dispatch._UnsupportedMediaType):
            dispatch._read_selection(handler, "/api/settings")
        self.assertEqual(handler.rfile.read(), b"")

    def test_unknown_post_route_drains_declared_body_before_404(self) -> None:
        sent: list[tuple[int, dict[str, str]]] = []
        handler = SimpleNamespace(
            headers={"Content-Length": "7"},
            rfile=BytesIO(b"payload"),
            send_json=lambda status, value: sent.append((status, value)),
        )

        def drain_body(size: int) -> None:
            handler.rfile.read(size)

        handler.drain_body = drain_body
        dispatch.dispatch_post(handler, object(), "/api/retired")
        self.assertEqual(sent, [(404, {"error": "not found"})])
        self.assertEqual(handler.rfile.read(), b"")


class WebHttpRoutesTests(unittest.TestCase):
    _tmp: ClassVar[tempfile.TemporaryDirectory[str]]
    root: ClassVar[Path]
    store: ClassVar[web_server.ScreenerStore]
    server: ClassVar[ThreadingHTTPServer]
    port: ClassVar[int]

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        two_calendar_windows(cls.root)
        cls.store = web_server.ScreenerStore(ScreenerInputs(cls.root))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web_server.make_handler(cls.store))
        cls.port = cls.server.server_port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.store.close()
        cls._tmp.cleanup()

    def tearDown(self) -> None:
        analytics = self.root / "data/analytics"
        if analytics.is_dir():
            for item in analytics.glob("*"):
                if item.is_file():
                    item.unlink()

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
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, raw
        finally:
            conn.close()

    def _host_status(self, host: str, origin: str | None = None) -> int:
        hdrs = {"Host": host, **({"Origin": origin} if origin is not None else {})}
        return self._request("GET", "/", headers=hdrs)[0]

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

        status, headers, raw = self._request("GET", "/static/css/layout.css")
        self.assertEqual(status, 200)
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
        for absent in ("render-progress-run", "К скринеру пар"):
            self.assertNotIn(absent, ui_bundle)
        for present in (
            'aria-valuemin="0"',
            'aria-valuemax="100"',
            "apiStream",
            "progress-pill",
            'id="auto-tune"',
        ):
            self.assertIn(present, ui_bundle)

    def test_static_path_traversal_missing_assets_and_retired_routes(self) -> None:
        for bad_path in (
            "/static/../server.py",
            "/static/%2e%2e/server.py",
            "/static/js/../../server.py",
            "/static/js/%2e%2e/%2e%2e/routes_proof.py",
            "/static/",
            "/static/js/nonexistent.js",
            "/api/catalog",
            "/api/runs",
            "/api/searches",
        ):
            self.assertEqual(self._request("GET", bad_path)[0], 404, bad_path)
        for path in ("/api/run", "/api/screen", "/api/optimize"):
            self.assertEqual(self._request("POST", path, body={})[0], 404, path)
        for ok_path in ("/api/timeframes", "/api/settings"):
            self.assertEqual(self._request("GET", ok_path)[0], 200, ok_path)

    def test_serve_prints_a_connectable_url_when_bound_to_all_interfaces(self) -> None:
        """#360: `0.0.0.0` is a bind marker, not an address a browser can open."""
        printed: list[str] = []

        class StopServer(ThreadingHTTPServer):
            def serve_forever(self, poll_interval: float = 0.5) -> None:
                raise KeyboardInterrupt

        with (
            mock.patch.object(web_server, "ThreadingHTTPServer", StopServer),
            mock.patch.object(
                builtins, "print", lambda *a, **k: printed.append(" ".join(map(str, a)))
            ),
            mock.patch.dict("os.environ", {"MSL_ALLOWED_HOSTS": PREVIEW}),
            self.assertRaises(KeyboardInterrupt),
        ):
            web_server.serve(ScreenerInputs(Path.cwd()), port=0, host="0.0.0.0")
        banner = next(line for line in printed if "Local research screener" in line)
        self.assertNotIn("http://0.0.0.0", banner)
        self.assertIn("http://127.0.0.1:", banner)
        self.assertIn("all interfaces", banner)

    def test_spoofed_loopback_host_from_a_remote_peer_is_rejected(self) -> None:
        """#350: the `Host` header alone must not admit a non-loopback peer."""
        handler_cls = web_server.make_handler(web_server.ScreenerStore(ScreenerInputs(Path.cwd())))
        handler = object.__new__(handler_cls)
        handler.headers = {"Host": "127.0.0.1:8765"}
        handler.server = SimpleNamespace(server_port=8765)
        for peer, admitted in (
            (("127.0.0.1", 50000), True),
            (("::1", 50000), True),
            (("::ffff:127.0.0.1", 50000), True),
            (("203.0.113.7", 50000), False),
            (("127.0.0.1.attacker.example", 50000), False),
            ("not-an-address", False),
        ):
            handler.client_address = peer
            self.assertEqual(handler.local_request(), admitted, peer)
        handler.headers = {"Host": "preview.example"}
        handler.client_address = ("203.0.113.7", 50000)
        self.assertFalse(handler.local_request())
        with mock.patch.dict("os.environ", {"MSL_ALLOWED_HOSTS": "preview.example"}):
            self.assertTrue(handler.local_request())

    def test_allowed_preview_host_posts_over_plain_http(self) -> None:
        """#351: `MSL_ALLOWED_HOSTS` hosts must not be restricted to https by `local_origin`."""
        with mock.patch.dict("os.environ", {"MSL_ALLOWED_HOSTS": PREVIEW}):
            for status in (
                self._request(
                    "POST",
                    "/api/settings",
                    body={},
                    headers={"Host": PREVIEW, "Origin": f"http://{PREVIEW}"},
                )[0],
                self._request(
                    "POST",
                    "/api/settings",
                    body={},
                    headers={"Host": PREVIEW, "Origin": f"https://{PREVIEW}"},
                )[0],
            ):
                self.assertNotEqual(status, 403)
            self.assertEqual(
                self._request(
                    "POST",
                    "/api/settings",
                    body={},
                    headers={"Host": PREVIEW, "Origin": "http://evil.example"},
                )[0],
                403,
            )

    def test_loopback_host_origin_and_preview_allowlist_guards(self) -> None:
        for path in (
            "/",
            "/api/strategies",
            "/static/js/app.js",
            "/vendor/lightweight-charts.standalone.production.js",
        ):
            self.assertEqual(
                self._request("GET", path, headers={"Host": "evil.example:8899"})[0], 403, path
            )
            self.assertEqual(
                self._request("GET", path, headers={"Origin": "http://evil.example"})[0], 403, path
            )
        self.assertEqual(
            self._request(
                "POST", "/api/proof/run", body={}, headers={"Origin": "https://untrusted.example"}
            )[0],
            403,
        )
        self.assertEqual(self._host_status(f"127.0.0.1:{self.port}"), 200)
        self.assertEqual(
            self._host_status(f"LOCALHOST:{self.port}", f"HTTP://LOCALHOST:{self.port}"), 200
        )
        self.assertEqual(self._host_status(PREVIEW), 403)
        self.assertEqual(self._host_status(PREVIEW, f"https://{PREVIEW}"), 403)
        with mock.patch.dict("os.environ", {"MSL_ALLOWED_HOSTS": PREVIEW}):
            self.assertEqual(self._host_status(PREVIEW), 200)
            self.assertEqual(self._host_status(PREVIEW, f"https://{PREVIEW}"), 200)
            self.assertEqual(self._host_status(PREVIEW.upper(), f"HTTPS://{PREVIEW.upper()}"), 200)
            self.assertEqual(self._host_status("evil.example:8899"), 403)
            self.assertEqual(self._host_status(PREVIEW, "https://evil.example"), 403)
            self.assertEqual(
                self._request("GET", f"http://{PREVIEW}/api/dev/health", headers={"Host": PREVIEW})[
                    0
                ],
                403,
            )
            self.assertEqual(
                self._request(
                    "POST",
                    f"http://{PREVIEW}/api/dev/probe",
                    body={"action": "catalog"},
                    headers={"Host": PREVIEW},
                )[0],
                403,
            )
        with mock.patch.dict("os.environ", {"MSL_ALLOWED_HOSTS": "*"}):
            self.assertEqual(self._host_status(PREVIEW, f"https://{PREVIEW}"), 200)

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
        self.assertEqual(by_id["mean_reversion"]["ui_order"], ["entry_z", "exit_z", "lookback"])
        self.assertIn("pair_screen", by_id["mean_reversion"]["supported_universes"])
        self.assertTrue(by_id["mean_reversion"]["timeframe_supported"])

        cal_mr = by_id["zscore_mean_reversion"]
        self.assertEqual(cal_mr["ui_order"], ["entry_z", "exit_z", "window"])
        cal_1d = cal_mr["context_parameters"]["calendar_native"]["1d"]
        self.assertEqual(
            next(p for p in cal_1d["quarterly"] if p["name"] == "window")["default"], 20
        )
        self.assertEqual(next(p for p in cal_1d["monthly"] if p["name"] == "window")["default"], 7)

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
        with mock.patch.object(
            strategy_registry, "get_strategy_registry", return_value=StrategyRegistry(merged)
        ):
            status, _, raw = self._request("GET", "/api/strategies")
            self.assertEqual(status, 200)
            by_id = {entry["id"]: entry for entry in json.loads(raw.decode("utf-8"))["strategies"]}
            self.assertTrue(by_id["custom_pair_alpha"]["proof_enabled"])
            self.assertEqual(by_id["custom_pair_alpha"]["parameters"][0]["name"], "alpha_z")
            self.assertFalse(by_id["custom_cal_spread"]["is_builtin"])
            self.assertTrue(by_id["custom_cal_spread"]["proof_enabled"])
            cal_ctx = by_id["custom_cal_spread"]["context_parameters"]["calendar_native"]["1d"]
            self.assertEqual(cal_ctx["quarterly"][0]["default"], 2.2)
            self.assertEqual(cal_ctx["monthly"][0]["default"], 1.6)

    def test_unexpected_api_exceptions_return_json_or_streamed_errors(self) -> None:
        opt_req = {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion"}
        with mock.patch.object(dispatch, "handle_get_proof", side_effect=KeyError("catalog boom")):
            status, _, raw = self._request("GET", "/api/proof/catalog")
            self.assertEqual(status, 500)
            self.assertIn("KeyError", json.loads(raw)["error"])
        with mock.patch.object(strategy_registry, "research_run", side_effect=KeyError("run boom")):
            status, _, raw = self._request("POST", "/api/proof/run", body=opt_req)
            self.assertEqual(status, 500)
            self.assertIn("KeyError", json.loads(raw)["error"])
            status, hdrs, raw = self._request(
                "POST", "/api/proof/run", body=opt_req, headers={"X-Progress-Stream": "1"}
            )
            self.assertEqual(status, 200)
            self.assertIn("application/x-ndjson", hdrs["content-type"])
            self.assertEqual(
                [json.loads(line) for line in raw.decode("utf-8").splitlines() if line],
                [{"type": "error", "error": "KeyError: 'run boom'"}],
            )

    def test_proof_ndjson_streaming_search_lock_409_and_large_body_cap(self) -> None:
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
            self.assertEqual(
                self._request("POST", "/api/proof/optimize", body=large_confirm)[0], 409
            )
        status, _, raw = self._request("POST", "/api/proof/optimize", body=large_confirm)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["remaining_count"], 1)
        self.assertFalse((self.root / "data/analytics/proof-searches.jsonl").exists())

        opt_req = {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion"}
        save_settings(self.root, {"random_seed": 41})
        status, _, plain_raw = self._request("POST", "/api/proof/optimize", body=opt_req)
        self.assertEqual(status, 200)
        plain = json.loads(plain_raw)
        logs = (self.root / "data/analytics/proof-searches.jsonl").read_text().splitlines()
        self.assertEqual(len(logs), 1)
        self.assertEqual(json.loads(logs[0])["run_id"], plain["run_id"])

        status, hdrs, stream_raw = self._request(
            "POST", "/api/proof/optimize", body=opt_req, headers={"X-Progress-Stream": "1"}
        )
        self.assertEqual(status, 200)
        self.assertIn("application/x-ndjson", hdrs["content-type"])
        frames = [json.loads(line) for line in stream_raw.decode("utf-8").splitlines() if line]
        self.assertTrue(any(f["type"] == "progress" for f in frames))
        self.assertEqual(frames[-1], {"type": "result", "data": plain})

        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(
                "POST",
                "/api/proof/plateau",
                body=b'{"targets":[]}' + b" " * (193 * 1024),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 400)
        finally:
            conn.close()
        self.assertEqual(
            self._request(
                "POST",
                "/api/proof/windows",
                body={"requests": [opt_req]},
                headers={"Origin": "https://evil.example"},
            )[0],
            403,
        )


if __name__ == "__main__":
    unittest.main()
