from __future__ import annotations

import hashlib
import http.client
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from moex_spread_lab.data.iss import catalog
from moex_spread_lab.data.iss.catalog import (
    _candidate_status,
    _quote_quality,
    analyze_candle_pair,
    fetch_candle_coverage,
    fetch_candle_series,
    normalize_candles,
    normalize_iss_intake,
    write_timeframe_inventory,
)
from moex_spread_lab.data.iss.iss_http import (
    CANDLE_INTERVAL_LABELS,
    IssHistoryError,
    IssIntakeError,
    candles_url,
    fetch_history_json,
    fetch_json_with_raw,
    is_safe_component,
    rows,
)
from moex_spread_lab.data.lake.refresh_merge import RefreshTarget


@pytest.mark.parametrize("field", ["secid", "board"])
@pytest.mark.parametrize("bad", ["../escape", "X/Y", "X?z=1", "", ".", "..", "X\\Y"])
def test_candle_intake_rejects_unsafe_segments_without_request_or_cache(tmp_path, field, bad):
    calls = []
    args = {"secid": "SAFE-1._", "board": "RFUD", field: bad}
    with pytest.raises(IssIntakeError, match=field):
        fetch_candle_series(
            **args,
            interval=60,
            from_date="2026-01-01",
            till_date="2026-01-02",
            cache_dir=tmp_path,
            opener=lambda *a, **k: calls.append(a),
        )
    assert not calls
    assert not list(tmp_path.rglob("*"))


@pytest.mark.parametrize("value", ["ABC", "Abc-1._", "_", "...", "12"])
def test_safe_candle_identifiers_are_accepted(value):
    assert RefreshTarget(value, 60).secid == value
    assert is_safe_component(value)


def test_candle_url_quotes_path_segments():
    url = candles_url("https://iss.example", "R/F", "X?z=1", 60, "2026-01-01", "2026-01-02", 0)
    assert urlparse(url).path.endswith("/boards/R%2FF/securities/X%3Fz%3D1/candles.json")
    assert parse_qs(urlparse(url).query)["interval"] == ["60"]


def test_candle_interval_labels_match_iss_durations():
    durations = block(
        "interval duration",
        [[1, 60], [10, 600], [60, 3600], [24, 86400], [7, 604800], [31, 2678400], [4, 8035200]],
    )
    labels_by_duration = {
        60: "1m",
        600: "10m",
        3600: "1h",
        86400: "1d",
        604800: "1w",
        2678400: "1mo",
        8035200: "1q",
    }
    expected = {
        item["interval"]: labels_by_duration[item["duration"]]
        for item in rows(durations, "durations")
    }
    assert expected == CANDLE_INTERVAL_LABELS
    for interval, label in expected.items():
        row = normalize_candles(
            candles([[1, 1, 1, 1, 1, 1, "2026-01-01", "2026-03-31"]]),
            secid="TEST",
            interval=interval,
            url="u",
            received_at="t",
            raw_sha256="h",
        )[0]
        assert row["timeframe"] == label
        assert RefreshTarget("TEST", interval).timeframe == label


def test_quarterly_cached_page_uses_current_timeframe(tmp_path):
    url = candles_url("https://iss.moex.com", "RFUD", "TEST", 4, "2026-01-01", "2026-03-31", 0)
    path = tmp_path / "TEST" / "4" / "page-000000.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"url": url, "rows": [{"begin": "2026-01-01", "timeframe": "4h"}]}))

    def no_network(*args, **kwargs):
        raise AssertionError("cache should be reused")

    result = fetch_candle_series(
        secid="TEST",
        interval=4,
        from_date="2026-01-01",
        till_date="2026-03-31",
        cache_dir=tmp_path,
        delay_seconds=0,
        opener=no_network,
    )
    assert result["timeframe"] == "1q"
    assert result["rows"][0]["timeframe"] == "1q"


