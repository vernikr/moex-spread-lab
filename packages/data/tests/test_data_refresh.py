import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from moex_spread_lab.data.lake.data_lake import ParquetBarStore, RawResponseArchive
from moex_spread_lab.data.lake.data_refresh import (
    DataRefreshError,
    RefreshTarget,
    refresh_candle_datasets,
)
from moex_spread_lab.data.lake.refresh_merge import (
    _overlap_start,
    _payload_rows,
    _payload_watermark,
    _read_legacy_payload,
)

ASSUMPTION = "user_A-06_candle_is_usable_medium_term_price_proxy"


def _row(day: int, close: float, raw_hash: str) -> dict:
    stamp = f"2026-01-{day:02d}"
    return {
        "begin": f"{stamp} 00:00:00",
        "end": f"{stamp} 23:59:59",
        "open": close - 0.1,
        "high": close + 0.2,
        "low": close - 0.2,
        "close": close,
        "value": close * 10,
        "volume": 100 + day,
        "price_proxy": close,
        "price_proxy_kind": "candle_close",
        "price_proxy_assumption": ASSUMPTION,
        "source": {
            "url": f"https://iss.example/ABC/{day}",
            "received_at": "2026-11-20T10:00:00+00:00",
            "raw_response_sha256": raw_hash,
        },
    }


def _payload(rows: list[dict]) -> dict:
    return {
        "schema_version": "iss-candles.v1",
        "secid": "ABC",
        "board": "RFUD",
        "interval": 24,
        "timeframe": "1d",
        "from": "2026-01-01",
        "till": "2026-01-02",
        "page_count": 1,
        "raw_row_count": len(rows),
        "row_count": len(rows),
        "duplicate_count": 0,
        "coverage_status": "available",
        "price_interpretation": "candle_price_proxy_for_medium_term_analysis",
        "rows": rows,
    }


