"""Lossless, fail-closed import of legacy baseline JSON; never removes the source."""
from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from moex_spread_lab.core.w0 import canonical_bytes, sha256_bytes, sha256_file
from .ledger import build_ledger
from .run_artifacts import (
    RunArtifactError, _atomic_write_json, _validate_report, load_run_artifact,
    publish_baseline_artifact, verify_run_artifact,
)
from .validation import build_validation


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RunArtifactError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise RunArtifactError(f"non-finite JSON value: {value}")


def _publish_new_directory(source: Path, target: Path) -> None:
    """Atomic no-replace rename, including an existing empty directory/symlink."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        fn = libc.renamex_np
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = fn(os.fsencode(source), os.fsencode(target), 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        fn = libc.renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = fn(-100, os.fsencode(source), -100, os.fsencode(target), 1)  # RENAME_NOREPLACE
    else:
        raise RunArtifactError("atomic no-replace import requires Linux or macOS")
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def _consumer_checks(report_path: Path, work: Path, label: str) -> dict:
    ledger = build_ledger(report_path)
    ledger_path = work / f"{label}-ledger.json"
    _atomic_write_json(ledger_path, ledger)
    for key in ("baseline_path", "baseline_sha256"):
        ledger["input_provenance"].pop(key, None)
    ledger_digest = sha256_bytes(canonical_bytes(ledger))
    del ledger
    validation = build_validation(report_path, ledger_path, [report_path])
    for key in ("baseline_path", "baseline_sha256", "ledger_path", "ledger_sha256"):
        validation["input_provenance"].pop(key, None)
    for item in validation["sensitivity"]:
        item.pop("path", None)
        item.pop("sha256", None)
    validation_digest = sha256_bytes(canonical_bytes(validation))
    ledger_path.unlink()
    return {"execution_ledger_sha256": ledger_digest, "validate_analytics_sha256": validation_digest}


def import_baseline_artifact(
    legacy: Path, output_dir: Path, *, expected_sha256: str | None = None,
    retention_days: int = 30, pin: bool = False,
) -> dict:
    """Publish only after exact rehydration and consumer compatibility checks.

    Source bytes are frozen in a private temporary workspace, then checked again
    immediately before publication. No deletion, overwrite or automatic pinning.
    """
    if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 0:
        raise RunArtifactError("retention_days must be a nonnegative integer")
    if expected_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise RunArtifactError("expected_sha256 must be a lowercase SHA-256")
    target = output_dir.absolute()
    if os.path.lexists(target):
        raise RunArtifactError(f"run artifact already exists: {target}")
    source = legacy.resolve(strict=True)
    raw = source.read_bytes()
    digest = sha256_bytes(raw)
    if expected_sha256 is not None and digest != expected_sha256:
        raise RunArtifactError("legacy SHA-256 does not match expected source")
    try:
        report = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if not isinstance(report, dict):
            raise RunArtifactError("legacy report must be an object")
        _validate_report(report)
        report_digest = sha256_bytes(canonical_bytes(report))
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise RunArtifactError(f"invalid legacy baseline: {exc}") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{target.name}.import-", dir=target.parent) as name:
        work = Path(name)
        frozen = work / "legacy.json"
        frozen.write_bytes(raw)
        del raw
        staged = work / "artifact"
        try:
            result = publish_baseline_artifact(staged, report, retention_days=retention_days, pin=pin)
            del report
            rehydrated, _ = load_run_artifact(staged)
            if sha256_bytes(canonical_bytes(rehydrated)) != report_digest:
                raise RunArtifactError("lossy JSON-to-Parquet roundtrip; artifact not published")
            del rehydrated
            legacy_checks = _consumer_checks(frozen, work, "legacy")
            compact_checks = _consumer_checks(staged, work, "compact")
            if legacy_checks != compact_checks:
                raise RunArtifactError("legacy and compact consumer results differ")
            manifest_path = staged / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            lineage = {
                "schema_version": "legacy-baseline-import.v1",
                "legacy_filename": source.name,
                "legacy_sha256": digest,
                "report_sha256": report_digest,
                "verification": "exact_json_roundtrip_and_consumers",
                "consumer_fingerprints": legacy_checks,
            }
            manifest["legacy_import"] = lineage
            identity = {"result_schema_version": manifest["result_schema_version"],
                        "summary_sha256": manifest["summary_sha256"],
                        "details": {k: v["sha256"] for k, v in manifest["details"].items()},
                        "legacy_import": lineage}
            artifact_id = sha256_bytes(canonical_bytes(identity))
            manifest.update(artifact_id=artifact_id, run_id=artifact_id)
            _atomic_write_json(manifest_path, manifest)
            verify_run_artifact(staged)
            if sha256_file(source) != digest:
                raise RunArtifactError("legacy source changed during import")
            _publish_new_directory(staged, target)
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            raise RunArtifactError(f"legacy import failed: {exc}") from exc
    return {**result, "status": "imported", "artifact_id": artifact_id,
            "artifact_dir": str(target), "summary_path": str(target / "summary.json"),
            "manifest_path": str(target / "manifest.json"), "legacy_sha256": digest,
            "verification": lineage["verification"], "consumer_fingerprints": legacy_checks}