@pytest.mark.parametrize("next_page", ["repeat", "overlap", "older", "advance"])
def test_candle_pages_must_advance_past_previous_page(next_page):
    def candle(index):
        stamp = f"2026-01-01 10:{index // 60:02d}:{index % 60:02d}"
        return [1, 1, 1, 1, 1, 1, stamp, stamp]

    first = [candle(index) for index in range(500)]
    pages = {
        "repeat": first,
        "overlap": [first[-1]],
        "older": [first[100]],
        "advance": [candle(500)],
    }
    calls = []

    def opener(request, timeout):
        start = int(parse_qs(urlparse(request.full_url).query)["start"][0])
        calls.append(start)
        if len(calls) > 2:
            raise AssertionError("repeated page was not rejected")
        return _Response(json.dumps(candles(first if start == 0 else pages[next_page])).encode())

    def fetch():
        return fetch_candle_series(
            secid="TEST",
            interval=1,
            from_date="2026-01-01",
            till_date="2026-01-02",
            opener=opener,
            delay_seconds=0,
            retries=0,
        )

    if next_page == "advance":
        assert fetch()["row_count"] == 501
    else:
        with pytest.raises(IssIntakeError, match="candle pagination did not advance"):
            fetch()
    assert calls == [0, 500]


def test_candle_incomplete_read_is_retried(monkeypatch):
    monkeypatch.setattr("moex_spread_lab.data.iss.iss_candles.time.sleep", lambda _: None)
    calls = []

    class Truncated(_Response):
        def read(self):
            raise http.client.IncompleteRead(b"{", 100)

    def opener(request, timeout):
        calls.append(1)
        return Truncated(b"") if len(calls) == 1 else _Response(json.dumps(candles([])).encode())

    result = fetch_candle_series(
        secid="TEST",
        interval=60,
        from_date="2026-01-01",
        till_date="2026-01-02",
        opener=opener,
        delay_seconds=0,
        retries=3,
    )
    assert len(calls) == 2
    assert result["row_count"] == 0
    calls.clear()

    def always_truncated(request, timeout):
        calls.append(1)
        return Truncated(b"")

    with pytest.raises(IssIntakeError, match="ISS request failed") as exc:
        fetch_candle_series(
            secid="TEST",
            interval=60,
            from_date="2026-01-01",
            till_date="2026-01-02",
            opener=always_truncated,
            delay_seconds=0,
            retries=3,
        )
    assert isinstance(exc.value.__cause__, http.client.IncompleteRead)
    assert len(calls) == 4


def test_history_incomplete_read_retries_and_exhaustion(monkeypatch):
    monkeypatch.setattr("moex_spread_lab.data.iss.iss_http.time.sleep", lambda _: None)
    calls = []

    class Truncated(_Response):
        def read(self):
            raise http.client.IncompleteRead(b"{", 100)

    def opener(request, timeout):
        calls.append(1)
        return Truncated(b"") if len(calls) == 1 else _Response(b"{}")

    assert fetch_history_json("https://example.test", opener=opener)[0] == {}
    assert len(calls) == 2
    calls.clear()

    def always_truncated(request, timeout):
        calls.append(1)
        return Truncated(b"")

    with pytest.raises(IssHistoryError, match="history request failed") as exc:
        fetch_history_json("https://example.test", opener=always_truncated)
    assert isinstance(exc.value.__cause__, http.client.IncompleteRead)
    assert len(calls) == 4


def block(columns, data):
    return {"columns": columns.split(), "data": data}


def payload():
    return {
        "securities": block(
            "SECID ASSETCODE LASTDELDATE",
            [["CNYRUBF", "CNYRUBTOM", "2100-01-01"], ["CRZ6", "CNY", "2026-12-17"]],
        ),
        "marketdata": block(
            "SECID BOARDID BID OFFER LAST SETTLEPRICE",
            [
                ["CNYRUBF", "RFUD", 12.12, 12.121, 12.121, 12.125],
                ["CRZ6", "RFUD", 12.30, 12.302, 12.301, 12.306],
            ],
        ),
    }


def snapshot(data):
    return normalize_iss_intake(
        data,
        url="https://iss.moex.com/test",
        received_at="2026-10-27T06:00:00+00:00",
        raw_sha256="raw-hash",
    )


def candles(data):
    return {"candles": block("open close high low value volume begin end", data)}


@pytest.mark.parametrize(
    ("bid", "ask", "expected"),
    [
        (0, 0, "missing_quote"),
        (71.1, 0, "missing_quote"),
        (0, 71.1, "missing_quote"),
        (None, 5, "missing_quote"),
        (5, -1, "missing_quote"),
        ("5", 6, "missing_quote"),
        (True, 6, "missing_quote"),
        (float("nan"), 6, "missing_quote"),
        (5, float("inf"), "missing_quote"),
        (5, 4, "crossed_quote"),
        (5, 5, "two_sided_quote"),
        (5, 6, "two_sided_quote"),
        (10**400, 10**401, "two_sided_quote"),
    ],
)
def test_quote_quality_requires_positive_finite_numeric_sides(bid, ask, expected):
    assert _quote_quality(bid, ask) == expected


