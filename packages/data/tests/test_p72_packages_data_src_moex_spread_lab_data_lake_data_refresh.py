"""Isolated regression coverage for strict-typed ``data_refresh.py`` (P7.2 #59)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from moex_spread_lab.data.lake import data_refresh
from moex_spread_lab.data.lake.data_lake import ParquetBarStore
from moex_spread_lab.data.lake.data_refresh import (
    REFRESH_SCHEMA,
    DataRefreshError,
    RefreshTarget,
    refresh_candle_datasets,
)

ASSUMPTION = "user_A-06_candle_is_usable_medium_term_price_proxy"


def _row(day: int, close: float, raw_hash: str, *, begin: str | None = None) -> dict[str, Any]:
    stamp = f"2026-02-{day:02d}"
    return {
        "begin": begin if begin is not None else f"{stamp} 00:00:00",
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
            "url": f"https://iss.example/TEST/{day}",
            "received_at": "2026-11-20T10:00:00+00:00",
            "raw_response_sha256": raw_hash,
        },
    }


def _series(
    secid: str, rows: list[Any], *, schema_version: str = "iss-candles.v1"
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "secid": secid,
        "board": "RFUD",
        "interval": 24,
        "timeframe": "1d",
        "from": "2026-02-01",
        "till": "2026-02-05",
        "page_count": 2,
        "raw_row_count": len(rows),
        "row_count": len(rows),
        "duplicate_count": 0,
        "coverage_status": "available",
        "price_interpretation": "candle_price_proxy_for_medium_term_analysis",
        "rows": rows,
    }


class DataRefreshStrictRegressionTests(unittest.TestCase):
    def test_public_contract_exports_unchanged(self) -> None:
        self.assertEqual(REFRESH_SCHEMA, "market-data-refresh.v1")
        self.assertEqual(
            data_refresh.__all__,
            [
                "REFRESH_SCHEMA",
                "DataRefreshError",
                "RefreshTarget",
                "refresh_candle_datasets",
            ],
        )

    def test_option_and_argument_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = RefreshTarget("AAA", 24)

            with self.assertRaises(DataRefreshError):
                refresh_candle_datasets(root=root, targets=[], till_date="2026-02-05")

            with self.assertRaises(TypeError):
                refresh_candle_datasets(
                    root=root,
                    targets=[target],
                    till_date="2026-02-05",
                    unexpected_flag=True,
                )

            with self.assertRaises(DataRefreshError):
                refresh_candle_datasets(
                    root=root,
                    targets=[target],
                    till_date="not-a-date",
                    from_date="2026-02-01",
                )

            with self.assertRaises(DataRefreshError):
                refresh_candle_datasets(
                    root=root,
                    targets=[target],
                    from_date="2026-02-06",
                    till_date="2026-02-05",
                )

            with self.assertRaises(DataRefreshError):
                refresh_candle_datasets(
                    root=root,
                    targets=[target],
                    from_date="2026-02-01",
                    till_date="2026-02-05",
                    overlap_sessions=-1,
                )

            with self.assertRaises(DataRefreshError):
                refresh_candle_datasets(
                    root=root,
                    targets=[target],
                    from_date="2026-02-01",
                    till_date="2026-02-05",
                    raw_ttl_days=-1,
                )

            with self.assertRaises(DataRefreshError):
                refresh_candle_datasets(
                    root=root,
                    targets=[target],
                    from_date="2026-02-01",
                    till_date="2026-02-05",
                    chunk_days=0,
                    fetcher=lambda **_kwargs: _series("AAA", []),
                )

    def test_fetcher_payload_row_and_raw_hash_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = RefreshTarget("BAD", 24)
            raw = b'{"candles": "ok"}'
            raw_hash = hashlib.sha256(raw).hexdigest()

            with self.assertRaises(DataRefreshError):
                refresh_candle_datasets(
                    root=root,
                    targets=[target],
                    from_date="2026-02-01",
                    till_date="2026-02-01",
                    workers=1,
                    fetcher=lambda **_kwargs: _series("OTHER", [_row(1, 10.0, raw_hash)]),
                )

            with self.assertRaises(DataRefreshError):
                refresh_candle_datasets(
                    root=root,
                    targets=[target],
                    from_date="2026-02-01",
                    till_date="2026-02-01",
                    workers=1,
                    fetcher=lambda **_kwargs: _series(
                        "BAD", [_row(1, 10.0, raw_hash)], schema_version="wrong.v0"
                    ),
                )

            with self.assertRaises(DataRefreshError):
                refresh_candle_datasets(
                    root=root,
                    targets=[target],
                    from_date="2026-02-01",
                    till_date="2026-02-01",
                    workers=1,
                    fetcher=lambda **_kwargs: _series("BAD", [{"begin": 123}]),
                )

            with self.assertRaises(DataRefreshError):
                refresh_candle_datasets(
                    root=root,
                    targets=[target],
                    from_date="2026-02-01",
                    till_date="2026-02-01",
                    workers=1,
                    fetcher=lambda **_kwargs: _series("BAD", []),
                )

            def tampered_hash_fetcher(**kwargs: Any) -> dict[str, Any]:
                kwargs["raw_sink"](
                    "https://iss.example/BAD/1",
                    raw,
                    "2026-11-20T11:00:00+00:00",
                    "0" * 64,
                )
                return _series("BAD", [_row(1, 10.0, raw_hash)])

            with self.assertRaises(DataRefreshError):
                refresh_candle_datasets(
                    root=root,
                    targets=[target],
                    from_date="2026-02-01",
                    till_date="2026-02-01",
                    workers=1,
                    fetcher=tampered_hash_fetcher,
                )

    def test_mixed_skipped_and_refreshed_targets_with_dedup_and_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_old = b'{"old": true}'
            raw_old_hash = hashlib.sha256(raw_old).hexdigest()
            legacy_done = root / "custom_out/DONE_1d.json"
            legacy_done.parent.mkdir(parents=True)
            legacy_done.write_text(
                json.dumps(
                    _series("DONE", [_row(1, 10.0, raw_old_hash), _row(3, 12.0, raw_old_hash)])
                ),
                encoding="utf-8",
            )
            ParquetBarStore(root).import_legacy(legacy_done)

            raw_new = b'{"new": true}'
            raw_new_hash = hashlib.sha256(raw_new).hexdigest()
            seen_cache_dirs: list[Path | None] = []

            def fetcher(**kwargs: Any) -> dict[str, Any]:
                seen_cache_dirs.append(kwargs["cache_dir"])
                kwargs["raw_sink"](
                    f"https://iss.example/{kwargs['secid']}/{kwargs['till_date']}",
                    raw_new,
                    "2026-11-20T12:00:00+00:00",
                    raw_new_hash,
                )
                return _series(
                    kwargs["secid"],
                    [_row(2, 20.0, raw_new_hash), _row(3, 30.0, raw_new_hash)],
                )

            result = refresh_candle_datasets(
                root=root,
                targets=[
                    RefreshTarget("NEW", 24),
                    RefreshTarget("DONE", 24),
                    RefreshTarget("NEW", 24),
                ],
                from_date="2026-02-01",
                till_date="2026-02-03",
                output_dir=Path("custom_out"),
                cache_dir=Path("custom_cache"),
                pin_raw=True,
                raw_ttl_days=0,
                workers=2,
                fetcher=fetcher,
            )

            self.assertEqual(result["schema_version"], REFRESH_SCHEMA)
            self.assertEqual(result["status"], "refreshed")
            self.assertEqual(result["scheduled_chunk_count"], 1)
            self.assertEqual(seen_cache_dirs, [(root.resolve() / "custom_cache")])

            by_id = {item["dataset_id"]: item for item in result["datasets"]}
            self.assertEqual(by_id["1d/DONE"]["status"], "up_to_date")
            self.assertEqual(by_id["1d/NEW"]["status"], "refreshed")
            self.assertEqual(by_id["1d/NEW"]["watermark_before"], None)
            self.assertEqual(by_id["1d/NEW"]["watermark_after"], "2026-02-03")
            self.assertEqual(by_id["1d/NEW"]["network_requests"], 2)
            self.assertEqual(by_id["1d/NEW"]["raw_entry_count"], 1)
            self.assertEqual(by_id["1d/NEW"]["row_count"], 2)

            store = ParquetBarStore(root)
            expected_lock = (
                store.lake_root / "locks" / (hashlib.sha256(b"1d/NEW").hexdigest() + ".lock")
            )
            self.assertTrue(expected_lock.is_file())
            self.assertEqual(list((root / "custom_out").glob("*.tmp")), [])
            self.assertEqual(store.raw_archive.gc(dry_run=True)["planned_files"], [])
            self.assertEqual(store.verify_all()["dataset_count"], 2)


if __name__ == "__main__":
    unittest.main()
