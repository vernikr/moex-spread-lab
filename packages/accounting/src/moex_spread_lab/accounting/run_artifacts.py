"""Versioned, compact local artifacts for analytical baseline and sensitivity runs."""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from moex_spread_lab.core.w0 import (
    W0Error,
    canonical_bytes,
    read_hashed_json,
    sha256_bytes,
    sha256_file,
)

from .run_artifact_store import (
    _DETAIL_NAMES,
    BASELINE_SCHEMA,
    RUN_ARTIFACT_SCHEMA,
    RunArtifactError,
    _atomic_write_json,
    _manifest,
    _now,
    _read_json,
    _timestamp,
    _validate_report,
    _write_parquet,
    gc_run_artifacts,
    pin_run_artifact,
)


def _extract_input_hashes(input_provenance: Any) -> dict[str, str]:
    if not isinstance(input_provenance, dict):
        return {}
    return {
        key: value
        for key, value in input_provenance.items()
        if key.endswith("_sha256") and isinstance(value, str)
    }


def _build_artifact_manifest(
    *,
    summary: dict[str, Any],
    summary_sha256: str,
    detail_records: dict[str, dict[str, Any]],
    created_at: datetime,
    retention_days: int,
    pin: bool,
) -> tuple[str, dict[str, Any]]:
    content = {
        "result_schema_version": summary["schema_version"],
        "summary_sha256": summary_sha256,
        "details": {name: record["sha256"] for name, record in detail_records.items()},
    }
    artifact_id = sha256_bytes(canonical_bytes(content))
    input_provenance = summary.get("input_provenance")
    manifest = {
        "schema_version": RUN_ARTIFACT_SCHEMA,
        "artifact_kind": "analytical_baseline",
        "artifact_id": artifact_id,
        "run_id": artifact_id,
        "created_at": _timestamp(created_at),
        **content,
        "summary_file": "summary.json",
        "details": detail_records,
        "parameters": summary.get("parameters"),
        "input_provenance": input_provenance,
        "input_hashes": _extract_input_hashes(input_provenance),
        "price_interpretation": summary.get("price_interpretation"),
        "release_state": summary.get("release_state"),
        "retention": {
            "pinned": bool(pin),
            "detail_ttl_days": retention_days,
            "expires_at": None if pin else _timestamp(created_at + timedelta(days=retention_days)),
            "detail_status": "available",
        },
    }
    return artifact_id, manifest


def publish_baseline_artifact(
    output_dir: Path,
    report: dict[str, Any],
    *,
    retention_days: int = 30,
    pin: bool = False,
) -> dict[str, Any]:
    """Publish summary JSON plus Zstd Parquet detail files into a new run directory."""
    if retention_days < 0:
        raise RunArtifactError("run artifact retention_days must not be negative")
    summary, details = _validate_report(report)
    target = output_dir.resolve()
    if target.exists():
        raise RunArtifactError(f"run artifact already exists: {target}")
    if not target.name:
        raise RunArtifactError("run artifact output directory must have a name")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        summary_path = temporary / "summary.json"
        _atomic_write_json(summary_path, summary)
        detail_records = {
            name: _write_parquet(temporary / f"{name}.parquet", rows)
            for name, rows in details.items()
        }
        artifact_id, manifest = _build_artifact_manifest(
            summary=summary,
            summary_sha256=sha256_file(summary_path),
            detail_records=detail_records,
            created_at=_now(),
            retention_days=retention_days,
            pin=pin,
        )
        _atomic_write_json(temporary / "manifest.json", manifest)
        os.replace(temporary, target)
        temporary = None
        return {
            "artifact_id": artifact_id,
            "artifact_dir": str(target),
            "summary_path": str(target / "summary.json"),
            "manifest_path": str(target / "manifest.json"),
            "detail_rows": {name: record["row_count"] for name, record in detail_records.items()},
            "pinned": bool(pin),
            "expires_at": manifest["retention"]["expires_at"],
        }
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)


