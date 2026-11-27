import importlib
import json
import tempfile
import unittest
from pathlib import Path

import pytest

from moex_spread_lab.data.lake.bar_types import DataLakeError
from moex_spread_lab.data.lake.parquet_store import ParquetBarStore


class DataLakeTests(unittest.TestCase):
    def _legacy(self, root: Path, name: str = "CNYRUBF_1h.json") -> Path:
        path = root / "data" / "w1" / "candles" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        sources = {
            letter: {
                "url": f"https://iss.moex.example/page-{index}",
                "received_at": f"2026-11-20T10:0{index}:00+00:00",
                "raw_response_sha256": letter * 64,
            }
            for index, letter in enumerate("ab", start=1)
        }
        rows = [
            {
                "begin": f"2026-01-01 1{index}:00:00",
                "end": f"2026-01-01 1{index}:59:59",
                "open": 10.0 + index * 0.5,
                "high": 11.0 + index * 0.5,
                "low": 9.5 + index * 0.5,
                "close": 10.5 + index * 0.5,
                "value": 12.0 + index * 2.0,
                "volume": None if index == 2 else 20 + index,
                "price_proxy": f"{10.5 + index * 0.5}",
                "price_proxy_kind": "candle_close",
                "price_proxy_assumption": "test_proxy",
                "source": sources["b" if index == 2 else "a"],
            }
            for index in (0, 1, 2)
        ]
        payload = {
            "schema_version": "iss-candles.v1",
            "secid": "CNYRUBF",
            "board": "RFUD",
            "interval": 60,
            "timeframe": "1h",
            "from": "2026-01-01",
            "till": "2026-01-02",
            "coverage_status": "available",
            "price_interpretation": "candle_price_proxy_for_medium_term_analysis",
            "rows": rows,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_import_preserves_rows_and_deduplicates_page_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = self._legacy(root)
            store = ParquetBarStore(root)
            record = store.import_legacy(legacy)
            verified = store.verify_legacy(legacy)
            series = store.read_series("CNYRUBF", "1h")
            projected = store.read_series("CNYRUBF", "1h", columns=("begin", "close"))

            self.assertEqual(record["dataset_id"], "1h/CNYRUBF")
            self.assertEqual(record["row_count"], 3)
            self.assertEqual(record["source_page_count"], 2)
            self.assertEqual(record["legacy_metadata"]["coverage_status"], "available")
            self.assertEqual(
                record["row_semantics"],
                {
                    "price_proxy_kind": "candle_close",
                    "price_proxy_assumption": "test_proxy",
                    "price_proxy_matches_close": True,
                },
            )
            self.assertTrue((root / record["bars_path"]).is_file())
            self.assertTrue((root / record["sources_path"]).is_file())
            self.assertEqual(verified["status"], "verified")
            self.assertEqual(
                [(row["begin"], row["close"]) for row in projected.rows],
                [
                    ("2026-01-01 10:00:00", 10.5),
                    ("2026-01-01 11:00:00", 11.0),
                    ("2026-01-01 12:00:00", 11.5),
                ],
            )
            self.assertEqual(
                [(row["volume"], row["source_page_id"]) for row in series.rows],
                [(20, 0), (21, 0), (None, 1)],
            )
            self.assertEqual(series.backend, "parquet")

    def test_manifest_integrity_is_checked_against_the_legacy_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = self._legacy(root)
            store = ParquetBarStore(root)
            record = store.import_legacy(legacy)
            manifest = json.loads(store.manifest_path.read_text())
            manifest["datasets"][0]["row_count"] += 1
            store.manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(DataLakeError, "row count"):
                ParquetBarStore(root).verify_legacy(legacy)
            manifest["datasets"][0]["row_count"] = record["row_count"]
            store.manifest_path.write_text(json.dumps(manifest))
            (root / record["sources_path"]).write_bytes(b"corrupt")
            with self.assertRaisesRegex(DataLakeError, "sources Parquet SHA-256"):
                ParquetBarStore(root).verify_legacy(legacy)

    def test_import_rejects_non_constant_price_semantics_and_fractional_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mutate in (
                lambda rows: rows[1].__setitem__("price_proxy_kind", "other_proxy"),
                lambda rows: rows[1].__setitem__("volume", 1.5),
                lambda rows: rows[0]["source"].__setitem__("raw_response_sha256", "not-a-hash"),
            ):
                legacy = self._legacy(root)
                payload = json.loads(legacy.read_text(encoding="utf-8"))
                mutate(payload["rows"])
                legacy.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(DataLakeError):
                    ParquetBarStore(root).import_legacy(legacy)

    def test_existing_dataset_requires_explicit_replace_and_verifier_detects_changed_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = self._legacy(root)
            store = ParquetBarStore(root)
            store.import_legacy(legacy)
            with self.assertRaises(DataLakeError):
                store.import_legacy(legacy)
            payload = json.loads(legacy.read_text(encoding="utf-8"))
            payload["rows"][0]["close"] = 999.0
            payload["rows"][0]["price_proxy"] = "999.0"
            legacy.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(DataLakeError):
                store.verify_legacy(legacy)
            replacement = store.import_legacy(legacy, replace=True)
            manifest = json.loads(
                (root / "data-lake" / "manifest.json").read_text(encoding="utf-8")
            )
            revisions = manifest["dataset_revisions"]
            self.assertEqual(revisions[-1]["dataset_id"], replacement["dataset_id"])
            self.assertNotEqual(
                revisions[-1]["record"]["legacy_sha256"], replacement["legacy_sha256"]
            )
            self.assertEqual(store.verify_legacy(legacy)["status"], "verified")


def test_data_root_shims_and_h1_archive_fail_to_import():
    """The deleted root data shims and the retired H1 archive command stay gone."""
    for module in (
        "benchmark",
        "data_archive_candidates",
        "data_inventory",
        "data_lake",
        "data_refresh",
        "data_retention",
        "h1_archive",
        "h1_migration",
        "h1_seal",
        "iss",
        "storage_policy",
    ):
        with pytest.raises(ImportError):
            importlib.import_module(f"moex_spread_lab.{module}")
    with pytest.raises(ImportError):
        importlib.import_module("moex_spread_lab.data.retention.h1_archive")


if __name__ == "__main__":
    unittest.main()
