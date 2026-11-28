import json
import os
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.core.w0 import (
    W0Error,
    canonical_bytes,
    load_contracts,
    run_id,
    scan_secrets,
    scan_secrets_detailed,
    write_json,
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

    def test_load_contracts_rejects_fractional_lot_size(self):
        for lot_size in (0.5, 1.9):
            with tempfile.TemporaryDirectory() as directory:
                path = _write_intake(directory, lot_size=lot_size)
                with self.assertRaises(W0Error) as ctx:
                    load_contracts(path, "NEAR", "FAR")
                self.assertIn("whole number", str(ctx.exception))

    def test_write_json_emits_strict_json_and_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.json"
            write_json(path, {"b": [1, 2], "a": "x"})
            raw = path.read_text(encoding="utf-8")
            self.assertEqual(json.loads(raw), {"b": [1, 2], "a": "x"})
            self.assertEqual(list(Path(directory).glob("*.json*")), [path])
            self.assertEqual(list(Path(directory).glob(".*.")), [])

    def test_write_json_rejects_non_finite_numbers(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "out.json"
                with self.assertRaises(ValueError):
                    write_json(path, {"x": bad})
                self.assertFalse(path.exists())

    def test_load_contracts_rejects_nan_tick_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_intake(directory, tick_size=float("nan"))
            with self.assertRaises(W0Error) as ctx:
                load_contracts(path, "NEAR", "FAR")
            # NaN is now refused at the JSON boundary itself (non-standard token).
            self.assertIn("non-standard JSON constant", str(ctx.exception))

    def test_load_contracts_rejects_null_source_vintage(self):
        for vintage in (None, "", "   ", 42):
            with tempfile.TemporaryDirectory() as directory:
                path = _write_intake(directory)
                payload = json.loads(path.read_text(encoding="utf-8"))
                for instrument in payload["instruments"]:
                    instrument["source_vintage"] = vintage
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(W0Error, "source_vintage"):
                    load_contracts(path, "NEAR", "FAR")

    def test_load_contracts_rejects_non_list_instruments(self):
        for bad in (None, "x", {"a": 1}, 42):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "intake.json"
                path.write_text(json.dumps({"instruments": bad}), encoding="utf-8")
                with self.assertRaisesRegex(W0Error, "must be a list"):
                    load_contracts(path, "NEAR", "FAR")

    def test_load_contracts_rejects_non_dict_rows_and_missing_secid(self):
        for bad_rows in ([["not", "a", "dict"]], [{"lot_size": 1}], [{"secid": "  "}]):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "intake.json"
                path.write_text(json.dumps({"instruments": bad_rows}), encoding="utf-8")
                with self.assertRaises(W0Error):
                    load_contracts(path, "NEAR", "FAR")

    def test_load_contracts_rejects_near_equals_far(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_intake(directory)
            with self.assertRaisesRegex(W0Error, "different contracts"):
                load_contracts(path, "NEAR", "NEAR")

    def test_load_contracts_rejects_duplicate_secid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_intake(directory)
            payload = json.loads(path.read_text(encoding="utf-8"))
            dupe = dict(payload["instruments"][0])
            dupe["initial_margin"] = 5.0
            payload["instruments"].append(dupe)
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(W0Error, "duplicate"):
                load_contracts(path, "NEAR", "FAR")

    def test_historical_only_fills_gaps_and_keeps_current_authoritative(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_intake(directory)  # initial_margin == 100.0 for both legs
            historical = {
                "instruments": [
                    {
                        "secid": "NEAR",
                        "lot_size": 1,
                        "tick_size": 0.1,
                        "tick_value": 1.0,
                        "initial_margin": 99999.0,
                        "source_vintage": "historical_2024",
                    },
                    {
                        "secid": "OLDQ",
                        "lot_size": 1,
                        "tick_size": 0.1,
                        "tick_value": 1.0,
                        "initial_margin": 7.0,
                        "source_vintage": "historical_2024",
                    },
                ]
            }
            (Path(directory) / "historical-contracts.json").write_text(
                json.dumps(historical), encoding="utf-8"
            )
            selected, _ = load_contracts(path, "NEAR", "FAR")
            # Current intake wins for legs present in both files.
            self.assertEqual(selected["NEAR"]["initial_margin"], 100.0)
            self.assertIs(selected["NEAR"]["historical_evidence"], False)

    def test_historical_supplies_missing_leg_marked_as_historical(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_intake(directory)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["instruments"] = [i for i in payload["instruments"] if i["secid"] != "FAR"]
            path.write_text(json.dumps(payload), encoding="utf-8")
            historical = {
                "instruments": [
                    {
                        "secid": "FAR",
                        "lot_size": 1,
                        "tick_size": 0.1,
                        "tick_value": 1.0,
                        "initial_margin": 42.0,
                        "source_vintage": "historical_2024",
                    }
                ]
            }
            (Path(directory) / "historical-contracts.json").write_text(
                json.dumps(historical), encoding="utf-8"
            )
            selected, _ = load_contracts(path, "NEAR", "FAR")
            self.assertEqual(selected["FAR"]["initial_margin"], 42.0)
            self.assertIs(selected["FAR"]["historical_evidence"], True)

    def test_empty_historical_file_does_not_change_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = _write_intake(directory)
            _, digest_before = load_contracts(path, "NEAR", "FAR")
            (Path(directory) / "historical-contracts.json").write_text(
                json.dumps({"instruments": []}), encoding="utf-8"
            )
            _, digest_after = load_contracts(path, "NEAR", "FAR")
            self.assertEqual(digest_before, digest_after)

    def test_json_readers_reject_non_finite_constants(self):
        from moex_spread_lab.core.w0 import load_object

        for constant in ("NaN", "Infinity", "-Infinity"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bad.json"
                path.write_text(f'{{"instruments": [], "x": {constant}}}', encoding="utf-8")
                with self.assertRaises(W0Error):
                    _reader = __import__("moex_spread_lab.core.w0", fromlist=["read_hashed_json"])
                    _reader.read_hashed_json(path, error=W0Error)
                with self.assertRaises(Exception) as ctx:
                    load_object(path, label="test", error=W0Error)
                self.assertIn("non-standard JSON constant", str(ctx.exception))

    def test_json_readers_raise_typed_error_on_deep_nesting(self):
        from moex_spread_lab.core.w0 import load_object

        deep = "[" * 200_000 + "]" * 200_000
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deep.json"
            path.write_text(deep, encoding="utf-8")
            from moex_spread_lab.core import w0

            with self.assertRaises(W0Error):
                w0.read_hashed_json(path, error=W0Error)
            with self.assertRaises(W0Error):
                load_object(path, label="test", error=W0Error)

    def test_json_readers_accept_utf8_bom(self):
        from moex_spread_lab.core import w0
        from moex_spread_lab.core.w0 import load_object

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bom.json"
            path.write_bytes(b'\xef\xbb\xbf{"a": 1}')
            loaded = load_object(path, label="test", error=W0Error)
            self.assertEqual(loaded["a"], 1)
            hashed, _ = w0.read_hashed_json(path, error=W0Error)
            self.assertEqual(hashed["a"], 1)

    def test_read_hashed_json_mutation_does_not_poison_cache(self):
        from moex_spread_lab.core import w0

        class E(W0Error):
            pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.json"
            path.write_text('{"a": 1}', encoding="utf-8")
            original_min_age = w0._HASHED_JSON_MIN_AGE_NS
            w0._HASHED_JSON_MIN_AGE_NS = 0
            try:
                first, digest = w0.read_hashed_json(path, error=E)
                first["a"] = 999
                second, second_digest = w0.read_hashed_json(path, error=E)
            finally:
                w0._HASHED_JSON_MIN_AGE_NS = original_min_age
            self.assertEqual(second, {"a": 1})
            self.assertEqual(second_digest, digest)

    def test_read_hashed_json_same_size_rewrite_with_restored_mtime(self):
        from moex_spread_lab.core import w0

        class E(W0Error):
            pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.json"
            path.write_text('{"a": 1}', encoding="utf-8")
            w0.read_hashed_json(path, error=E)
            stat = path.stat()
            path.write_text('{"a": 2}', encoding="utf-8")
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            # Freshly written files bypass the memo (coarse clocks), so the rewrite is seen.
            after, _ = w0.read_hashed_json(path, error=E)
            self.assertEqual(after, {"a": 2})

    def test_validate_contract_number_rejects_huge_ints(self):
        from moex_spread_lab.core.w0 import _validate_contract_number

        with self.assertRaises(W0Error):
            _validate_contract_number(10**400, "field")

    def test_secret_scan_missing_root_raises(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(W0Error):
            scan_secrets(Path(directory) / "does-not-exist")

    def test_secret_scan_root_under_excluded_dir_name_is_scanned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "node_modules" / "proj"
            root.mkdir(parents=True)
            (root / "k.txt").write_text("ghp_" + "a" * 30, encoding="utf-8")
            self.assertTrue(scan_secrets(root))

    def test_secret_scan_reports_large_skipped_files(self):
        with tempfile.TemporaryDirectory() as directory:
            big = Path(directory) / "big.bin"
            big.write_bytes(b"ghp_" + b"a" * 36 + b"\n" + b"x" * (5 * 1024 * 1024))
            report = scan_secrets_detailed(Path(directory))
            self.assertEqual(report["findings"], [])
            self.assertEqual(len(report["skipped"]), 1)
            self.assertEqual(report["skipped"][0]["reason"], "file-too-large")
            self.assertEqual(report["skipped"][0]["path"], "big.bin")

    def test_secret_scan_tracked_only_outside_git_repo_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "k.txt").write_text("ghp_" + "a" * 30, encoding="utf-8")
            with self.assertRaises(W0Error):
                scan_secrets(root, tracked_only=True)

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