class DataRefreshTests(unittest.TestCase):
    def test_raw_archive_round_trip_pin_and_gc_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = RawResponseArchive(Path(directory) / "data-lake")
            pinned = archive.archive(
                url="https://iss.example/pinned",
                raw=b'{"pinned": true}',
                received_at="2026-11-20T10:00:00+00:00",
                ttl_days=0,
                pin=True,
            )
            expired = archive.archive(
                url="https://iss.example/expired",
                raw=b'{"expired": true}',
                received_at="2026-11-20T10:00:00+00:00",
                ttl_days=0,
            )
            repeated = archive.archive(
                url="https://iss.example/expired",
                raw=b'{"expired": true}',
                received_at="2026-11-20T10:00:01+00:00",
                ttl_days=0,
            )
            self.assertNotEqual(expired["id"], repeated["id"])
            verified = archive.verify()
            self.assertEqual(verified["entry_count"], 3)
            plan = archive.gc(dry_run=True)
            self.assertIn(expired["path"], plan["planned_files"])
            self.assertIn(repeated["path"], plan["planned_files"])
            self.assertNotIn(pinned["path"], plan["planned_files"])
            self.assertTrue((archive.root / expired["path"]).is_file())
            collected = archive.gc(dry_run=False)
            self.assertEqual(collected["status"], "collected")
            self.assertFalse((archive.root / expired["path"]).exists())
            self.assertTrue((archive.root / pinned["path"]).is_file())
            self.assertEqual(archive.verify()["entry_count"], 1)

    def test_refresh_uses_overlap_upserts_idempotently_and_skips_when_current(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "data/w1/candles/ABC_1d.json"
            legacy.parent.mkdir(parents=True)
            hash_a = hashlib.sha256(b"a").hexdigest()
            hash_b = hashlib.sha256(b"b").hexdigest()
            legacy.write_text(
                json.dumps(_payload([_row(1, 10.0, hash_a), _row(2, 11.0, hash_b)])),
                encoding="utf-8",
            )
            ParquetBarStore(root).import_legacy(legacy)
            calls = []
            raw = b'{"candles": "delta"}'
            raw_hash = hashlib.sha256(raw).hexdigest()

            def fetcher(**kwargs):
                calls.append((kwargs["from_date"], kwargs["till_date"]))
                kwargs["raw_sink"](
                    "https://iss.example/ABC/delta", raw, "2026-11-20T11:00:00+00:00", raw_hash
                )
                return _payload([_row(2, 21.0, raw_hash), _row(3, 31.0, raw_hash)])

            result = refresh_candle_datasets(
                root=root,
                targets=[RefreshTarget("ABC", 24)],
                till_date="2026-01-03",
                overlap_sessions=1,
                workers=1,
                fetcher=fetcher,
            )
            self.assertEqual(result["status"], "refreshed")
            self.assertEqual(calls, [("2026-01-01", "2026-01-03")])
            refreshed = json.loads(legacy.read_text(encoding="utf-8"))
            self.assertEqual(
                [row["begin"] for row in refreshed["rows"]],
                [
                    "2026-01-01 00:00:00",
                    "2026-01-02 00:00:00",
                    "2026-01-03 00:00:00",
                ],
            )
            self.assertEqual(refreshed["rows"][1]["close"], 21.0)
            store = ParquetBarStore(root)
            self.assertEqual(store.verify_legacy(legacy)["row_count"], 3)
            self.assertEqual(store.raw_archive.verify()["entry_count"], 1)
            self.assertEqual(store.compact(force=True)["status"], "compacted")
            self.assertEqual(store.verify_all()["dataset_count"], 1)

            def should_not_fetch(**_kwargs):
                raise AssertionError("up-to-date refresh must not make a network request")

            second = refresh_candle_datasets(
                root=root,
                targets=[RefreshTarget("ABC", 24)],
                till_date="2026-01-03",
                workers=1,
                fetcher=should_not_fetch,
            )
            self.assertEqual(second["status"], "up_to_date")
            self.assertEqual(second["datasets"][0]["network_requests"], 0)

    def test_first_backfill_is_chunked_and_worker_bound_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            raw = b'{"candles": "chunk"}'
            raw_hash = hashlib.sha256(raw).hexdigest()

            def fetcher(**kwargs):
                calls.append((kwargs["from_date"], kwargs["till_date"]))
                day = int(kwargs["till_date"].rsplit("-", 1)[1])
                kwargs["raw_sink"](
                    f"https://iss.example/FIRST/{day}",
                    raw,
                    "2026-11-20T11:00:00+00:00",
                    raw_hash,
                )
                payload = _payload([_row(day, float(day), raw_hash)])
                payload["secid"] = "FIRST"
                return payload

            result = refresh_candle_datasets(
                root=root,
                targets=[RefreshTarget("FIRST", 24)],
                from_date="2026-01-01",
                till_date="2026-01-05",
                chunk_days=2,
                workers=2,
                fetcher=fetcher,
            )
            self.assertEqual(result["scheduled_chunk_count"], 3)
            self.assertEqual(
                sorted(calls),
                [
                    ("2026-01-01", "2026-01-02"),
                    ("2026-01-03", "2026-01-04"),
                    ("2026-01-05", "2026-01-05"),
                ],
            )
            with self.assertRaises(DataRefreshError):
                refresh_candle_datasets(
                    root=root,
                    targets=[RefreshTarget("FIRST", 24)],
                    till_date="2026-01-05",
                    workers=5,
                    fetcher=fetcher,
                )


class RefreshMergeHelperTests(unittest.TestCase):
    """Typed refresh-merge edge cases folded back from the P7.2 shard."""

    def test_payload_rows_and_watermark_ignore_non_object_entries(self):
        def row(begin: str) -> dict:
            return {"begin": begin, "close": 100.0}

        payload = {"rows": [row("2026-01-05 00:00:00"), "junk", 7, row("2026-01-06 00:00:00")]}
        self.assertEqual(
            [item["begin"] for item in _payload_rows(payload)],
            ["2026-01-05 00:00:00", "2026-01-06 00:00:00"],
        )
        self.assertEqual(_payload_rows({}), [])
        self.assertEqual(_payload_watermark(payload), date(2026, 1, 6))
        self.assertIsNone(_payload_watermark({"rows": ["junk"]}))
        self.assertIsNone(_payload_watermark(None))

    def test_overlap_start_uses_observed_sessions_then_the_two_day_fallback(self):
        rows = [{"begin": f"2026-01-{day:02d} 00:00:00", "close": 1.0} for day in (5, 6, 7, 8)]
        self.assertEqual(_overlap_start({"rows": rows}, date(2026, 1, 8), 1), date(2026, 1, 7))
        self.assertEqual(_overlap_start(None, date(2026, 1, 8), 2), date(2026, 1, 4))

    def test_read_legacy_payload_rejects_a_non_object_root(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ABC_1d.json"
            path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
            with self.assertRaises(DataRefreshError):
                _read_legacy_payload(path, RefreshTarget("ABC", 24))


if __name__ == "__main__":
    unittest.main()
