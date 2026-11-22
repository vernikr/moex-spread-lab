import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.data.retention.data_inventory import build_data_inventory
from moex_spread_lab.data.retention.data_retention import (
    DATA_ARCHIVE_SCHEMA,
    DataRetentionError,
    archive_rebuildable_caches,
    plan_rebuildable_cache_archive,
)


class DataRetentionTests(unittest.TestCase):
    def _write(self, root: Path, relative: str, body: bytes) -> Path:
        path = root / "data" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def _manifest(self, root: Path) -> Path:
        manifest = build_data_inventory(root, include_hashes=True)
        path = root / "inventory.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_plan_selects_only_verified_rebuildable_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write(root, "w1/candles-cache/CRZ6/60/page-000000.json", b'{"rows":[]}')
            self._write(root, "w1/candles/CNYRUBF_1h.json", b'{"timeframe":"1h","rows":[]}')
            manifest = self._manifest(root)
            archive = root / "archives" / "cache.tar.gz"
            plan = plan_rebuildable_cache_archive(root, manifest, archive)

        self.assertEqual(plan["schema_version"], DATA_ARCHIVE_SCHEMA)
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["file_count"], 1)
        self.assertEqual(plan["files"][0]["path"], "w1/candles-cache/CRZ6/60/page-000000.json")
        self.assertTrue(plan["requires_apply"])

    def test_archive_verifies_before_removing_cache_and_preserves_active_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self._write(root, "w1/candles-cache/CRZ6/60/page-000000.json", b'{"rows":[1]}')
            second = self._write(root, "w1/candleborders-cache/CRZ6.json", b'{"borders":[]}')
            active = self._write(
                root, "w1/candles/CNYRUBF_1h.json", b'{"timeframe":"1h","rows":[]}'
            )
            manifest = self._manifest(root)
            archive = root / "archives" / "verified-cache.tar.gz"
            result = archive_rebuildable_caches(root, manifest, archive)

            self.assertEqual(result["status"], "archived")
            self.assertEqual(result["deleted_file_count"], 2)
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())
            self.assertTrue(active.exists())
            self.assertTrue(archive.is_file())
            with tarfile.open(archive, "r:gz") as handle:
                self.assertIn("archive-manifest.json", handle.getnames())
                self.assertIn("data/w1/candles-cache/CRZ6/60/page-000000.json", handle.getnames())
                embedded = json.loads(handle.extractfile("archive-manifest.json").read())
            self.assertEqual(embedded["schema_version"], DATA_ARCHIVE_SCHEMA)
            self.assertEqual(len(embedded["files"]), 2)

    def test_symbolic_link_replacement_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._write(root, "w1/candles-cache/CRZ6/60/page-000000.json", b'{"rows":[]}')
            manifest = self._manifest(root)
            external = root / "external.json"
            external.write_bytes(b'{"rows":["external"]}')
            source.unlink()
            source.symlink_to(external)
            with self.assertRaises(DataRetentionError):
                plan_rebuildable_cache_archive(
                    root, manifest, root / "archives" / "link-cache.tar.gz"
                )
            self.assertTrue(source.is_symlink())
            self.assertTrue(external.exists())

    def test_stale_manifest_refuses_archive_without_deleting_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._write(root, "w1/candles-cache/CRZ6/60/page-000000.json", b'{"rows":[]}')
            manifest = self._manifest(root)
            source.write_bytes(b'{"rows":["changed"]}')
            archive = root / "archives" / "stale-cache.tar.gz"
            with self.assertRaises(DataRetentionError):
                archive_rebuildable_caches(root, manifest, archive)
            self.assertTrue(source.exists())
            self.assertFalse(archive.exists())


if __name__ == "__main__":
    unittest.main()
