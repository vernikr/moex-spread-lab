"""Preview/proxy host opt-in for the loopback server (`MSL_ALLOWED_HOSTS`)."""

from __future__ import annotations

import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import moex_spread_lab.web.server as web_server
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs

PREVIEW = "8765-x.e2b.app"


class AllowedPreviewHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        store = web_server.ScreenerStore(ScreenerInputs(Path(self._tmp.name)))
        self.addCleanup(store.close)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_server.make_handler(store))
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)

    def _status(self, host: str, origin: str | None = None) -> int:
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            headers = {"Host": host}
            if origin is not None:
                headers["Origin"] = origin
            conn.request("GET", "/", headers=headers)
            return conn.getresponse().status
        finally:
            conn.close()

    def test_default_stays_loopback_only(self) -> None:
        self.assertEqual(self._status(f"127.0.0.1:{self.server.server_port}"), 200)
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
