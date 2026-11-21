import json
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.analytics import run_mean_reversion_baseline
from moex_spread_lab.ledger import build_ledger
from moex_spread_lab.run_artifacts import (
    RunArtifactError,
    gc_run_artifacts,
    load_run_artifact,
    pin_run_artifact,
    publish_baseline_artifact,
    verify_run_artifact,
    write_legacy_json,
)
from moex_spread_lab.validation import build_validation


def make_baseline():
    rows = []
    spreads = [0, 10, 20, 30, 20, 10, 0, -10, -20, -10, 0, 10, 20, 10, 0, -10, -20, -10, 0, 10]
    for index, spread in enumerate(spreads):
        rows.append({
            "date": f"2026-01-{index + 1:02d}",
            "timestamp": f"2026-01-{index + 1:02d} 00:00:00",
            "near_close": 10.0 + spread / 1000,
            "far_close": 10.0,
            "spread_ticks": spread,
            "benchmark_price": 1.0,
            "benchmark_source_kind": "observed",
        })
    return run_mean_reversion_baseline({
        "near": "NEAR",
        "far": "FAR",
        "timeframe": "1d",
        "contracts": {
            "NEAR": {"lot_size": 1000, "tick_size": 0.001, "tick_value": 1.0, "initial_margin": 900},
            "FAR": {"lot_size": 1000, "tick_size": 0.001, "tick_value": 1.0, "initial_margin": 1000},
        },
        "rows": rows,
        "provenance": {
            "near_candles_sha256": "near-evidence",
            "far_candles_sha256": "far-evidence",
        },
    }, lookback=3, entry_z=1.0, exit_z=0.5)


class RunArtifactTests(unittest.TestCase):
    def test_publish_separates_details_and_rehydrates_for_existing_consumers(self):
        baseline = make_baseline()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact_dir = root / "baseline-run"
            published = publish_baseline_artifact(artifact_dir, baseline, retention_days=14)
            summary = json.loads((artifact_dir / "summary.json").read_text(encoding="utf-8"))
            manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))

            self.assertNotIn("daily", summary)
            self.assertNotIn("trades", summary)
            self.assertTrue((artifact_dir / "daily.parquet").is_file())
            self.assertTrue((artifact_dir / "trades.parquet").is_file())
            self.assertEqual(published["artifact_id"], manifest["run_id"])
            self.assertEqual(manifest["input_hashes"]["near_candles_sha256"], "near-evidence")
            self.assertEqual(manifest["details"]["daily"]["row_count"], len(baseline["daily"]))
            self.assertEqual(load_run_artifact(artifact_dir)[0], baseline)

            ledger = build_ledger(artifact_dir)
            ledger_path = root / "ledger.json"
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            validation = build_validation(artifact_dir, ledger_path, [artifact_dir])
            self.assertEqual(ledger["summary"]["final_X"], baseline["summary"]["final_X"])
            self.assertEqual(validation["sensitivity"][0]["trade_count"], baseline["summary"]["trade_count"])

            legacy_path = root / "legacy.json"
            write_legacy_json(legacy_path, baseline)
            self.assertEqual(json.loads(legacy_path.read_text(encoding="utf-8")), baseline)

    def test_hash_verification_rejects_tampered_detail_file(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory) / "baseline-run"
            publish_baseline_artifact(artifact_dir, make_baseline())
            with (artifact_dir / "daily.parquet").open("ab") as handle:
                handle.write(b"tampered")
            with self.assertRaises(RunArtifactError):
                verify_run_artifact(artifact_dir)

    def test_manifest_identity_rejects_joint_metadata_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory) / "baseline-run"
            publish_baseline_artifact(artifact_dir, make_baseline())
            manifest_path = artifact_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["details"]["daily"]["sha256"] = "tampered-manifest-hash"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(RunArtifactError):
                verify_run_artifact(artifact_dir)

    def test_retention_is_review_first_and_pins_protect_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runs"
            expired = root / "expired"
            pinned = root / "pinned"
            publish_baseline_artifact(expired, make_baseline(), retention_days=0)
            publish_baseline_artifact(pinned, make_baseline(), retention_days=0, pin=True)

            plan = gc_run_artifacts(root)
            self.assertEqual(plan["status"], "dry_run")
            self.assertEqual(plan["expired_artifact_count"], 1)
            self.assertEqual(len(plan["planned_detail_files"]), 2)
            self.assertTrue((expired / "daily.parquet").exists())
            self.assertTrue((pinned / "daily.parquet").exists())

            pin_run_artifact(expired, pinned=True)
            self.assertEqual(gc_run_artifacts(root)["expired_artifact_count"], 0)
            pin_run_artifact(expired, pinned=False)
            collected = gc_run_artifacts(root, dry_run=False)
            self.assertEqual(collected["status"], "collected")
            self.assertFalse((expired / "daily.parquet").exists())
            self.assertTrue((expired / "summary.json").exists())
            self.assertTrue((pinned / "daily.parquet").exists())
            self.assertEqual(verify_run_artifact(expired, require_details=False)["details"], {})


if __name__ == "__main__":
    unittest.main()
