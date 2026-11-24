from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from moex_spread_lab.data.iss.catalog import (
    analyze_candle_pair,
    fetch_candle_series,
    normalize_candle_borders,
    normalize_candles,
    normalize_iss_intake,
)
from moex_spread_lab.data.iss.iss_http import IssIntakeError, fetch_json_with_raw, rows


def payload():
    return {
        "securities": {
            "columns": [
                "SECID",
                "SHORTNAME",
                "ASSETCODE",
                "FRSTTRADE",
                "LOTVOLUME",
                "MINSTEP",
                "STEPPRICE",
                "INITIALMARGIN",
                "LASTTRADEDATE",
                "LASTDELDATE",
            ],
            "data": [
                [
                    "CNYRUBF",
                    "CNYRUBF",
                    "CNYRUBTOM",
                    "2022-04-26",
                    1000,
                    0.001,
                    1,
                    1000,
                    "2100-01-01",
                    "2100-01-01",
                ],
                [
                    "CRZ6",
                    "CNY-12.26",
                    "CNY",
                    "2026-06-01",
                    1000,
                    0.001,
                    1,
                    1200,
                    "2026-12-17",
                    "2026-12-17",
                ],
            ],
        },
        "marketdata": {
            "columns": [
                "SECID",
                "BOARDID",
                "BID",
                "OFFER",
                "LAST",
                "SETTLEPRICE",
                "UPDATETIME",
                "TIME",
                "SYSTIME",
                "TRADEDATE",
            ],
            "data": [
                [
                    "CNYRUBF",
                    "RFUD",
                    12.12,
                    12.121,
                    12.121,
                    12.125,
                    "09:00:00",
                    "08:59:00",
                    "2026-10-27 09:15:00",
                    "2026-10-27",
                ],
                [
                    "CRZ6",
                    "RFUD",
                    12.30,
                    12.302,
                    12.301,
                    12.306,
                    "09:00:00",
                    "08:59:00",
                    "2026-10-27 09:15:00",
                    "2026-10-27",
                ],
            ],
        },
    }