def test_zero_quotes_are_missing_in_summary_and_block_candidate():
    data = payload()
    data["marketdata"]["data"][0][2:4] = [0, 0]
    result = snapshot(data)
    assert result["quote_quality_summary"]["missing_quote"] == 1
    assert result["candidate"]["status"] == "not_admitted"


@pytest.mark.parametrize(
    ("leg", "field", "value"),
    [
        ("CNYRUBF", "asset_code", "SBER"),
        ("CRZ6", "asset_code", "GAZR"),
        ("CNYRUBF", "source_vintage", "historical"),
        ("CRZ6", "source_vintage", "historical"),
        ("CNYRUBF", "quote_quality", "missing_quote"),
        ("CRZ6", "quote_quality", "crossed_quote"),
    ],
)
def test_candidate_rejects_invalid_pair(leg, field, value):
    instruments = snapshot(payload())["instruments"]
    next(item for item in instruments if item["secid"] == leg)[field] = value
    result = _candidate_status(instruments, "CNYRUBF", "CRZ6")
    assert result["status"] == "not_admitted"
    assert any("historical executable bid/ask" in reason for reason in result["reasons"])


def test_candidate_admission_preserves_caveat_and_rejects_missing_leg():
    instruments = snapshot(payload())["instruments"]
    accepted = _candidate_status(instruments, "CNYRUBF", "CRZ6")
    assert accepted["status"] == "conditionally_admitted_analytical_only"
    assert accepted["execution_status"] == "blocked_historical_quote_evidence"
    assert any("historical executable bid/ask" in reason for reason in accepted["reasons"])
    assert _candidate_status(instruments, "CNYRUBF", "MISSING")["status"] == "not_admitted"


def test_candidate_status_does_not_parse_reason_text(monkeypatch):
    instruments = snapshot(payload())["instruments"]
    monkeypatch.setattr(catalog, "_evaluate_candidate_pair", lambda *_: (["rewritten"], [], False))
    assert _candidate_status(instruments, "CNYRUBF", "CRZ6")["status"] == "not_admitted"
    monkeypatch.setattr(
        catalog, "_evaluate_candidate_pair", lambda *_: (["invalid or missing caveat"], [], True)
    )
    assert (
        _candidate_status(instruments, "CNYRUBF", "CRZ6")["status"]
        == "conditionally_admitted_analytical_only"
    )


class IssIntakeTests(unittest.TestCase):
    def test_retired_research_commands_fail_clearly(self):
        for function in (
            analyze_candle_pair,
            fetch_candle_coverage,
            write_timeframe_inventory,
        ):
            with (
                self.subTest(function=function.__name__),
                self.assertRaisesRegex(IssIntakeError, "has been retired"),
            ):
                function()

    def test_candle_rows_use_close_as_explicit_price_proxy(self):
        result = normalize_candles(
            candles(
                [[12.0, 12.1, 12.2, 11.9, 0, 10, "2026-10-01 10:00:00", "2026-10-01 10:09:59"]]
            ),
            secid="CRZ6",
            interval=10,
            url="https://iss.moex.com/test/candles",
            received_at="2026-10-27T06:00:00+00:00",
            raw_sha256="candle-hash",
        )
        self.assertEqual(result[0]["price_proxy"], 12.1)
        self.assertEqual(result[0]["price_proxy_kind"], "candle_close")

    def test_candle_invalid_numeric_values_are_rejected(self):
        for index in range(6):
            for value in ("bad", True, float("nan"), float("inf")):
                row = [1, 1, 1, 1, 1, 1, "begin", "end"]
                row[index] = value
                with (
                    self.subTest(index=index, value=value),
                    self.assertRaisesRegex(IssIntakeError, "invalid candle"),
                ):
                    normalize_candles(
                        candles([row]),
                        secid="TEST",
                        interval=1,
                        url="u",
                        received_at="t",
                        raw_sha256="h",
                    )

    def test_candle_pagination_fetches_pages_and_deduplicates(self):
        calls = []

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
            return _Response(json.dumps(candles(rows)).encode())

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

    def test_current_snapshot_keeps_price_roles_separate(self):
        result = snapshot(payload())
        near = next(item for item in result["instruments"] if item["secid"] == "CNYRUBF")
        self.assertEqual(near["settlement"], 12.125)
        self.assertEqual(near["last_trade"], 12.121)
        self.assertEqual(near["last_trade_close"], None)
        self.assertEqual(near["analytical_close"], None)
        self.assertEqual(near["live_mark"], None)
        self.assertEqual(result["admission_status"], "conditionally_admitted_analytical_only")

    def test_candidate_blockers_are_explicit(self):
        result = snapshot(payload())
        reasons = result["candidate"]["reasons"]
        self.assertTrue(any("executable bid/ask" in reason for reason in reasons))
        self.assertEqual(
            result["source"]["rights_status"], "accepted_project_assumption_personal_use"
        )
        self.assertTrue(result["candidate"]["assumptions"])

    def test_normalized_output_is_json_serializable(self):
        result = snapshot(payload())
        json.dumps(result)

    def test_crossed_quote_is_flagged_without_dropping_catalog(self):
        broken = payload()
        broken["marketdata"]["data"][0][2:4] = [12.13, 12.12]
        result = snapshot(broken)
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
    """Row and fetcher contracts for ISS HTTP."""

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


