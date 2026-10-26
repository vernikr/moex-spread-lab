import json
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.w0 import (
    W0Error,
    canonical_bytes,
    manifest_config_hash,
    normalize_fixture,
    scan_secrets,
)


ROOT = Path(__file__).resolve().parents[1]


class W0Tests(unittest.TestCase):
    def test_canonical_hash_is_independent_of_object_order(self):
        self.assertEqual(canonical_bytes({"b": 2, "a": 1}), canonical_bytes({"a": 1, "b": 2}))

    def test_manifest_hash_matches_pinned_manifest(self):
        manifest = json.loads((ROOT / "config/run-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["config_hash"], manifest_config_hash(manifest))

    def test_fixture_normalization_is_sorted(self):
        fixture = json.loads((ROOT / "fixtures/w0/replay.json").read_text(encoding="utf-8"))
        normalized = normalize_fixture(fixture)
        self.assertEqual([r["record_id"] for r in normalized["records"]], ["probe-001", "probe-002"])

    def test_secret_scan_detects_private_key_pattern(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = "-----BEGIN " + "RSA PRIVATE KEY-----\n"
            (root / "sample.txt").write_text(marker, encoding="utf-8")
            self.assertTrue(scan_secrets(root))

    def test_secret_scan_ignores_clean_fixture(self):
        self.assertEqual(scan_secrets(ROOT / "fixtures"), [])


if __name__ == "__main__":
    unittest.main()
