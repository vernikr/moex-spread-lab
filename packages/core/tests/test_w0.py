import json
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.core.w0 import (
    W0Error,
    canonical_bytes,
    load_contracts,
    run_id,
    scan_secrets,
)

ROOT = Path(__file__).resolve().parents[3]


def _write_intake(directory: str, *, lot_size: object = 1, tick_size: object = 0.1) -> Path:
    path = Path(directory) / "intake.json"
    instruments = [
        {
            "secid": secid,
            "stable_id": secid.lower(),
            "board": "RFUD",
            "expiry": "2026-12-17",
            "lot_size": lot_size,
            "tick_size": tick_size,
            "tick_value": 1.0,
            "initial_margin": 100.0,
            "source_vintage": "current_snapshot",
        }
        for secid in ("NEAR", "FAR")
    ]
    path.write_text(json.dumps({"instruments": instruments}), encoding="utf-8")
    return path


class W0Tests(unittest.TestCase):
    def test_canonical_hash_is_independent_of_object_order(self):
        self.assertEqual(canonical_bytes({"b": 2, "a": 1}), canonical_bytes({"a": 1, "b": 2}))

    def test_run_id_is_stable_and_order_independent(self):
        identity = {"b": 2, "a": 1}
        self.assertEqual(run_id(identity), run_id({"a": 1, "b": 2}))
        self.assertEqual(len(run_id(identity)), 16)

    def test_run_id_rejects_non_finite_numbers(self):
        with self.assertRaises(ValueError):
            run_id({"value": float("nan")})
        with self.assertRaises(ValueError):
            run_id({"value": float("inf")})

    def test_load_contracts_accepts_valid_intake(self):
        with tempfile.TemporaryDirectory() as directory:
            selected, digest = load_contracts(_write_intake(directory), "NEAR", "FAR")
        self.assertEqual(selected["NEAR"]["lot_size"], 1)
        self.assertEqual(selected["FAR"]["tick_size"], 0.1)
        self.assertEqual(len(digest), 64)

    def test_load_contracts_rejects_bool_lot_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_intake(directory, lot_size=True)
            with self.assertRaises(W0Error) as ctx:
                load_contracts(path, "NEAR", "FAR")
            self.assertIn("must be a finite number", str(ctx.exception))

    def test_load_contracts_rejects_nan_tick_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_intake(directory, tick_size=float("nan"))
            with self.assertRaises(W0Error) as ctx:
                load_contracts(path, "NEAR", "FAR")
            self.assertIn("must be a finite number", str(ctx.exception))

    def test_secret_scan_detects_private_key_pattern(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = "-----BEGIN " + "RSA PRIVATE KEY-----\n"
            (root / "sample.txt").write_text(marker, encoding="utf-8")
            self.assertTrue(scan_secrets(root))

    def test_secret_scan_ignores_clean_config(self):
        self.assertEqual(scan_secrets(ROOT / "config"), [])

    def test_tracked_secret_scan_uses_git_index(self):
        self.assertEqual(scan_secrets(ROOT, tracked_only=True), [])


if __name__ == "__main__":
    unittest.main()
