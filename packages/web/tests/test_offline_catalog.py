"""The proof catalog resolves synthetic local inputs without live ISS requests."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
for extra in (_REPO_ROOT / "packages" / "proof" / "tests", _REPO_ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import moex_spread_lab.web.server as web_server  # noqa: E402
from moex_spread_lab.strategies.pair_screener import ScreenerInputs  # noqa: E402
from proof_fixtures import two_calendar_windows  # noqa: E402


class CatalogOfflineRouteTests(unittest.TestCase):
    def test_catalog_route_resolves_offline(self) -> None:
        saved = os.environ.get("MOEX_OFFLINE_ONLY")
        os.environ["MOEX_OFFLINE_ONLY"] = "1"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                two_calendar_windows(root)
                store = web_server.ScreenerStore(ScreenerInputs(root))
                try:
                    server = ThreadingHTTPServer(("127.0.0.1", 0), web_server.make_handler(store))
                    threading.Thread(target=server.serve_forever, daemon=True).start()
                    try:
                        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=30)
                        try:
                            conn.request("GET", "/api/proof/catalog")
                            resp = conn.getresponse()
                            self.assertEqual(resp.status, 200)
                            payload = json.loads(resp.read())
                            self.assertTrue(payload["assets"])
                        finally:
                            conn.close()
                    finally:
                        server.shutdown()
                        server.server_close()
                finally:
                    store.close()
        finally:
            if saved is None:
                os.environ.pop("MOEX_OFFLINE_ONLY", None)
            else:
                os.environ["MOEX_OFFLINE_ONLY"] = saved
