"""Regression coverage for the strict-typing refactor of the refresh merge helpers.

Guards the behavior-preserving extraction of typed payload/row helpers: legacy
payloads may carry non-object row entries, and the overlap window must stay
anchored to observed session dates rather than weekday arithmetic.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from moex_spread_lab.data.lake.refresh_merge import (
    DataRefreshError,
    RefreshTarget,
    _overlap_start,
    _payload_rows,
    _payload_watermark,
    _read_legacy_payload,
)


def _row(begin: str) -> dict:
    return {"begin": begin, "close": 100.0}


class RefreshMergeP72RegressionTests(unittest.TestCase):
    def test_payload_rows_ignores_non_object_entries(self):
        payload = {"rows": [_row("2026-01-05 00:00:00"), "junk", 7, _row("2026-01-06 00:00:00")]}
        self.assertEqual(
            [row["begin"] for row in _payload_rows(payload)],
            ["2026-01-05 00:00:00", "2026-01-06 00:00:00"],
        )

    def test_payload_rows_defaults_to_empty_when_missing(self):
        self.assertEqual(_payload_rows({}), [])

    def test_payload_watermark_uses_last_object_row(self):
        payload = {"rows": [_row("2026-01-05 00:00:00"), "junk", _row("2026-01-06 00:00:00")]}
        self.assertEqual(_payload_watermark(payload), date(2026, 1, 6))
        self.assertIsNone(_payload_watermark({"rows": ["junk"]}))
        self.assertIsNone(_payload_watermark(None))

    def test_overlap_start_uses_observed_sessions(self):
        payload = {"rows": [_row(f"2026-01-{day:02d} 00:00:00") for day in (5, 6, 7, 8)]}
        self.assertEqual(_overlap_start(payload, date(2026, 1, 8), 1), date(2026, 1, 7))

    def test_overlap_start_falls_back_to_two_days_per_session(self):
        self.assertEqual(_overlap_start(None, date(2026, 1, 8), 2), date(2026, 1, 4))

    def test_read_legacy_payload_rejects_non_object_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ABC_1d.json"
            path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
            with self.assertRaises(DataRefreshError):
                _read_legacy_payload(path, RefreshTarget("ABC", 24))


if __name__ == "__main__":
    unittest.main()
