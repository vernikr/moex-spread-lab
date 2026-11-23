"""Regression coverage for the P7.2 strict slice of data/iss/iss_coverage.py (#54).

The slice is behavior-preserving: cached coverage payloads keep being relabeled on
load, instruments keep being sorted by ``stable_id``, failures keep being recorded
per instrument and the CSV inventory keeps its column contract.
"""

import csv
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar

from moex_spread_lab.data.iss.iss_coverage import (
    CSV_COLUMNS,
    fetch_candle_coverage,
    normalize_candle_borders,
    write_timeframe_inventory,
)

INSTRUMENTS: list[dict[str, Any]] = [
    {
        "stable_id": "futures:CRZ6",
        "secid": "CRZ6",
        "security": "CNY-12.26",
        "asset_code": "CNY",
        "expiry": "2026-12-17",
    },
    {
        "stable_id": "futures:CNYRUBF",
        "secid": "CNYRUBF",
        "security": "CNYRUBF",
        "asset_code": "CNYRUBTOM",
        "expiry": "2100-01-01",
    },
]


class _Response:
    status = 200
    headers: ClassVar[dict[str, str]] = {"Content-Type": "application/json"}

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _borders_body(*intervals: int) -> bytes:
    return json.dumps(
        {
            "borders": {
                "columns": ["begin", "end", "interval"],
                "data": [["2022-01-01", "2026-10-27", interval] for interval in intervals],
            }
        }
    ).encode()


def _borders_opener(calls: list[str], *intervals: int):
    def opener(request: Any, timeout: float) -> _Response:
        calls.append(request.full_url)
        return _Response(_borders_body(*intervals))

    return opener