def _verify_manifest_identity(artifact_dir: Path, manifest: dict[str, Any]) -> None:
    identity_details: dict[str, str] = {}
    for name in _DETAIL_NAMES:
        record = manifest["details"].get(name)
        if not isinstance(record, dict) or not isinstance(record.get("sha256"), str):
            raise RunArtifactError(
                f"run artifact {name} identity metadata is invalid: {artifact_dir}"
            )
        identity_details[name] = record["sha256"]
    identity_content: dict[str, Any] = {
        "result_schema_version": manifest.get("result_schema_version"),
        "summary_sha256": manifest.get("summary_sha256"),
        "details": identity_details,
    }
    if "legacy_import" in manifest:
        identity_content["legacy_import"] = manifest["legacy_import"]
    expected_artifact_id = sha256_bytes(canonical_bytes(identity_content))
    if (
        manifest.get("artifact_id") != expected_artifact_id
        or manifest.get("run_id") != expected_artifact_id
    ):
        raise RunArtifactError(
            f"run artifact identity differs from manifest contents: {artifact_dir}"
        )


def _verify_summary_file(artifact_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    summary_file = manifest.get("summary_file")
    if not isinstance(summary_file, str) or Path(summary_file).name != summary_file:
        raise RunArtifactError(f"run artifact summary file is unsafe: {artifact_dir}")
    summary_path = artifact_dir / summary_file
    summary = _read_json(summary_path, label="run artifact summary")
    if sha256_file(summary_path) != manifest.get("summary_sha256"):
        raise RunArtifactError(
            f"run artifact summary SHA-256 differs from manifest: {artifact_dir}"
        )
    if summary.get("schema_version") != manifest.get("result_schema_version"):
        raise RunArtifactError(f"run artifact summary schema differs from manifest: {artifact_dir}")
    return summary


def _verify_single_detail_table(
    artifact_dir: Path, manifest: dict[str, Any], name: str
) -> list[dict[str, Any]]:
    record = manifest["details"].get(name)
    if not isinstance(record, dict):
        raise RunArtifactError(f"run artifact is missing {name} metadata: {artifact_dir}")
    filename = record.get("file")
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise RunArtifactError(f"run artifact {name} path is unsafe: {artifact_dir}")
    path = artifact_dir / filename
    if not path.is_file():
        raise RunArtifactError(f"run artifact {name} file is missing: {path}")
    if sha256_file(path) != record.get("sha256"):
        raise RunArtifactError(f"run artifact {name} SHA-256 differs from manifest: {artifact_dir}")
    table = pq.ParquetFile(path).read()
    if table.num_rows != record.get("row_count") or table.column_names != record.get("columns"):
        raise RunArtifactError(f"run artifact {name} table differs from manifest: {artifact_dir}")
    return table.to_pylist()


def verify_run_artifact(artifact_dir: Path, *, require_details: bool = True) -> dict[str, Any]:
    """Verify summary and available Parquet detail files against the manifest."""
    resolved = artifact_dir.resolve()
    manifest = _manifest(resolved)
    _verify_manifest_identity(resolved, manifest)
    summary = _verify_summary_file(resolved, manifest)
    detail_status = manifest["retention"].get("detail_status")
    if detail_status != "available":
        if require_details:
            raise RunArtifactError(
                f"run artifact details are unavailable ({detail_status}): {resolved}"
            )
        return {"manifest": manifest, "summary": summary, "details": {}}
    details = {
        name: _verify_single_detail_table(resolved, manifest, name) for name in _DETAIL_NAMES
    }
    return {"manifest": manifest, "summary": summary, "details": details}


def load_run_artifact(artifact_dir: Path) -> tuple[dict[str, Any], str]:
    """Rehydrate a baseline report for consumers that need daily/trade details."""
    verified = verify_run_artifact(artifact_dir)
    report = dict(verified["summary"])
    report.update(verified["details"])
    artifact_id = verified["manifest"].get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id:
        raise RunArtifactError(f"run artifact has no identity: {artifact_dir}")
    return report, artifact_id


def load_report(path: Path, *, error: type[W0Error]) -> tuple[dict[str, Any], str]:
    """Load either a compact run directory or the legacy pretty-JSON report."""
    if path.is_dir():
        try:
            return load_run_artifact(path)
        except RunArtifactError as exc:
            raise error(str(exc)) from exc
    return read_hashed_json(path, error=error, label="analytical report")


def write_legacy_json(path: Path, report: dict[str, Any]) -> None:
    """Explicit compatibility export; normal publishing never writes pretty detail JSON."""
    _validate_report(report)
    _atomic_write_json(path, report)


__all__ = [
    "BASELINE_SCHEMA",
    "RUN_ARTIFACT_SCHEMA",
    "RunArtifactError",
    "_atomic_write_json",
    "_validate_report",
    "gc_run_artifacts",
    "load_report",
    "load_run_artifact",
    "pin_run_artifact",
    "publish_baseline_artifact",
    "verify_run_artifact",
    "write_legacy_json",
]