class IssIntakeTests(unittest.TestCase):
    def test_pair_check_reports_common_timestamps_and_ohlc_quality(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def series(secid, begins):
                return {
                    "schema_version": "iss-candles.v1",
                    "secid": secid,
                    "interval": 24,
                    "timeframe": "1d",
                    "rows": [
                        {
                            "begin": begin,
                            "open": 1,
                            "close": 1.1,
                            "high": 1.2,
                            "low": 0.9,
                            "price_proxy_kind": "candle_close",
                        }
                        for begin in begins
                    ],
                }

            near = root / "near.json"
            far = root / "far.json"
            near.write_text(
                json.dumps(series("CNYRUBF", ["2026-01-01", "2026-01-02"])), encoding="utf-8"
            )
            far.write_text(
                json.dumps(series("CRZ6", ["2026-01-02", "2026-01-03"])), encoding="utf-8"
            )
            result = analyze_candle_pair(near, far)
        self.assertEqual(result["synchronization"]["common_timestamp_count"], 1)
        self.assertEqual(result["near"]["invalid_ohlc_count"], 0)
        self.assertEqual(
            result["synchronization"]["pair_analytical_status"], "usable_common_proxy_rows"
        )

    def test_candle_rows_use_close_as_explicit_price_proxy(self):
        result = normalize_candles(
            {
                "candles": {
                    "columns": ["open", "close", "high", "low", "value", "volume", "begin", "end"],
                    "data": [
                        [
                            12.0,
                            12.1,
                            12.2,
                            11.9,
                            0,
                            10,
                            "2026-10-01 10:00:00",
                            "2026-10-01 10:09:59",
                        ]
                    ],
                },
            },
            secid="CRZ6",
            interval=10,
            url="https://iss.moex.com/test/candles",
            received_at="2026-10-27T06:00:00+00:00",
            raw_sha256="candle-hash",
        )
        self.assertEqual(result[0]["price_proxy"], 12.1)
        self.assertEqual(result[0]["price_proxy_kind"], "candle_close")

    def test_candle_pagination_fetches_pages_and_deduplicates(self):
        calls = []

        class Response:
            status = 200
            headers = {"Content-Type": "application/json"}

            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return self.body

        def opener(request, timeout):
            query = parse_qs(urlparse(request.full_url).query)
            start = int(query["start"][0])
            calls.append(start)
            rows = (
                []
                if start >= 500
                else [
                    [
                        12.0,
                        12.1,
                        12.2,
                        11.9,
                        0,
                        10,
                        f"2026-10-01 10:{index:02d}:00",
                        f"2026-10-01 10:{index:02d}:59",
                    ]
                    for index in range(500)
                ]
            )
            return Response(
                json.dumps(
                    {
                        "candles": {
                            "columns": [
                                "open",
                                "close",
                                "high",
                                "low",
                                "value",
                                "volume",
                                "begin",
                                "end",
                            ],
                            "data": rows,
                        },
                    }
                ).encode()
            )

        with tempfile.TemporaryDirectory() as directory:
            result = fetch_candle_series(
                secid="CRZ6",
                interval=10,
                from_date="2026-10-01",
                till_date="2026-10-02",
                cache_dir=Path(directory),
                delay_seconds=0,
                opener=opener,
            )
        self.assertEqual(calls, [0, 500])
        self.assertEqual(result["row_count"], 500)
        self.assertEqual(result["page_count"], 2)

    def test_candle_borders_select_smallest_available_interval(self):
        result = normalize_candle_borders(
            {
                "borders": {
                    "columns": ["begin", "end", "interval", "board_group_id"],
                    "data": [
                        ["2022-01-01", "2026-10-27", 24, 45],
                        ["2022-01-01", "2026-10-27", 10, 45],
                        ["2022-01-01", "2026-10-27", 1, 45],
                    ],
                },
            },
            secid="CNYRUBF",
            url="https://iss.moex.com/test/candleborders",
            received_at="2026-10-27T06:00:00+00:00",
            raw_sha256="borders-hash",
        )
        self.assertEqual(result["minimum_interval"], 1)
        self.assertEqual(result["minimum_timeframe"], "1m")
        self.assertEqual(
            [item["timeframe"] for item in result["available_intervals"]], ["1m", "10m", "1d"]
        )

    def test_current_snapshot_keeps_price_roles_separate(self):
        result = normalize_iss_intake(
            payload(),
            url="https://iss.moex.com/test",
            received_at="2026-10-27T06:00:00+00:00",
            raw_sha256="raw-hash",
        )
        near = next(item for item in result["instruments"] if item["secid"] == "CNYRUBF")
        self.assertEqual(near["settlement"], 12.125)
        self.assertEqual(near["last_trade"], 12.121)
        self.assertEqual(near["last_trade_close"], None)
        self.assertEqual(near["analytical_close"], None)
        self.assertEqual(near["live_mark"], None)
        self.assertEqual(result["admission_status"], "conditionally_admitted_analytical_only")

    def test_candidate_blockers_are_explicit(self):
        result = normalize_iss_intake(
            payload(),
            url="https://iss.moex.com/test",
            received_at="2026-10-27T06:00:00+00:00",
            raw_sha256="raw-hash",
        )
        reasons = result["candidate"]["reasons"]
        self.assertTrue(any("executable bid/ask" in reason for reason in reasons))
        self.assertEqual(
            result["source"]["rights_status"], "accepted_project_assumption_personal_use"
        )
        self.assertTrue(result["candidate"]["assumptions"])

    def test_normalized_output_is_json_serializable(self):
        result = normalize_iss_intake(
            payload(),
            url="https://iss.moex.com/test",
            received_at="2026-10-27T06:00:00+00:00",
            raw_sha256="raw-hash",
        )
        json.dumps(result)

    def test_crossed_quote_is_flagged_without_dropping_catalog(self):
        broken = payload()
        broken["marketdata"]["data"][0][2:4] = [12.13, 12.12]
        result = normalize_iss_intake(
            broken,
            url="https://iss.moex.com/test",
            received_at="2026-10-27T06:00:00+00:00",
            raw_sha256="raw-hash",
        )
        near = next(item for item in result["instruments"] if item["secid"] == "CNYRUBF")
        self.assertEqual(near["quote_quality"], "crossed_quote")
        self.assertEqual(result["instrument_count"], 2)


class _Response:
    """Minimal urllib-style response double for the ISS fetcher."""

    def __init__(self, body: bytes, *, status: int = 200, content_type: str = "application/json"):
        self._body = body
        self.status = status
        self.headers = {"Content-Type": content_type}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class IssHttpContractTests(unittest.TestCase):
    """Row and fetcher contract of ``data.iss.iss_http`` (folded from the P7.2 shard)."""

    def test_rows_maps_columns_to_dicts(self):
        block = {"columns": ["SECID", "BID"], "data": [["CNYRUBF", 12.5], ["CRZ6", None]]}
        self.assertEqual(
            rows(block, "marketdata"),
            [{"SECID": "CNYRUBF", "BID": 12.5}, {"SECID": "CRZ6", "BID": None}],
        )
        self.assertEqual(rows({"columns": ["SECID"], "data": []}, "securities"), [])

    def test_rows_rejects_malformed_blocks_and_row_width(self):
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
            with self.subTest(block=block), self.assertRaises(IssIntakeError) as ctx:
                rows(block, "securities")
            self.assertEqual(str(ctx.exception), "ISS block is malformed: securities")
        widths = [
            ({"columns": ["SECID"], "data": [["a", "b"]]}, "securities[0]"),
            ({"columns": ["SECID", "BID"], "data": [["a"]]}, "securities[0]"),
            ({"columns": ["SECID"], "data": [["a"], "row"]}, "securities[1]"),
            ({"columns": ["SECID"], "data": [["a"], ["b"], ["c", "d"]]}, "securities[2]"),
        ]
        for block, where in widths:
            with self.subTest(block=block), self.assertRaises(IssIntakeError) as ctx:
                rows(block, "securities")
            self.assertEqual(str(ctx.exception), f"ISS row has invalid width: {where}")

    def test_fetch_json_with_raw_returns_digest_and_raw_body(self):
        body = json.dumps({"securities": {"columns": ["SECID"], "data": [["CNYRUBF"]]}}).encode()
        calls = []

        def opener(request, timeout=None):
            calls.append((request, timeout))
            return _Response(body)

        payload, received_at, raw_sha256, raw = fetch_json_with_raw(
            "https://iss.moex.com/iss.json", timeout=3.5, opener=opener, probe="coverage"
        )
        self.assertEqual(payload, {"securities": {"columns": ["SECID"], "data": [["CNYRUBF"]]}})
        self.assertEqual(raw, body)
        self.assertEqual(raw_sha256, hashlib.sha256(body).hexdigest())
        self.assertIsInstance(received_at, str)
        self.assertEqual(calls[0][1], 3.5)
        self.assertEqual(calls[0][0].get_header("Accept"), "application/json")

    def test_fetch_json_with_raw_rejects_http_content_and_network_failures(self):
        def opener_returning(response):
            def opener(request, timeout=None):
                return response

            return opener

        def opener_raising(request, timeout=None):
            raise OSError("network down")

        cases = [
            (opener_returning(_Response(b"{}", status=500)), "ISS request returned HTTP 500"),
            (
                opener_returning(_Response(b"<html>denied</html>", content_type="text/html")),
                "ISS response is not JSON; possible access/error page",
            ),
            (opener_returning(_Response(b"not-json")), "ISS response is not valid UTF-8 JSON"),
            (opener_returning(_Response(b"[1, 2]")), "ISS JSON root is not an object"),
            (opener_raising, "ISS request failed: network down"),
        ]
        for opener, message in cases:
            with self.subTest(message=message), self.assertRaises(IssIntakeError) as ctx:
                fetch_json_with_raw("https://iss.moex.com/iss.json", timeout=3.5, opener=opener)
            self.assertEqual(str(ctx.exception), message)

    def test_url_builders_keep_the_documented_query_contract(self):
        from moex_spread_lab.data.iss import iss_http

        self.assertEqual(
            iss_http.url("https://iss.moex.com/", "RFUD"),
            "https://iss.moex.com/iss/engines/futures/markets/forts/boards/RFUD/securities.json"
            "?iss.meta=off&iss.only=securities,marketdata"
            f"&securities.columns={','.join(iss_http.SECURITY_FIELDS)}"
            f"&marketdata.columns={','.join(iss_http.MARKETDATA_FIELDS)}",
        )
        self.assertEqual(
            iss_http.candles_url(
                "https://iss.moex.com", "RFUD", "CNYRUBF", 60, "2026-01-01", "2026-01-31", 500
            ),
            "https://iss.moex.com/iss/engines/futures/markets/forts/boards/RFUD"
            "/securities/CNYRUBF/candles.json?iss.meta=off&interval=60&from=2026-01-01"
            "&till=2026-01-31&start=500",
        )


if __name__ == "__main__":
    unittest.main()
