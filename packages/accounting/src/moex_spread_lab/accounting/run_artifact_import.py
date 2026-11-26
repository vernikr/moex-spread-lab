"""Lossless, fail-closed import of legacy baseline JSON; never removes the source."""

from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

from moex_spread_lab.core.w0 import canonical_bytes, sha256_bytes, sha256_file

from .ledger import build_ledger
from .run_artifacts import (
    RunArtifactError,
    _atomic_write_json,
    _validate_report,
    load_run_artifact,
    publish_baseline_artifact,
    verify_run_artifact,
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RunArtifactError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise RunArtifactError(f"non-finite JSON value: {value}")


def _publish_new_directory(source: Path, target: Path) -> None:
    """Atomic no-replace rename, including an existing empty directory/symlink."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        fn = libc.renamex_np
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = fn(os.fsencode(source), os.fsencode(target), 4)
    elif sys.platform.startswith("linux"):
        fn = libc.renameat2
        fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = fn(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    else:
        raise RunArtifactError("atomic no-replace import requires Linux or macOS")
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def _consumer_checks(report_path: Path, work: Path, label: str) -> dict[str, str]:
    ledger = build_ledger(report_path)
    ledger_path = work / f"{label}-ledger.json"
    _atomic_write_json(ledger_path, ledger)
    for key in ("baseline_path", "baseline_sha256"):
        ledger["input_provenance"].pop(key, None)
    ledger_digest = sha256_bytes(canonical_bytes(ledger))
    ledger_path.unlink()
    return {
        "execution_ledger_sha256": ledger_digest,
    }


def _validate_import_args(
    legacy: Path,
    output_dir: Path,
    expected_sha256: str | None,
    retention_days: object,
) -> tuple[Path, Path, bytes, str, dict[str, Any], str]:
    if (
        isinstance(retention_days, bool)
        or not isinstance(retention_days, int)
        or retention_days < 0
    ):
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
        report = cast(dict[str, Any], report)
        _validate_report(report)
        report_digest = sha256_bytes(canonical_bytes(report))
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise RunArtifactError(f"invalid legacy baseline: {exc}") from exc
    return source, target, raw, digest, report, report_digest


def _bind_legacy_lineage(
    staged: Path,
    source_name: str,
    digest: str,
    report_digest: str,
    legacy_checks: dict[str, str],
) -> tuple[str, dict[str, Any]]:
    manifest_path = staged / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    lineage: dict[str, Any] = {
        "schema_version": "legacy-baseline-import.v1",
        "legacy_filename": source_name,
        "legacy_sha256": digest,
        "report_sha256": report_digest,
        "verification": "exact_json_roundtrip_and_consumers",
        "consumer_fingerprints": legacy_checks,
    }
    manifest["legacy_import"] = lineage
    identity = {
        "result_schema_version": manifest["result_schema_version"],
        "summary_sha256": manifest["summary_sha256"],
        "details": {k: v["sha256"] for k, v in manifest["details"].items()},
        "legacy_import": lineage,
    }
    artifact_id = sha256_bytes(canonical_bytes(identity))
    manifest.update(artifact_id=artifact_id, run_id=artifact_id)
    _atomic_write_json(manifest_path, manifest)
    verify_run_artifact(staged)
    return artifact_id, lineage


def _stage_and_publish_import(
    *,
    source: Path,
    target: Path,
    raw: bytes,
    digest: str,
    report: dict[str, Any],
    report_digest: str,
    retention_options: tuple[int, bool],
) -> dict[str, Any]:
    retention_days, pin = retention_options
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{target.name}.import-", dir=target.parent) as name:
        work = Path(name)
        frozen = work / "legacy.json"
        frozen.write_bytes(raw)
        staged = work / "artifact"
        result = publish_baseline_artifact(staged, report, retention_days=retention_days, pin=pin)
        rehydrated, _ = load_run_artifact(staged)
        if sha256_bytes(canonical_bytes(rehydrated)) != report_digest:
            raise RunArtifactError("lossy JSON-to-Parquet roundtrip; artifact not published")
        legacy_checks = _consumer_checks(frozen, work, "legacy")
        compact_checks = _consumer_checks(staged, work, "compact")
        if legacy_checks != compact_checks:
            raise RunArtifactError("legacy and compact consumer results differ")
        artifact_id, lineage = _bind_legacy_lineage(
            staged, source.name, digest, report_digest, legacy_checks
        )
        if sha256_file(source) != digest:
            raise RunArtifactError("legacy source changed during import")
        _publish_new_directory(staged, target)
    return {
        **result,
        "status": "imported",
        "artifact_id": artifact_id,
        "artifact_dir": str(target),
        "summary_path": str(target / "summary.json"),
        "manifest_path": str(target / "manifest.json"),
        "legacy_sha256": digest,
        "verification": lineage["verification"],
        "consumer_fingerprints": legacy_checks,
    }


def import_baseline_artifact(
    legacy: Path,
    output_dir: Path,
    *,
    expected_sha256: str | None = None,
    retention_days: int = 30,
    pin: bool = False,
) -> dict[str, Any]:
    """Publish only after exact rehydration and consumer compatibility checks."""
    source, target, raw, digest, report, report_digest = _validate_import_args(
        legacy, output_dir, expected_sha256, retention_days
    )
    try:
        return _stage_and_publish_import(
            source=source,
            target=target,
            raw=raw,
            digest=digest,
            report=report,
            report_digest=report_digest,
            retention_options=(retention_days, pin),
        )
    except RunArtifactError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise RunArtifactError(f"legacy import failed: {exc}") from exc
