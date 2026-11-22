"""Read-only storage monitoring and verified recovery catalogs; no deletion API."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from moex_spread_lab.core.w0 import W0Error, canonical_bytes, sha256_bytes, sha256_file

from ..lake.data_lake import RawResponseArchive
from .data_archive_candidates import verify_archive
from .data_inventory import _policy

RUN_ARTIFACT_SCHEMA = "research-run-artifact.v1"
_RUN_DETAIL_NAMES = ("daily", "trades")


def _parse_manifest_expiry(expires_at: Any) -> datetime:
    if not isinstance(expires_at, str):
        raise W0Error("run artifact expires_at must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(expires_at)
    except ValueError as exc:
        raise W0Error(f"run artifact expires_at is not an ISO timestamp: {expires_at!r}") from exc
    if parsed.tzinfo is None:
        raise W0Error(f"run artifact expires_at must include a timezone: {expires_at!r}")
    return parsed.astimezone(UTC)


def _inspect_run_manifest(manifest_path: Path, now: datetime) -> tuple[str, list[Path]]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise W0Error(f"cannot read run artifact manifest: {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != RUN_ARTIFACT_SCHEMA:
        raise W0Error(f"unsupported run artifact schema: {manifest_path.parent}")
    details = manifest.get("details")
    retention = manifest.get("retention")
    if not isinstance(details, dict) or not isinstance(retention, dict):
        raise W0Error(f"run artifact manifest is incomplete: {manifest_path.parent}")
    if retention.get("detail_status") != "available":
        return "skip", []
    if retention.get("pinned") is True or _parse_manifest_expiry(retention.get("expires_at")) > now:
        return "retained", []
    files: list[Path] = []
    for name in _RUN_DETAIL_NAMES:
        record = details.get(name)
        if not isinstance(record, dict) or not isinstance(record.get("file"), str):
            raise W0Error(f"run artifact {name} metadata is invalid: {manifest_path.parent}")
        files.append(manifest_path.parent / record["file"])
    return "expired", files


def _dry_run_gc_run_artifacts(runs_root: Path) -> dict[str, Any]:
    """Read-only TTL inspection of run-artifact detail files without deletion."""
    resolved = runs_root.resolve()
    now = datetime.now(UTC)
    expired: list[list[Path]] = []
    retained = 0
    manifest_paths = sorted(resolved.rglob("manifest.json")) if resolved.is_dir() else []
    for manifest_path in manifest_paths:
        state, files = _inspect_run_manifest(manifest_path, now)
        if state == "retained":
            retained += 1
        elif state == "expired":
            expired.append(files)
    planned = sorted(
        str(path.relative_to(resolved)) for files in expired for path in files if path.exists()
    )
    return {
        "status": "dry_run",
        "expired_artifact_count": len(expired),
        "retained_artifact_count": retained,
        "planned_detail_files": planned,
    }


def _validate_walk_entry(path: Path, is_file_entry: bool) -> None:
    if path.is_symlink():
        raise W0Error(f"symbolic storage path refused: {path}")
    if is_file_entry and not path.is_file():
        raise W0Error(f"nonregular storage path refused: {path}")


def _files(folder: Path) -> Iterator[Path]:
    if folder.is_symlink():
        raise W0Error(f"symbolic storage root refused: {folder}")
    if not folder.exists():
        return
    if not folder.is_dir():
        raise W0Error(f"storage root is not a directory: {folder}")
    for base, dirs, names in os.walk(folder, followlinks=False):
        name_set = set(names)
        for entry_name in sorted(dirs + names):
            path = Path(base) / entry_name
            is_file_entry = entry_name in name_set
            _validate_walk_entry(path, is_file_entry)
            if is_file_entry:
                yield path


def _size(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {
        "file_count": 1,
        "logical_bytes": stat.st_size,
        "allocated_bytes": getattr(stat, "st_blocks", (stat.st_size + 511) // 512) * 512,
    }


def _resolve_budget_scopes(
    resolved_root: Path, archives_root: Path | None
) -> list[tuple[str, Path]]:
    scopes: list[tuple[str, Path]] = [
        ("data", resolved_root / "data"),
        ("lake", resolved_root / "data-lake"),
    ]
    if archives_root is None:
        return scopes
    if archives_root.is_symlink():
        raise W0Error("symbolic archive root refused")
    resolved_archives = archives_root.resolve()
    if not resolved_archives.is_dir():
        raise W0Error("archive root must exist")
    if any(
        resolved_archives.is_relative_to(p) or p.is_relative_to(resolved_archives)
        for _, p in scopes
    ):
        raise W0Error("archive scope overlaps data/lake scope")
    scopes.append(("archives_and_evidence", resolved_archives))
    return scopes


def _classify_scope_file(scope: str, folder: Path, path: Path) -> str:
    if scope == "data":
        relative = path.relative_to(folder).as_posix()
        if relative.startswith("analytics/runs/"):
            return "run_artifacts"
        return _policy(relative, {}).classification
    if scope == "lake":
        return "raw" if "raw" in path.relative_to(folder).parts[:-1] else "retained_lake"
    return scope


def storage_budget(
    root: Path,
    archives_root: Path | None = None,
    max_allocated_bytes: int | None = None,
) -> dict[str, Any]:
    resolved_root = root.resolve()
    if max_allocated_bytes is not None and max_allocated_bytes < 0:
        raise W0Error("budget must not be negative")
    scopes = _resolve_budget_scopes(resolved_root, archives_root)
    groups: dict[str, dict[str, int]] = {}
    for scope, folder in scopes:
        for path in _files(folder):
            category = _classify_scope_file(scope, folder, path)
            group = groups.setdefault(
                category, {"file_count": 0, "logical_bytes": 0, "allocated_bytes": 0}
            )
            for key, value in _size(path).items():
                group[key] += value
    total = {
        key: sum(g[key] for g in groups.values())
        for key in ("file_count", "logical_bytes", "allocated_bytes")
    }
    ttl = {
        "raw_default_lake": RawResponseArchive(resolved_root / "data-lake").gc(dry_run=True),
        "run_details": _dry_run_gc_run_artifacts(resolved_root / "data/analytics/runs"),
    }
    is_over = max_allocated_bytes is not None and total["allocated_bytes"] > max_allocated_bytes
    return {
        "schema_version": "storage-budget.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "status": "over_budget" if is_over else "reported",
        "max_allocated_bytes": max_allocated_bytes,
        "scopes": {s: str(p) for s, p in scopes},
        "classes": dict(sorted(groups.items())),
        "total": total,
        "ttl_dry_run": ttl,
        "deletion_authorized": False,
        "notes": [
            "Classification is path-based; candidates are not deletion permission.",
            "Allocated totals sum file st_blocks; hardlinks/clones may be counted more than once.",
            "Live monitoring is not an atomic snapshot; quiesce writers for comparisons.",
            "Archives excluded unless --archives-root is provided.",
        ],
    }


def _is_safe_manifest_path(value: Any) -> bool:
    if not isinstance(value, str) or not value or value == ".":
        return False
    p = Path(value)
    return not p.is_absolute() and ".." not in p.parts and p.as_posix() == value


def _verify_single_catalog_archive(archive: Path, seen: set[Path], review: date) -> dict[str, Any]:
    abs_archive = archive.absolute()
    receipt = Path(str(abs_archive) + ".verified.json")
    for path in (abs_archive, receipt):
        if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
            raise W0Error(f"missing or symbolic archive/receipt: {path}")
    resolved = abs_archive.resolve()
    if resolved in seen:
        raise W0Error("duplicate archive")
    seen.add(resolved)
    record = json.loads(receipt.read_text(encoding="utf-8"))
    manifest = record["manifest"]
    if record.get("status") != "verified" or sha256_file(resolved) != record.get("archive_sha256"):
        raise W0Error("archive receipt/hash mismatch")
    paths = [e["path"] for e in manifest["files"]]
    if (
        not paths
        or len(paths) != len(set(paths))
        or not all(_is_safe_manifest_path(p) for p in paths)
    ):
        raise W0Error("unsafe or duplicate archive manifest path")
    if not manifest.get("git_commit"):
        raise W0Error("archive lacks release commit")
    verify_archive(resolved, manifest)
    if sha256_file(resolved) != record["archive_sha256"]:
        raise W0Error("archive changed during verification")
    return {
        "archive": str(resolved),
        "archive_sha256": record["archive_sha256"],
        "release_commit": manifest["git_commit"],
        "manifest": manifest,
        "manifest_sha256": sha256_bytes(canonical_bytes(manifest)),
        "receipt_sha256": sha256_file(receipt),
        "storage": _size(resolved),
        "next_review": review.isoformat(),
        "automatic_expiry": False,
        "verification": "full_stream_member_hashes",
        "restore_instructions": [
            "Verify archive SHA-256 and every member against this embedded manifest.",
            "Extract only into a new empty staging directory; verify every restored size and SHA-256.",
            "Stop writers before restoring original paths; never overwrite changed files.",
            "For active H1, restore all original JSON exactly before guarded data-h1-seal --deactivate.",
            "Same-disk recovery storage is not an independent backup; review does not authorize deletion.",
        ],
    }


def archive_catalog(archives: list[Path], review_date: str) -> dict[str, Any]:
    if not archives:
        raise W0Error("at least one archive is required")
    review = date.fromisoformat(review_date)
    if review <= datetime.now(UTC).date():
        raise W0Error("review date must be in the future; it is not an expiry date")
    seen: set[Path] = set()
    entries = [_verify_single_catalog_archive(arc, seen, review) for arc in archives]
    return {
        "schema_version": "archive-catalog.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "entries": entries,
        "deletion_authorized": False,
    }


def publish_report(path: Path, report: dict[str, Any]) -> None:
    """Exclusive output: never replace an existing report, data file or symlink."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
