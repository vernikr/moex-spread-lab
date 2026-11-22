"""Read-only storage monitoring and verified recovery catalogs; no deletion API."""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

from moex_spread_lab.core.w0 import W0Error, canonical_bytes, sha256_bytes, sha256_file

from ..lake.data_lake import RawResponseArchive
from .data_archive_candidates import verify_archive
from .data_inventory import _policy

RUN_ARTIFACT_SCHEMA = "research-run-artifact.v1"
_RUN_DETAIL_NAMES = ("daily", "trades")


def _dry_run_gc_run_artifacts(runs_root: Path) -> dict:
    """Read-only TTL inspection of run-artifact detail files without deletion."""
    runs_root = runs_root.resolve()
    now = datetime.now(UTC)
    expired: list[list[Path]] = []
    retained = 0
    for manifest_path in sorted(runs_root.rglob("manifest.json")) if runs_root.is_dir() else []:
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
            continue
        if retention.get("pinned") is True:
            retained += 1
            continue
        expires_at = retention.get("expires_at")
        if not isinstance(expires_at, str):
            raise W0Error("run artifact expires_at must be an ISO timestamp")
        try:
            parsed = datetime.fromisoformat(expires_at)
        except ValueError as exc:
            raise W0Error(
                f"run artifact expires_at is not an ISO timestamp: {expires_at!r}"
            ) from exc
        if parsed.tzinfo is None:
            raise W0Error(f"run artifact expires_at must include a timezone: {expires_at!r}")
        if parsed.astimezone(UTC) > now:
            retained += 1
            continue
        files = []
        for name in _RUN_DETAIL_NAMES:
            record = details.get(name)
            if not isinstance(record, dict) or not isinstance(record.get("file"), str):
                raise W0Error(f"run artifact {name} metadata is invalid: {manifest_path.parent}")
            files.append(manifest_path.parent / record["file"])
        expired.append(files)
    planned = sorted(
        str(path.relative_to(runs_root)) for files in expired for path in files if path.exists()
    )
    return {
        "status": "dry_run",
        "expired_artifact_count": len(expired),
        "retained_artifact_count": retained,
        "planned_detail_files": planned,
    }


def _files(folder: Path):
    if folder.is_symlink():
        raise W0Error(f"symbolic storage root refused: {folder}")
    if not folder.exists():
        return
    if not folder.is_dir():
        raise W0Error(f"storage root is not a directory: {folder}")
    for base, dirs, names in os.walk(folder, followlinks=False):
        for name in sorted(dirs + names):
            path = Path(base) / name
            if path.is_symlink():
                raise W0Error(f"symbolic storage path refused: {path}")
            if name in names:
                if not path.is_file():
                    raise W0Error(f"nonregular storage path refused: {path}")
                yield path


def _size(path):
    stat = path.stat()
    return {
        "file_count": 1,
        "logical_bytes": stat.st_size,
        "allocated_bytes": getattr(stat, "st_blocks", (stat.st_size + 511) // 512) * 512,
    }


def storage_budget(
    root: Path, archives_root: Path | None = None, max_allocated_bytes: int | None = None
):
    root = root.resolve()
    scopes = [("data", root / "data"), ("lake", root / "data-lake")]
    if max_allocated_bytes is not None and max_allocated_bytes < 0:
        raise W0Error("budget must not be negative")
    if archives_root is not None:
        if archives_root.is_symlink():
            raise W0Error("symbolic archive root refused")
        archives_root = archives_root.resolve()
        if not archives_root.is_dir():
            raise W0Error("archive root must exist")
        if any(
            archives_root.is_relative_to(p) or p.is_relative_to(archives_root) for _, p in scopes
        ):
            raise W0Error("archive scope overlaps data/lake scope")
        scopes.append(("archives_and_evidence", archives_root))
    groups = {}
    for scope, folder in scopes:
        for path in _files(folder):
            relative = path.relative_to(folder).as_posix()
            if scope == "data":
                category = (
                    "run_artifacts"
                    if relative.startswith("analytics/runs/")
                    else _policy(relative, {}).classification
                )
            elif scope == "lake":
                category = (
                    "raw" if "raw" in path.relative_to(folder).parts[:-1] else "retained_lake"
                )
            else:
                category = scope
            group = groups.setdefault(
                category, {"file_count": 0, "logical_bytes": 0, "allocated_bytes": 0}
            )
            for key, value in _size(path).items():
                group[key] += value
    total = {
        key: sum(g[key] for g in groups.values())
        for key in ("file_count", "logical_bytes", "allocated_bytes")
    }
    # Only the documented default TTL stores; candidate lake raw stores are not GC targets.
    ttl = {
        "raw_default_lake": RawResponseArchive(root / "data-lake").gc(dry_run=True),
        "run_details": _dry_run_gc_run_artifacts(root / "data/analytics/runs"),
    }
    return {
        "schema_version": "storage-budget.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "status": "over_budget"
        if max_allocated_bytes is not None and total["allocated_bytes"] > max_allocated_bytes
        else "reported",
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


def archive_catalog(archives: list[Path], review_date: str):
    review = date.fromisoformat(review_date)
    if review <= datetime.now(UTC).date():
        raise W0Error("review date must be in the future; it is not an expiry date")
    entries = []
    seen = set()
    for archive in archives:
        archive = archive.absolute()
        receipt = Path(str(archive) + ".verified.json")
        for path in (archive, receipt):
            if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
                raise W0Error(f"missing or symbolic archive/receipt: {path}")
        archive = archive.resolve()
        if archive in seen:
            raise W0Error("duplicate archive")
        seen.add(archive)
        record = json.loads(receipt.read_text())
        manifest = record["manifest"]
        if record.get("status") != "verified" or sha256_file(archive) != record.get(
            "archive_sha256"
        ):
            raise W0Error("archive receipt/hash mismatch")
        paths = [e["path"] for e in manifest["files"]]
        if (
            not paths
            or len(paths) != len(set(paths))
            or any(
                not isinstance(p, str)
                or not p
                or p == "."
                or Path(p).is_absolute()
                or ".." in Path(p).parts
                or Path(p).as_posix() != p
                for p in paths
            )
        ):
            raise W0Error("unsafe or duplicate archive manifest path")
        if not manifest.get("git_commit"):
            raise W0Error("archive lacks release commit")
        verify_archive(archive, manifest)
        if sha256_file(archive) != record["archive_sha256"]:
            raise W0Error("archive changed during verification")
        entries.append(
            {
                "archive": str(archive),
                "archive_sha256": record["archive_sha256"],
                "release_commit": manifest["git_commit"],
                "manifest": manifest,
                "manifest_sha256": sha256_bytes(canonical_bytes(manifest)),
                "receipt_sha256": sha256_file(receipt),
                "storage": _size(archive),
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
        )
    if not entries:
        raise W0Error("at least one archive is required")
    return {
        "schema_version": "archive-catalog.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "entries": entries,
        "deletion_authorized": False,
    }


def publish_report(path: Path, report: dict):
    """Exclusive output: never replace an existing report, data file or symlink."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
