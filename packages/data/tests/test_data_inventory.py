import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.data.retention.data_inventory import DATA_INVENTORY_SCHEMA, build_data_inventory


class DataInventoryTests(unittest.TestCase):
    def _write_json(self, root: Path, relative: str, value: dict) -> Path:
        path = root / "data" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_inventory_classifies_inputs_caches_and_archival_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            h1 = self._write_json(root, "w1/candles/CNYRUBF_1h.json", {
                "schema_version": "iss-candles.v1", "timeframe": "1h", "from": "2026-01-01",
                "till": "2026-01-02", "row_count": 2, "rows": [],
            })
            self._write_json(root, "w1/candles/CNYRUBF_10m.json", {
                "schema_version": "iss-candles.v1", "timeframe": "10m", "rows": [],
            })
            self._write_json(root, "w1/candles-cache/CNYRUBF/60/page-000000.json", {"rows": []})
            self._write_json(root, "w1/candles-common/CNYRUBF_1m.json", {
                "schema_version": "iss-candles.v1", "timeframe": "1m", "rows": [],
            })
            self._write_json(root, "w1/current-intake.json", {"schema_version": "iss-intake.v1"})
            self._write_json(root, "analytics/cny-diagnostic.json", {"schema_version": "analytical-baseline.v1", "daily": []})
            unknown = root / "data" / "other.bin"
            unknown.write_bytes(b"unknown")

            h1_hash = hashlib.sha256(h1.read_bytes()).hexdigest()
            result = build_data_inventory(root, include_hashes=True)

        self.assertEqual(result["schema_version"], DATA_INVENTORY_SCHEMA)
        entries = {entry["path"]: entry for entry in result["files"]}
        self.assertEqual(entries["w1/candles/CNYRUBF_1h.json"]["classification"], "active_input")
        self.assertEqual(entries["w1/candles/CNYRUBF_1h.json"]["metadata"]["row_count"], 2)
        self.assertEqual(entries["w1/candles/CNYRUBF_1h.json"]["metadata"]["time_range"], {
            "from": "2026-01-01", "till": "2026-01-02",
        })
        self.assertEqual(entries["w1/candles/CNYRUBF_1h.json"]["sha256"], h1_hash)
        self.assertEqual(entries["w1/candles/CNYRUBF_10m.json"]["classification"], "archive_candidate")
        self.assertEqual(entries["w1/candles-cache/CNYRUBF/60/page-000000.json"]["classification"], "rebuildable_cache")
        self.assertEqual(entries["w1/candles-common/CNYRUBF_1m.json"]["classification"], "archive_candidate")
        self.assertEqual(entries["w1/current-intake.json"]["classification"], "active_input")
        self.assertEqual(entries["analytics/cny-diagnostic.json"]["classification"], "derived_archive_candidate")
        self.assertEqual(entries["other.bin"]["classification"], "unclassified")
        self.assertGreater(result["summary"]["candidate_reclaimable_bytes"], 0)
        self.assertTrue(result["summary"]["candidate_reclaimable_requires_review"])

    def test_fast_inventory_skips_json_inspection_and_missing_data_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = build_data_inventory(root)
            self.assertFalse(missing["summary"]["data_root_exists"])
            self._write_json(root, "w1/candles/CNYRUBF_1h.json", {
                "schema_version": "iss-candles.v1", "timeframe": "1h", "rows": [],
            })
            fast = build_data_inventory(root, inspect_json=False)
        entry = fast["files"][0]
        self.assertEqual(entry["classification"], "active_input")
        self.assertNotIn("metadata", entry)
        self.assertFalse(fast["json_inspection"])


if __name__ == "__main__":
    unittest.main()
