"""Conservative archival of Phase-0 rebuildable local-data caches."""

from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from moex_spread_lab.core.w0 import W0Error, canonical_bytes, sha256_file
from .data_inventory import DATA_INVENTORY_SCHEMA


DATA_ARCHIVE_SCHEMA = "data-archive.v1"
_ARCHIVE_MANIFEST_NAME = "archive-manifest.json"
_REBUILDABLE_CACHE = "rebuildable_cache"


class DataRetentionError(W0Error):
    """Raised when a local-data archive cannot be safely created."""


def _load_inventory_manifest(path: Path, root: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataRetentionError(f"cannot read data inventory manifest: {path}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != DATA_INVENTORY_SCHEMA:
        raise DataRetentionError("unsupported data inventory manifest schema")
    if manifest.get("hashes_included") is not True:
        raise DataRetentionError("data inventory manifest must include SHA-256 hashes")
    expected_root = root.resolve()
    try:
        manifest_root = Path(str(manifest["root"])).resolve()
    except (KeyError, TypeError, OSError) as exc:
        raise DataRetentionError("data inventory manifest has no valid root") from exc
    if manifest_root != expected_root:
        raise DataRetentionError("data inventory manifest belongs to a different repository root")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise DataRetentionError("data inventory manifest files must be a list")
    return manifest


def _safe_data_path(root: Path, relative: str) -> Path:
    data_root = (root / "data").resolve()
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise DataRetentionError(f"manifest path escapes data root: {relative}")
    candidate = data_root / relative_path
    current = candidate
    while current != data_root:
        if current.is_symlink():
            raise DataRetentionError(f"refusing to archive symbolic link: {relative}")
        current = current.parent
    path = candidate.resolve()
    try:
        path.relative_to(data_root)
    except ValueError as exc:
        raise DataRetentionError(f"manifest path escapes data root: {relative}") from exc
    return path


def _selected_entries(manifest: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or entry.get("classification") != _REBUILDABLE_CACHE:
            continue
        relative = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(relative, str) or not relative or not isinstance(digest, str) or len(digest) != 64:
            raise DataRetentionError("rebuildable-cache entry lacks a valid path or SHA-256")
        if relative in seen:
            raise DataRetentionError(f"duplicate rebuildable-cache path in manifest: {relative}")
        source = _safe_data_path(root, relative)
        if not source.is_file():
            raise DataRetentionError(f"manifest cache file is missing: {relative}")
        if sha256_file(source) != digest:
            raise DataRetentionError(f"manifest cache hash no longer matches: {relative}")
        selected.append({
            "path": relative,
            "sha256": digest,
            "logical_bytes": int(entry.get("logical_bytes", source.stat().st_size)),
            "allocated_bytes": int(entry.get("allocated_bytes", source.stat().st_size)),
            "dataset": entry.get("dataset"),
        })
        seen.add(relative)
    return sorted(selected, key=lambda item: item["path"])


def _validate_archive_path(root: Path, archive_path: Path) -> Path:
    resolved = archive_path.expanduser().resolve()
    data_root = (root.resolve() / "data").resolve()
    try:
        resolved.relative_to(data_root)
    except ValueError:
        return resolved
    raise DataRetentionError("archive path must be outside the repository data directory")


def plan_rebuildable_cache_archive(root: Path, manifest_path: Path, archive_path: Path) -> dict[str, Any]:
    """Validate a pinned manifest and return a no-side-effect cache archive plan."""
    resolved_root = root.resolve()
    manifest = _load_inventory_manifest(manifest_path, resolved_root)
    archive = _validate_archive_path(resolved_root, archive_path)
    if archive.exists():
        raise DataRetentionError(f"archive destination already exists: {archive}")
    entries = _selected_entries(manifest, resolved_root)
    return {
        "schema_version": DATA_ARCHIVE_SCHEMA,
        "status": "planned",
        "root": str(resolved_root),
        "inventory_manifest": str(manifest_path.resolve()),
        "archive": str(archive),
        "classification": _REBUILDABLE_CACHE,
        "file_count": len(entries),
        "logical_bytes": sum(item["logical_bytes"] for item in entries),
        "allocated_bytes": sum(item["allocated_bytes"] for item in entries),
        "requires_apply": True,
        "files": entries,
    }


def _archive_manifest(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": DATA_ARCHIVE_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "root": plan["root"],
        "inventory_manifest": plan["inventory_manifest"],
        "classification": plan["classification"],
        "files": plan["files"],
    }


def _add_manifest(handle: tarfile.TarFile, manifest: dict[str, Any]) -> None:
    body = canonical_bytes(manifest) + b"\n"
    info = tarfile.TarInfo(_ARCHIVE_MANIFEST_NAME)
    info.size = len(body)
    info.mtime = int(datetime.now(UTC).timestamp())
    handle.addfile(info, io.BytesIO(body))


def _verify_archive(archive_path: Path, files: list[dict[str, Any]]) -> None:
    expected = {f"data/{entry['path']}": entry["sha256"] for entry in files}
    with tarfile.open(archive_path, "r:gz") as handle:
        names = set(handle.getnames())
        if _ARCHIVE_MANIFEST_NAME not in names:
            raise DataRetentionError("archive is missing its embedded manifest")
        missing = sorted(set(expected) - names)
        if missing:
            raise DataRetentionError(f"archive is missing expected members: {missing[:3]}")
        for name, expected_hash in expected.items():
            member = handle.getmember(name)
            source = handle.extractfile(member)
            if source is None:
                raise DataRetentionError(f"cannot read archived member: {name}")
            import hashlib
            digest = hashlib.sha256()
            with source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected_hash:
                raise DataRetentionError(f"archived member hash mismatch: {name}")


def _prune_empty_parents(root: Path, files: list[dict[str, Any]]) -> None:
    data_root = (root / "data").resolve()
    candidates = {(_safe_data_path(root, entry["path"])).parent for entry in files}
    for directory in sorted(candidates, key=lambda item: len(item.parts), reverse=True):
        current = directory
        while current != data_root:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent


def archive_rebuildable_caches(root: Path, manifest_path: Path, archive_path: Path) -> dict[str, Any]:
    """Archive verified page caches, then remove only the verified originals."""
    plan = plan_rebuildable_cache_archive(root, manifest_path, archive_path)
    files = plan["files"]
    if not files:
        return {**plan, "status": "nothing_to_archive", "requires_apply": False}
    archive = Path(plan["archive"])
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{archive.name}.", suffix=".tmp", dir=archive.parent, delete=False,
        ) as handle:
            temporary = Path(handle.name)
        with tarfile.open(temporary, "w:gz", compresslevel=6) as handle:
            for entry in files:
                source = _safe_data_path(root, entry["path"])
                handle.add(source, arcname=f"data/{entry['path']}", recursive=False)
            _add_manifest(handle, _archive_manifest(plan))
        _verify_archive(temporary, files)
        os.replace(temporary, archive)
        temporary = None
        for entry in files:
            source = _safe_data_path(root, entry["path"])
            if sha256_file(source) != entry["sha256"]:
                raise DataRetentionError(f"source changed before deletion: {entry['path']}")
            source.unlink()
        _prune_empty_parents(root, files)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {
        **plan,
        "status": "archived",
        "requires_apply": False,
        "archive_sha256": sha256_file(archive),
        "archive_logical_bytes": archive.stat().st_size,
        "deleted_file_count": len(files),
    }
