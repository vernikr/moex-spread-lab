"""Regression coverage locking data.iss.iss_http behavior for P7.2 strict typing (#56).

The P7.2 pass must be behavior-preserving: these tests pin the public facade
aliases, the ISS row normalizer, and the W1 fetcher error contract so typing-only
edits cannot drift payloads, tuple shapes, or error messages.
"""

from __future__ import annotations

import hashlib
import json
import unittest

from moex_spread_lab.data.iss import iss_http
from moex_spread_lab.data.iss.iss_http import (
    IssIntakeError,
    _fetch_json,
    fetch_json_with_raw,
)


class _p72_packages_data_src_moex_spread_lab_data_iss_iss_http_response:
    """Minimal urllib-style response double for the W1 fetcher."""

    def __init__(self, body: bytes, *, status: int = 200, content_type: str = "application/json"):
        self._body = body
        self.status = status
        self.headers = {"Content-Type": content_type}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _p72_packages_data_src_moex_spread_lab_data_iss_iss_http_response:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class Test_p72_packages_data_src_moex_spread_lab_data_iss_iss_http(unittest.TestCase):
    def test_p72_packages_data_src_moex_spread_lab_data_iss_iss_http_rows_maps_columns_to_dicts(
        self,
    ):
        block = {"columns": ["SECID", "BID"], "data": [["CNYRUBF", 12.5], ["CRZ6", None]]}
        self.assertEqual(
            iss_http.rows(block, "marketdata"),
            [{"SECID": "CNYRUBF", "BID": 12.5}, {"SECID": "CRZ6", "BID": None}],
        )
        self.assertEqual(iss_http.rows({"columns": ["SECID"], "data": []}, "securities"), [])

    def test_p72_packages_data_src_moex_spread_lab_data_iss_iss_http_rows_rejects_malformed_blocks(
        self,
    ):
        malformed = [
            None,
            [],
            "block",
            3,
            {},
            {"columns": ["SECID"]},
            {"data": []},
            {"columns": "SECID", "data": []},
            {"columns": [], "data": "rows"},
        ]
        for block in malformed:
            with self.subTest(block=block):
                with self.assertRaises(IssIntakeError) as ctx:
                    iss_http.rows(block, "securities")
                self.assertEqual(str(ctx.exception), "ISS block is malformed: securities")

    def test_p72_packages_data_src_moex_spread_lab_data_iss_iss_http_rows_rejects_invalid_row_width(
        self,
    ):
        cases = [
            (
                {"columns": ["SECID"], "data": [["a", "b"]]},
                "ISS row has invalid width: securities[0]",
            ),
            (
                {"columns": ["SECID", "BID"], "data": [["a"]]},
                "ISS row has invalid width: securities[0]",
            ),
            (
                {"columns": ["SECID"], "data": [["a"], "row"]},
                "ISS row has invalid width: securities[1]",
            ),
            (
                {"columns": ["SECID"], "data": [["a"], ["b"], ["c", "d"]]},
                "ISS row has invalid width: securities[2]",
            ),
        ]
        for block, message in cases:
            with self.subTest(block=block):
                with self.assertRaises(IssIntakeError) as ctx:
                    iss_http.rows(block, "securities")
                self.assertEqual(str(ctx.exception), message)

    def test_p72_packages_data_src_moex_spread_lab_data_iss_iss_http_fetch_json_with_raw_returns_digest_and_raw(
        self,
    ):
        body = json.dumps({"securities": {"columns": ["SECID"], "data": [["CNYRUBF"]]}}).encode(
            "utf-8"
        )
        calls = []

        def opener(request, timeout=None):
            calls.append((request, timeout))
            return _p72_packages_data_src_moex_spread_lab_data_iss_iss_http_response(body)

        payload, received_at, raw_sha256, raw = fetch_json_with_raw(
            "https://iss.moex.com/iss.json", timeout=3.5, opener=opener, probe="coverage"
        )
        self.assertEqual(payload, {"securities": {"columns": ["SECID"], "data": [["CNYRUBF"]]}})
        self.assertEqual(raw, body)
        self.assertEqual(raw_sha256, hashlib.sha256(body).hexdigest())
        self.assertIsInstance(received_at, str)
        self.assertEqual(calls[0][1], 3.5)
        self.assertEqual(calls[0][0].get_header("Accept"), "application/json")

    def test_p72_packages_data_src_moex_spread_lab_data_iss_iss_http_fetch_json_with_raw_rejects_bad_payloads(
        self,
    ):
        def opener_returning(response):
            def opener(request, timeout=None):
                return response

            return opener

        def opener_raising(request, timeout=None):
            raise OSError("network down")

        cases = [
            (
                opener_returning(
                    _p72_packages_data_src_moex_spread_lab_data_iss_iss_http_response(
                        b"{}", status=500
                    )
                ),
                "ISS request returned HTTP 500",
            ),
            (
                opener_returning(
                    _p72_packages_data_src_moex_spread_lab_data_iss_iss_http_response(
                        b"<html>denied</html>", content_type="text/html"
                    )
                ),
                "ISS response is not JSON; possible access/error page",
            ),
            (
                opener_returning(
                    _p72_packages_data_src_moex_spread_lab_data_iss_iss_http_response(b"not-json")
                ),
                "ISS response is not valid UTF-8 JSON",
            ),
            (
                opener_returning(
                    _p72_packages_data_src_moex_spread_lab_data_iss_iss_http_response(b"[1, 2]")
                ),
                "ISS JSON root is not an object",
            ),
            (opener_raising, "ISS request failed: network down"),
        ]
        for opener, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(IssIntakeError) as ctx:
                    fetch_json_with_raw("https://iss.moex.com/iss.json", timeout=3.5, opener=opener)
                self.assertEqual(str(ctx.exception), message)

    def test_p72_packages_data_src_moex_spread_lab_data_iss_iss_http_fetch_json_return_raw_shapes(
        self,
    ):
        body = b'{"ok": true}'

        def opener(request, timeout=None):
            return _p72_packages_data_src_moex_spread_lab_data_iss_iss_http_response(body)

        three = _fetch_json("https://iss.moex.com/iss.json", timeout=1.0, opener=opener)
        self.assertEqual(len(three), 3)
        self.assertEqual(three[0], {"ok": True})
        self.assertEqual(three[2], hashlib.sha256(body).hexdigest())

        four = _fetch_json(
            "https://iss.moex.com/iss.json", timeout=1.0, opener=opener, return_raw=True
        )
        self.assertEqual(len(four), 4)
        self.assertEqual(four[3], body)

    def test_p72_packages_data_src_moex_spread_lab_data_iss_iss_http_facade_aliases_preserve_private_entry_points(
        self,
    ):
        self.assertIs(iss_http.rows, iss_http._rows)
        self.assertIs(iss_http.url, iss_http._url)
        self.assertIs(iss_http.candles_url, iss_http._candles_url)
        self.assertIs(iss_http.candleborders_url, iss_http._candleborders_url)
        self.assertIs(iss_http.fetch_json, iss_http._fetch_json)

    def test_p72_packages_data_src_moex_spread_lab_data_iss_iss_http_url_builders_are_unchanged(
        self,
    ):
        self.assertEqual(
            iss_http.url("https://iss.moex.com/", "RFUD"),
            "https://iss.moex.com/iss/engines/futures/markets/forts/boards/RFUD/securities.json"
            "?iss.meta=off&iss.only=securities,marketdata"
            f"&securities.columns={','.join(iss_http.SECURITY_FIELDS)}"
            f"&marketdata.columns={','.join(iss_http.MARKETDATA_FIELDS)}",
        )
        self.assertEqual(
            iss_http.candleborders_url("https://iss.moex.com", "RFUD", "CNYRUBF"),
            "https://iss.moex.com/iss/engines/futures/markets/forts/boards/RFUD"
            "/securities/CNYRUBF/candleborders.json?iss.meta=off",
        )
        self.assertEqual(
            iss_http.candles_url(
                "https://iss.moex.com", "RFUD", "CNYRUBF", 60, "2026-01-01", "2026-01-31", 500
            ),
            "https://iss.moex.com/iss/engines/futures/markets/forts/boards/RFUD"
            "/securities/CNYRUBF/candles.json?iss.meta=off&interval=60&from=2026-01-01"
            "&till=2026-01-31&start=500",
        )
