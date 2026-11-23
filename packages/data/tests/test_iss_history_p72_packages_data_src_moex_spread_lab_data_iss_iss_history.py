"""Isolated regression tests for `data.iss.iss_history` (P7.2 leaf #55)."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from moex_spread_lab.data import iss as pkg_iss
from moex_spread_lab.data.iss import catalog as iss_catalog
from moex_spread_lab.data.iss.iss_history import (
    _HISTORY_JSON_CACHE,
    HISTORY_FIELDS,
    HISTORY_PAGE_SIZE,
    HISTORY_SCHEMA,
    IssHistoryError,
    _fetch_history_json,
    _history_rows,
    _history_url,
    fetch_history,
)


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


class _CustomHistoryError(IssHistoryError):
    pass


def _history_row(tradedate: str, close: float) -> list[Any]:
    return [
        "RFUD",
        tradedate,
        "CRZ6",
        close,
        close + 0.01,
        0.0,
        100,
        1000.0,
        10,
        close,
        5,
        "CNY",
    ]


class IssHistoryRegressionTests(unittest.TestCase):
    def test_facade_reexports_preserved(self) -> None:
        self.assertIs(iss_catalog.fetch_history, fetch_history)
        self.assertIs(iss_catalog.IssHistoryError, IssHistoryError)
        self.assertIs(iss_catalog._HISTORY_JSON_CACHE, _HISTORY_JSON_CACHE)
        self.assertIs(iss_catalog._fetch_history_json, _fetch_history_json)
        self.assertIs(iss_catalog._history_rows, _history_rows)
        self.assertIs(iss_catalog._history_url, _history_url)
        self.assertIs(pkg_iss._HISTORY_JSON_CACHE, _HISTORY_JSON_CACHE)
        self.assertIs(pkg_iss._history_url, _history_url)

    def test_history_rows_validation_and_filtering(self) -> None:
        with self.assertRaises(_CustomHistoryError):
            _history_rows(["not-a-dict"], "history", error=_CustomHistoryError)
        with self.assertRaises(IssHistoryError):
            _history_rows({"columns": ["A"], "data": "bad"}, "history")

        rows = _history_rows(
            {"columns": ["TRADEDATE", "CLOSE"], "data": [["2026-10-01", 12.1], "skip-me"]},
            "history",
        )
        self.assertEqual(rows, [{"TRADEDATE": "2026-10-01", "CLOSE": 12.1}])

    def test_fetch_history_json_errors(self) -> None:
        def fail_os(_url: str, timeout: float = 30.0) -> _FakeResponse:
            raise OSError("offline")

        with self.assertRaises(_CustomHistoryError):
            _fetch_history_json("https://example.test/x", opener=fail_os, error=_CustomHistoryError)

        with self.assertRaises(IssHistoryError):
            _fetch_history_json(
                "https://example.test/x",
                opener=lambda _url, timeout=30.0: _FakeResponse(b"not-json"),
            )

        with self.assertRaises(IssHistoryError):
            _fetch_history_json(
                "https://example.test/x",
                opener=lambda _url, timeout=30.0: _FakeResponse(b"[1, 2]"),
            )

    def test_fetch_history_pagination_dedup_and_cache_invalidation(self) -> None:
        calls: list[int] = []

        def opener(url: str, timeout: float = 30.0) -> _FakeResponse:
            start = int(parse_qs(urlparse(url).query)["start"][0])
            calls.append(start)
            if start == 0:
                data = [
                    _history_row(f"2026-01-{idx + 1:02d}", 12.0 + idx * 0.01)
                    for idx in range(HISTORY_PAGE_SIZE)
                ]
                # Duplicate date across pages should deduplicate to the later row.
                data[0] = _history_row("2026-01-01", 11.5)
            else:
                data = [_history_row("2026-01-01", 12.0), _history_row("2026-05-01", 13.0)]
            payload = {
                "history": {"columns": list(HISTORY_FIELDS), "data": data},
                "history.cursor": {"columns": ["INDEX", "TOTAL"], "data": [[start, 102]]},
            }
            return _FakeResponse(json.dumps(payload).encode("utf-8"))

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            result = fetch_history(
                "CRZ6",
                "2026-01-01",
                "2026-05-01",
                cache_dir=cache_dir,
                delay_seconds=0,
                opener=opener,
            )
            self.assertEqual(calls, [0, HISTORY_PAGE_SIZE])
            self.assertEqual(result["schema_version"], HISTORY_SCHEMA)
            self.assertEqual(result["page_count"], 2)
            self.assertEqual(result["row_count"], 101)
            self.assertEqual(result["rows"][0]["CLOSE"], 12.0)
            self.assertEqual(result["rows"][-1]["TRADEDATE"], "2026-05-01")

            # Second read hits disk/LRU cache without invoking opener.
            cached = fetch_history(
                "CRZ6",
                "2026-01-01",
                "2026-05-01",
                cache_dir=cache_dir,
                delay_seconds=0,
                opener=opener,
            )
            self.assertEqual(calls, [0, HISTORY_PAGE_SIZE])
            self.assertEqual(cached["rows"], result["rows"])

            # Modifying the cached file invalidates the mtime/size cache key.
            page0_path = cache_dir / "CRZ6-2026-01-01-2026-05-01-0.json"
            doc = json.loads(page0_path.read_text(encoding="utf-8"))
            doc["payload"]["history"]["data"][1][3] = 99.0
            time.sleep(0.02)
            page0_path.write_text(json.dumps(doc), encoding="utf-8")
            refreshed = fetch_history(
                "CRZ6",
                "2026-01-01",
                "2026-05-01",
                cache_dir=cache_dir,
                delay_seconds=0,
                opener=opener,
            )
            self.assertEqual(refreshed["rows"][1]["CLOSE"], 99.0)

    def test_fetch_history_validation_empty_and_incomplete(self) -> None:
        with self.assertRaises(TypeError):
            fetch_history("CRZ6", "2026-01-01", "2026-01-02", unexpected_opt=1)
        with self.assertRaises(_CustomHistoryError):
            fetch_history("", "2026-01-01", "2026-01-02", error=_CustomHistoryError)

        empty_body = json.dumps({"history": {"columns": list(HISTORY_FIELDS), "data": []}}).encode()
        with self.assertRaises(IssHistoryError):
            fetch_history(
                "CRZ6",
                "2026-01-01",
                "2026-01-02",
                delay_seconds=0,
                opener=lambda _url, timeout=30.0: _FakeResponse(empty_body),
            )

        allowed = fetch_history(
            "CRZ6",
            "2026-01-01",
            "2026-01-02",
            delay_seconds=0,
            allow_empty=True,
            opener=lambda _url, timeout=30.0: _FakeResponse(empty_body),
        )
        self.assertEqual(allowed["row_count"], 0)
        self.assertEqual(allowed["rows"], [])

        incomplete_body = json.dumps(
            {
                "history": {
                    "columns": list(HISTORY_FIELDS),
                    "data": [_history_row("2026-01-01", 12.0)],
                },
                "history.cursor": {"columns": ["INDEX", "TOTAL"], "data": [[0, 5]]},
            }
        ).encode()
        with self.assertRaises(_CustomHistoryError):
            fetch_history(
                "CRZ6",
                "2026-01-01",
                "2026-01-02",
                delay_seconds=0,
                error=_CustomHistoryError,
                opener=lambda _url, timeout=30.0: _FakeResponse(incomplete_body),
            )
