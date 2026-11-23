"""Regression coverage for the typed manifest/plan boundary (issue #69)."""

from __future__ import annotations

import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Any

from moex_spread_lab.data.retention.data_inventory import DATA_INVENTORY_SCHEMA
from moex_spread_lab.data.retention.data_retention import (
    DataRetentionError,
    archive_rebuildable_caches,
    plan_rebuildable_cache_archive,
)

_SUFFIX = "p72_packages_data_src_moex_spread_lab_data_retention_data_retention"


def _sha256(body: bytes) -> str:
    import hashlib

    return hashlib.sha256(body).hexdigest()


class DataRetentionTypedBoundaryP72PackagesDataSrcMoexSpreadLabDataRetentionTests(
    unittest.TestCase
):
    """The JSON boundary keeps its validation order, messages and payload shape."""

    def _write_cache(self, root: Path, relative: str, body: bytes) -> Path:
        path = root / "data" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def _write_manifest(self, root: Path, payload: Any) -> Path:
        path = root / "inventory.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _manifest_payload(self, root: Path, files: list[Any]) -> dict[str, Any]:
        return {
            "schema_version": DATA_INVENTORY_SCHEMA,
            "root": str(root.resolve()),
            "hashes_included": True,
            "files": files,
        }

    def test_plan_payload_stays_a_plain_json_safe_dict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = b'{"rows":[]}'
            self._write_cache(root, "w1/candles-cache/CRZ6/60/page-000000.json", body)
            manifest = self._write_manifest(
                root,
                self._manifest_payload(
                    root,
                    [
                        {
                            "path": "w1/candles-cache/CRZ6/60/page-000000.json",
                            "sha256": _sha256(body),
                            "classification": "rebuildable_cache",
                            "logical_bytes": 11,
                            "allocated_bytes": 4096,
                            "dataset": "w1.candles-cache",
                        }
                    ],
                ),
            )
            plan = plan_rebuildable_cache_archive(root, manifest, root / "archives" / "a.tar.gz")

        self.assertEqual(type(plan), dict)
        self.assertEqual(plan["files"][0]["logical_bytes"], 11)
        self.assertEqual(plan["files"][0]["allocated_bytes"], 4096)
        self.assertEqual(plan["files"][0]["dataset"], "w1.candles-cache")
        self.assertEqual(json.loads(json.dumps(plan))["status"], "planned")

    def test_counters_fall_back_to_file_size_and_dataset_passes_through(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = b'{"rows":[1,2,3]}'
            self._write_cache(root, "w1/candles-cache/CRZ6/60/page-000000.json", body)
            manifest = self._write_manifest(
                root,
                self._manifest_payload(
                    root,
                    [
                        {
                            "path": "w1/candles-cache/CRZ6/60/page-000000.json",
                            "sha256": _sha256(body),
                            "classification": "rebuildable_cache",
                            "dataset": None,
                        }
                    ],
                ),
            )
            archive = root / "archives" / "defaults.tar.gz"
            result = archive_rebuildable_caches(root, manifest, archive)
            with tarfile.open(archive, "r:gz") as handle:
                member = handle.extractfile("archive-manifest.json")
                if member is None:
                    raise AssertionError("archive is missing its embedded manifest")
                embedded = json.loads(member.read())

        entry = embedded["files"][0]
        self.assertEqual(entry["logical_bytes"], len(body))
        self.assertEqual(entry["allocated_bytes"], len(body))
        self.assertIsNone(entry["dataset"])
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(result["status"], "archived")

    def test_non_object_manifest_keeps_schema_error_and_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._write_manifest(root, ["not", "an", "object"])
            with self.assertRaises(DataRetentionError) as caught:
                plan_rebuildable_cache_archive(root, manifest, root / "archives" / "a.tar.gz")
        self.assertEqual(str(caught.exception), "unsupported data inventory manifest schema")

    def test_missing_files_array_is_rejected_before_archive_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = self._manifest_payload(root, [])
            payload["files"] = "not-a-list"
            manifest = self._write_manifest(root, payload)
            with self.assertRaises(DataRetentionError) as caught:
                plan_rebuildable_cache_archive(root, manifest, root / "data" / "inside.tar.gz")
        self.assertEqual(str(caught.exception), "data inventory manifest files must be a list")

    def test_error_messages_for_invalid_root_and_hashes_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            without_hashes = self._manifest_payload(root, [])
            without_hashes["hashes_included"] = False
            manifest = self._write_manifest(root, without_hashes)
            with self.assertRaises(DataRetentionError) as caught:
                plan_rebuildable_cache_archive(root, manifest, root / "archives" / "a.tar.gz")
            self.assertEqual(
                str(caught.exception),
                "data inventory manifest must include SHA-256 hashes",
            )

            other_root = self._manifest_payload(root, [])
            other_root["root"] = str(root / "elsewhere")
            manifest = self._write_manifest(root, other_root)
            with self.assertRaises(DataRetentionError) as caught:
                plan_rebuildable_cache_archive(root, manifest, root / "archives" / "a.tar.gz")
            self.assertEqual(
                str(caught.exception),
                "data inventory manifest belongs to a different repository root",
            )

            missing_root = self._manifest_payload(root, [])
            del missing_root["root"]
            manifest = self._write_manifest(root, missing_root)
            with self.assertRaises(DataRetentionError) as caught:
                plan_rebuildable_cache_archive(root, manifest, root / "archives" / "a.tar.gz")
            self.assertEqual(str(caught.exception), "data inventory manifest has no valid root")


if __name__ == "__main__":
    unittest.main()
