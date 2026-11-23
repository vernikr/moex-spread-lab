"""Shared storage primitives, manifest helpers, and retention GC for run artifacts."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq

from moex_spread_lab.core.w0 import W0Error, sha256_file

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
    return cast(dict[str, Any], value)


def _write_parquet(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    # Narrow the stubs' partially unknown optional arguments to the calls we use.
    from_rows = cast(Callable[[list[dict[str, Any]]], pa.Table], pa.Table.from_pylist)
    empty_table = cast(Callable[[dict[str, Any]], pa.Table], pa.table)
    write_table = cast(Callable[..., None], pq.write_table)
    table = from_rows(rows) if rows else empty_table({})
    write_table(table, path, compression="zstd", version="2.6")
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
        if not isinstance(rows, list):
            raise RunArtifactError(f"analytical baseline {name} must be a list of objects")
        rows = cast(list[Any], rows)
        if any(not isinstance(row, dict) for row in rows):
            raise RunArtifactError(f"analytical baseline {name} must be a list of objects")
        details[name] = [dict(row) for row in cast(list[dict[str, Any]], rows)]
    return summary, details


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


def _collect_detail_files(artifact_dir: Path, manifest: dict[str, Any]) -> list[Path]:
    files: list[Path] = []
    for name in _DETAIL_NAMES:
        record = manifest["details"].get(name)
        if not isinstance(record, dict):
            raise RunArtifactError(f"run artifact {name} metadata is invalid: {artifact_dir}")
        record = cast(dict[str, Any], record)
        if not isinstance(record.get("file"), str):
            raise RunArtifactError(f"run artifact {name} metadata is invalid: {artifact_dir}")
        files.append(artifact_dir / record["file"])
    return files


def _scan_expired_artifacts(
    runs_root: Path, now: datetime
) -> tuple[list[tuple[Path, dict[str, Any], list[Path]]], int]:
    expired: list[tuple[Path, dict[str, Any], list[Path]]] = []
    retained = 0
    manifest_paths = sorted(runs_root.rglob("manifest.json")) if runs_root.is_dir() else []
    for manifest_path in manifest_paths:
        artifact_dir = manifest_path.parent
        manifest = _manifest(artifact_dir)
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
        expired.append((artifact_dir, manifest, _collect_detail_files(artifact_dir, manifest)))
    return expired, retained


def gc_run_artifacts(runs_root: Path, *, dry_run: bool = True) -> dict[str, Any]:
    """Collect only expired, unpinned Parquet details; summaries/manifests remain."""
    resolved_root = runs_root.resolve()
    now = _now()
    expired, retained = _scan_expired_artifacts(resolved_root, now)
    planned = sorted(
        str(path.relative_to(resolved_root))
        for _, _, files in expired
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


# Shared names within accounting; old private names remain importable.
DETAIL_NAMES = _DETAIL_NAMES
atomic_write_json = _atomic_write_json
manifest = _manifest
now = _now
read_json = _read_json
timestamp = _timestamp
validate_report = _validate_report
write_parquet = _write_parquet