class CandleCoverageRegressionTest(unittest.TestCase):
    def test_fetch_sorts_instruments_summarizes_and_reuses_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            calls: list[str] = []
            first = fetch_candle_coverage(
                INSTRUMENTS,
                cache_dir=cache_dir,
                delay_seconds=0,
                opener=_borders_opener(calls, 10, 60),
            )
            self.assertEqual(
                [item["stable_id"] for item in first["instruments"]],
                ["futures:CNYRUBF", "futures:CRZ6"],
            )
            self.assertEqual(first["instrument_count"], 2)
            self.assertEqual(first["coverage_status_counts"], {"available": 2})
            self.assertEqual(first["minimum_timeframe_counts"], {"10m": 2})
            self.assertEqual(len(calls), 2)
            self.assertEqual(
                sorted(path.name for path in cache_dir.iterdir()),
                ["CNYRUBF.json", "CRZ6.json"],
            )

            cached_calls: list[str] = []
            second = fetch_candle_coverage(
                INSTRUMENTS,
                cache_dir=cache_dir,
                delay_seconds=0,
                opener=_borders_opener(cached_calls, 24),
            )
            self.assertEqual(cached_calls, [])
            self.assertEqual(second["minimum_timeframe_counts"], {"10m": 2})

    def test_cached_payload_relabels_timeframes_on_load(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            cache_dir.mkdir(parents=True, exist_ok=True)
            url = (
                "https://iss.moex.com/iss/engines/futures/markets/forts/boards/RFUD"
                "/securities/CRZ6/candleborders.json?iss.meta=off"
            )
            (cache_dir / "CRZ6.json").write_text(
                json.dumps(
                    {
                        "secid": "CRZ6",
                        "source": {"url": url},
                        "available_intervals": [
                            {"interval": 60, "timeframe": "stale"},
                            {"interval": 10, "timeframe": "stale"},
                        ],
                        "minimum_interval": 60,
                        "minimum_timeframe": "stale",
                        "coverage_status": "available",
                    }
                ),
                encoding="utf-8",
            )
            calls: list[str] = []
            result = fetch_candle_coverage(
                [INSTRUMENTS[0]],
                cache_dir=cache_dir,
                delay_seconds=0,
                opener=_borders_opener(calls, 1),
            )
            self.assertEqual(calls, [])
            coverage = result["instruments"][0]["candle_coverage"]
            self.assertEqual(
                [row["timeframe"] for row in coverage["available_intervals"]], ["1h", "10m"]
            )
            self.assertEqual(coverage["minimum_timeframe"], "1h")

    def test_cache_from_another_instrument_or_url_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / "CRZ6.json").write_text(
                json.dumps({"secid": "CNYRUBF", "source": {"url": "https://example.test"}}),
                encoding="utf-8",
            )
            calls: list[str] = []
            result = fetch_candle_coverage(
                [INSTRUMENTS[0]],
                cache_dir=cache_dir,
                delay_seconds=0,
                opener=_borders_opener(calls, 1),
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(
                result["instruments"][0]["candle_coverage"]["coverage_status"], "available"
            )

    def test_unreadable_cache_falls_back_to_network(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / "CRZ6.json").write_text("[1, 2, 3]", encoding="utf-8")
            calls: list[str] = []
            result = fetch_candle_coverage(
                [INSTRUMENTS[0]],
                cache_dir=cache_dir,
                delay_seconds=0,
                opener=_borders_opener(calls, 1),
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(result["instruments"][0]["candle_coverage"]["minimum_timeframe"], "1m")

    def test_request_failure_is_recorded_without_dropping_instrument(self):
        def failing_opener(request: Any, timeout: float) -> _Response:
            raise OSError("connection reset")

        with tempfile.TemporaryDirectory() as directory:
            result = fetch_candle_coverage(
                INSTRUMENTS,
                cache_dir=Path(directory),
                delay_seconds=0,
                opener=failing_opener,
            )
        self.assertEqual(result["coverage_status_counts"], {"request_failed": 2})
        self.assertEqual(result["minimum_timeframe_counts"], {"unavailable": 2})
        failure = result["instruments"][0]["candle_coverage"]
        self.assertEqual(failure["available_intervals"], [])
        self.assertIsNone(failure["minimum_interval"])
        self.assertIn("connection reset", failure["error"])

    def test_option_validation_is_unchanged(self):
        with self.assertRaises(TypeError):
            fetch_candle_coverage([], unexpected=1)
        with self.assertRaises(Exception) as positive:
            fetch_candle_coverage([], workers=0)
        self.assertEqual(str(positive.exception), "coverage workers must be positive")
        with self.assertRaises(Exception) as negative:
            fetch_candle_coverage([], delay_seconds=-1)
        self.assertEqual(str(negative.exception), "coverage delay must not be negative")

    def test_normalize_rejects_non_positive_interval(self):
        with self.assertRaises(Exception) as raised:
            normalize_candle_borders(
                {"borders": {"columns": ["interval"], "data": [[0]]}},
                secid="CRZ6",
                url="https://iss.moex.com/test/candleborders",
                received_at="2026-10-27T06:00:00+00:00",
                raw_sha256="borders-hash",
            )
        self.assertIn("invalid candle interval for CRZ6: 0", str(raised.exception))

    def test_write_timeframe_inventory_emits_csv_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            result = fetch_candle_coverage(
                INSTRUMENTS,
                delay_seconds=0,
                opener=_borders_opener([], 10, 60),
            )
            json_path = Path(directory) / "inventory.json"
            csv_path = Path(directory) / "nested" / "inventory.csv"
            write_timeframe_inventory(result, json_path, csv_path)
            self.assertEqual(
                json.loads(json_path.read_text(encoding="utf-8"))["instruments"],
                result["instruments"],
            )
            with csv_path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(list(rows[0]), CSV_COLUMNS)
        self.assertEqual([row["stable_id"] for row in rows], ["futures:CNYRUBF", "futures:CRZ6"])
        self.assertEqual(rows[0]["available_timeframes"], "10m,1h")
        self.assertEqual(rows[0]["coverage_status"], "available")
        self.assertEqual(rows[0]["minimum_interval"], "10")


if __name__ == "__main__":
    unittest.main()
