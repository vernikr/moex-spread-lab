import json
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.data.lake.data_lake import DataLakeError, ParquetBarStore


class DataLakeTests(unittest.TestCase):
    def _legacy(self, root: Path, name: str = "CNYRUBF_1h.json") -> Path:
        path = root / "data" / "w1" / "candles" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        source_a = {
            "url": "https://iss.moex.example/page-0",
            "received_at": "2026-11-20T10:00:00+00:00",
            "raw_response_sha256": "a" * 64,
        }
        source_b = {
            "url": "https://iss.moex.example/page-1",
            "received_at": "2026-11-20T10:01:00+00:00",
            "raw_response_sha256": "b" * 64,
        }
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
            "rows": [
                {
                    "begin": "2026-01-01 10:00:00",
                    "end": "2026-01-01 10:59:59",
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.5,
                    "close": 10.5,
                    "value": 12.0,
                    "volume": 20,
                    "price_proxy": "10.5",
                    "price_proxy_kind": "candle_close",
                    "price_proxy_assumption": "test_proxy",
                    "source": source_a,
                },
                {
                    "begin": "2026-01-01 11:00:00",
                    "end": "2026-01-01 11:59:59",
                    "open": 10.5,
                    "high": 11.5,
                    "low": 10.0,
                    "close": 11.0,
                    "value": 14.0,
                    "volume": 21,
                    "price_proxy": "11.0",
                    "price_proxy_kind": "candle_close",
                    "price_proxy_assumption": "test_proxy",
                    "source": source_a,
                },
                {
                    "begin": "2026-01-01 12:00:00",
                    "end": "2026-01-01 12:59:59",
                    "open": 11.0,
                    "high": 12.0,
                    "low": 10.5,
                    "close": 11.5,
                    "value": 16.0,
                    "volume": None,
                    "price_proxy": "11.5",
                    "price_proxy_kind": "candle_close",
                    "price_proxy_assumption": "test_proxy",
                    "source": source_b,
                },
            ],
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
            full_table = store.read_bars("CNYRUBF", "1h")
            selected = store.read_bars(
                "CNYRUBF", "1h", columns=("begin", "close"), start="2026-01-01 11:00:00"
            )

            self.assertEqual(record["dataset_id"], "1h/CNYRUBF")
            self.assertEqual(str(full_table.schema.field("close").type), "double")
            self.assertEqual(str(full_table.schema.field("volume").type), "int64")
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
            self.assertFalse((root / "data-lake" / "catalog.duckdb").exists())
            self.assertEqual(verified["status"], "verified")
            self.assertEqual(
                selected.to_pylist(),
                [
                    {"begin": "2026-01-01 11:00:00", "close": 11.0},
                    {"begin": "2026-01-01 12:00:00", "close": 11.5},
                ],
            )

    def test_obsolete_catalog_is_ignored_but_manifest_integrity_is_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = self._legacy(root)
            store = ParquetBarStore(root)
            store.lake_root.mkdir()
            old_catalog = store.lake_root / "catalog.duckdb"
            old_catalog.write_bytes(b"obsolete catalog")
            record = store.import_legacy(legacy)
            self.assertEqual(store.verify_legacy(legacy)["status"], "verified")
            self.assertEqual(old_catalog.read_bytes(), b"obsolete catalog")
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
            legacy = self._legacy(root)
            payload = json.loads(legacy.read_text(encoding="utf-8"))
            payload["rows"][1]["price_proxy_kind"] = "other_proxy"
            legacy.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(DataLakeError):
                ParquetBarStore(root).import_legacy(legacy)

            legacy = self._legacy(root)
            payload = json.loads(legacy.read_text(encoding="utf-8"))
            payload["rows"][1]["volume"] = 1.5
            legacy.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(DataLakeError):
                ParquetBarStore(root).import_legacy(legacy)

            legacy = self._legacy(root)
            payload = json.loads(legacy.read_text(encoding="utf-8"))
            payload["rows"][0]["source"]["raw_response_sha256"] = "not-a-hash"
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


if __name__ == "__main__":
    unittest.main()