class IssHistoryContractTests(unittest.TestCase):
    def test_history_decoder_preserves_tolerant_rows(self):
        from moex_spread_lab.data.iss.catalog import IssHistoryError, _history_rows

        block = {
            "columns": ["TRADEDATE", "CLOSE"],
            "data": [["2026-01-01"], "bad", ["2026-01-02", 2, 3]],
        }
        self.assertEqual(
            _history_rows(block, "history"),
            [{"TRADEDATE": "2026-01-01"}, {"TRADEDATE": "2026-01-02", "CLOSE": 2}],
        )
        with self.assertRaisesRegex(IssHistoryError, "malformed ISS block"):
            _history_rows({}, "history")

    def test_history_pagination_cache_deduplication_and_empty(self):
        from moex_spread_lab.data.iss.catalog import fetch_history

        calls = []

        def opener(url, timeout):
            start = int(parse_qs(urlparse(url).query)["start"][0])
            calls.append(start)
            data = (
                [[f"2026-01-{i:03}", i] for i in range(100)]
                if start == 0
                else [["2026-01-099", 999], ["2026-02-01", 1]]
            )
            return _Response(
                json.dumps(
                    {
                        "history": {"columns": ["TRADEDATE", "CLOSE"], "data": data},
                        "history.cursor": {"columns": ["TOTAL"], "data": [[102]]},
                    }
                ).encode()
            )

        with tempfile.TemporaryDirectory() as directory:
            kwargs = {"cache_dir": Path(directory), "opener": opener, "delay_seconds": 0}
            first = fetch_history("TEST", "2026-01-01", "2026-02-01", **kwargs)
            second = fetch_history("TEST", "2026-01-01", "2026-02-01", **kwargs)
        self.assertEqual(calls, [0, 100])
        self.assertEqual(first, second)
        self.assertEqual(first["page_count"], 2)
        self.assertEqual(first["row_count"], 101)
        self.assertEqual(first["rows"][99]["CLOSE"], 999)

        def empty(url, timeout):
            return _Response(b'{"history":{"columns":["TRADEDATE"],"data":[]}}')

        self.assertEqual(
            fetch_history("TEST", "a", "b", opener=empty, delay_seconds=0, allow_empty=True)[
                "rows"
            ],
            [],
        )

    def test_history_errors_use_caller_error_type(self):
        from moex_spread_lab.core.w0 import W0Error
        from moex_spread_lab.data.iss.catalog import _fetch_history_json, fetch_history

        class CustomError(W0Error):
            pass

        def opener(url, timeout):
            return _Response(b"[]")

        with self.assertRaisesRegex(CustomError, "history response is not an object"):
            _fetch_history_json("test", opener=opener, error=CustomError)
        with self.assertRaisesRegex(CustomError, "invalid history arguments"):
            fetch_history("", "a", "b", error=CustomError)


if __name__ == "__main__":
    unittest.main()


def test_retired_data_compatibility_paths_are_absent():
    module = "moex_spread_lab.data.lake.data_lake"
    with pytest.raises(ModuleNotFoundError) as exc:
        importlib.import_module(module)
    assert exc.value.name == module
    for name in ("moex_spread_lab.data.iss", "moex_spread_lab.data.iss.catalog"):
        assert not hasattr(importlib.import_module(name), "_history_url")
