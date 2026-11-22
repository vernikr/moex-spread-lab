"""Versioned, compact local artifacts for analytical baseline and sensitivity runs."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from moex_spread_lab.core.w0 import (
    W0Error,
    canonical_bytes,
    read_hashed_json,
    sha256_bytes,
    sha256_file,
)

RUN_ARTIFACT_SCHEMA = "research-run-artifact.v1"
BASELINE_SCHEMA = "analytical-baseline.v1"
_DETAIL_NAMES = ("daily", "trades")


class RunArtifactError(W0Error):
    """Raised when a compact research artifact cannot preserve its contract."""


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
                + "\n",
                encoding="utf-8",
            )
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunArtifactError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunArtifactError(f"{label} root must be an object: {path}")
    return value


def _write_parquet(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    table = pa.Table.from_pylist(rows) if rows else pa.table({})
    pq.write_table(table, path, compression="zstd", version="2.6")
    return {
        "file": path.name,
        "row_count": table.num_rows,
        "columns": table.column_names,
        "sha256": sha256_file(path),
    }


def _manifest(artifact_dir: Path) -> dict[str, Any]:
    value = _read_json(artifact_dir / "manifest.json", label="run artifact manifest")
    if value.get("schema_version") != RUN_ARTIFACT_SCHEMA:
        raise RunArtifactError(f"unsupported run artifact schema: {artifact_dir}")
    if not isinstance(value.get("details"), dict) or not isinstance(value.get("retention"), dict):
        raise RunArtifactError(f"run artifact manifest is incomplete: {artifact_dir}")
    return value


def _parse_utc(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise RunArtifactError(f"{label} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RunArtifactError(f"{label} is not an ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise RunArtifactError(f"{label} must include a timezone: {value!r}")
    return parsed.astimezone(UTC)


def _validate_report(
    report: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    if report.get("schema_version") != BASELINE_SCHEMA:
        raise RunArtifactError(
            "only analytical-baseline.v1 reports can be published as baseline artifacts"
        )
    summary = {key: value for key, value in report.items() if key not in _DETAIL_NAMES}
    details: dict[str, list[dict[str, Any]]] = {}
    for name in _DETAIL_NAMES:
        rows = report.get(name)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RunArtifactError(f"analytical baseline {name} must be a list of objects")
        details[name] = [dict(row) for row in rows]
    return summary, details


def publish_baseline_artifact(
    output_dir: Path,
    report: dict[str, Any],
    *,
    retention_days: int = 30,
    pin: bool = False,
) -> dict[str, Any]:
    """Publish summary JSON plus Zstd Parquet detail files into a new run directory.

    Publishing refuses to replace an existing directory so a previous evidence run
    cannot be silently destroyed. The directory becomes visible only after all
    hashes and its manifest have been written in a same-parent staging directory.
    """
    if retention_days < 0:
        raise RunArtifactError("run artifact retention_days must not be negative")
    summary, details = _validate_report(report)
    target = output_dir.resolve()
    if target.exists():
        raise RunArtifactError(f"run artifact already exists: {target}")
    if not target.name:
        raise RunArtifactError("run artifact output directory must have a name")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        summary_path = temporary / "summary.json"
        _atomic_write_json(summary_path, summary)
        detail_records = {
            name: _write_parquet(temporary / f"{name}.parquet", rows)
            for name, rows in details.items()
        }
        created_at = _now()
        content = {
            "result_schema_version": summary["schema_version"],
            "summary_sha256": sha256_file(summary_path),
            "details": {name: record["sha256"] for name, record in detail_records.items()},
        }
        artifact_id = sha256_bytes(canonical_bytes(content))
        input_provenance = summary.get("input_provenance")
        input_hashes = (
            {
                key: value
                for key, value in input_provenance.items()
                if key.endswith("_sha256") and isinstance(value, str)
            }
            if isinstance(input_provenance, dict)
            else {}
        )
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
            "input_hashes": input_hashes,
            "price_interpretation": summary.get("price_interpretation"),
            "release_state": summary.get("release_state"),
            "retention": {
                "pinned": bool(pin),
                "detail_ttl_days": retention_days,
                "expires_at": None
                if pin
                else _timestamp(created_at + timedelta(days=retention_days)),
                "detail_status": "available",
            },
        }
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


def verify_run_artifact(artifact_dir: Path, *, require_details: bool = True) -> dict[str, Any]:
    """Verify summary and available Parquet detail files against the manifest."""
    artifact_dir = artifact_dir.resolve()
    manifest = _manifest(artifact_dir)
    identity_details = {}
    for name in _DETAIL_NAMES:
        record = manifest["details"].get(name)
        if not isinstance(record, dict) or not isinstance(record.get("sha256"), str):
            raise RunArtifactError(
                f"run artifact {name} identity metadata is invalid: {artifact_dir}"
            )
        identity_details[name] = record["sha256"]
    identity_content = {
        "result_schema_version": manifest.get("result_schema_version"),
        "summary_sha256": manifest.get("summary_sha256"),
        "details": identity_details,
    }
    if "legacy_import" in manifest:
        # Imported source identity and equivalence evidence are integrity-bound.
        identity_content["legacy_import"] = manifest["legacy_import"]
    expected_artifact_id = sha256_bytes(canonical_bytes(identity_content))
    if (
        manifest.get("artifact_id") != expected_artifact_id
        or manifest.get("run_id") != expected_artifact_id
    ):
        raise RunArtifactError(
            f"run artifact identity differs from manifest contents: {artifact_dir}"
        )
    summary_file = manifest.get("summary_file")
    if not isinstance(summary_file, str) or Path(summary_file).name != summary_file:
        raise RunArtifactError(f"run artifact summary file is unsafe: {artifact_dir}")
    summary_path = artifact_dir / summary_file
    summary = _read_json(summary_path, label="run artifact summary")
    summary_hash = sha256_file(summary_path)
    if summary_hash != manifest.get("summary_sha256"):
        raise RunArtifactError(
            f"run artifact summary SHA-256 differs from manifest: {artifact_dir}"
        )
    if summary.get("schema_version") != manifest.get("result_schema_version"):
        raise RunArtifactError(f"run artifact summary schema differs from manifest: {artifact_dir}")
    retention = manifest["retention"]
    detail_status = retention.get("detail_status")
    if detail_status != "available":
        if require_details:
            raise RunArtifactError(
                f"run artifact details are unavailable ({detail_status}): {artifact_dir}"
            )
        return {"manifest": manifest, "summary": summary, "details": {}}
    details: dict[str, list[dict[str, Any]]] = {}
    for name in _DETAIL_NAMES:
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
            raise RunArtifactError(
                f"run artifact {name} SHA-256 differs from manifest: {artifact_dir}"
            )
        table = pq.ParquetFile(path).read()
        if table.num_rows != record.get("row_count") or table.column_names != record.get("columns"):
            raise RunArtifactError(
                f"run artifact {name} table differs from manifest: {artifact_dir}"
            )
        details[name] = table.to_pylist()
    return {"manifest": manifest, "summary": summary, "details": details}


def load_run_artifact(artifact_dir: Path) -> tuple[dict[str, Any], str]:
    """Rehydrate a baseline report for consumers that need daily/trade details."""
    verified = verify_run_artifact(artifact_dir)
    report = dict(verified["summary"])
    report.update(verified["details"])
    manifest = verified["manifest"]
    artifact_id = manifest.get("artifact_id")
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


def pin_run_artifact(artifact_dir: Path, *, pinned: bool = True) -> dict[str, Any]:
    artifact_dir = artifact_dir.resolve()
    manifest = _manifest(artifact_dir)
    retention = manifest["retention"]
    if retention.get("detail_status") != "available":
        raise RunArtifactError(f"cannot pin unavailable artifact details: {artifact_dir}")
    ttl_days = retention.get("detail_ttl_days")
    if isinstance(ttl_days, bool) or not isinstance(ttl_days, int) or ttl_days < 0:
        raise RunArtifactError(f"run artifact retention TTL is invalid: {artifact_dir}")
    retention["pinned"] = bool(pinned)
    retention["expires_at"] = None if pinned else _timestamp(_now() + timedelta(days=ttl_days))
    retention["updated_at"] = _timestamp(_now())
    _atomic_write_json(artifact_dir / "manifest.json", manifest)
    return manifest


def gc_run_artifacts(runs_root: Path, *, dry_run: bool = True) -> dict[str, Any]:
    """Collect only expired, unpinned Parquet details; summaries/manifests remain."""
    runs_root = runs_root.resolve()
    now = _now()
    expired: list[tuple[Path, dict[str, Any], list[Path]]] = []
    retained = 0
    for manifest_path in sorted(runs_root.rglob("manifest.json")) if runs_root.is_dir() else []:
        manifest = _manifest(manifest_path.parent)
        retention = manifest["retention"]
        if retention.get("detail_status") != "available":
            continue
        if retention.get("pinned") is True:
            retained += 1
            continue
        expires_at = retention.get("expires_at")
        if _parse_utc(expires_at, label="run artifact expires_at") > now:
            retained += 1
            continue
        files = []
        for name in _DETAIL_NAMES:
            record = manifest["details"].get(name)
            if not isinstance(record, dict) or not isinstance(record.get("file"), str):
                raise RunArtifactError(
                    f"run artifact {name} metadata is invalid: {manifest_path.parent}"
                )
            files.append(manifest_path.parent / record["file"])
        expired.append((manifest_path.parent, manifest, files))
    planned = sorted(
        str(path.relative_to(runs_root))
        for artifact_dir, _, files in expired
        for path in files
        if path.exists()
    )
    if not dry_run:
        collected_at = _timestamp(now)
        for artifact_dir, manifest, files in expired:
            for path in files:
                path.unlink(missing_ok=True)
            manifest["retention"].update(
                {
                    "detail_status": "collected",
                    "collected_at": collected_at,
                    "expires_at": manifest["retention"].get("expires_at"),
                }
            )
            _atomic_write_json(artifact_dir / "manifest.json", manifest)
    return {
        "status": "dry_run" if dry_run else "collected",
        "expired_artifact_count": len(expired),
        "retained_artifact_count": retained,
        "planned_detail_files": planned,
    }
