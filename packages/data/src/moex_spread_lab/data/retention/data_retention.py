"""Conservative archival of Phase-0 rebuildable local-data caches."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, cast

from moex_spread_lab.core.json_types import JsonObject, JsonValue, as_str_dict
from moex_spread_lab.core.w0 import W0Error, canonical_bytes, sha256_file

from .data_inventory import DATA_INVENTORY_SCHEMA, _inspect_file, _policy

DATA_ARCHIVE_SCHEMA = "data-archive.v1"
_ARCHIVE_MANIFEST_NAME = "archive-manifest.json"
_REBUILDABLE_CACHE = "rebuildable_cache"
# Preserve the wildcard surface that preceded #67.
__all__ = [
    "DATA_ARCHIVE_SCHEMA",
    "DATA_INVENTORY_SCHEMA",
    "UTC",
    "Any",
    "DataRetentionError",
    "JsonObject",
    "JsonValue",
    "Mapping",
    "Path",
    "Sequence",
    "TypedDict",
    "W0Error",
    "_prune_empty_parents",
    "_safe_data_path",
    "_validate_archive_path",
    "annotations",
    "archive_rebuildable_caches",
    "as_str_dict",
    "canonical_bytes",
    "cast",
    "datetime",
    "io",
    "json",
    "os",
    "plan_rebuildable_cache_archive",
    "sha256_file",
    "tarfile",
    "tempfile",
]


class _CacheEntry(TypedDict):
    path: str
    sha256: str
    logical_bytes: int
    allocated_bytes: int
    dataset: JsonValue


class _ArchivePlan(TypedDict):
    schema_version: str
    status: str
    root: str
    inventory_manifest: str
    archive: str
    classification: str
    file_count: int
    logical_bytes: int
    allocated_bytes: int
    requires_apply: bool
    files: list[_CacheEntry]


class DataRetentionError(W0Error):
    """Raised when a local-data archive cannot be safely created."""


def _manifest_files(manifest: JsonObject) -> list[JsonValue]:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise DataRetentionError("data inventory manifest files must be a list")
    return files


def _manifest_int(entry: JsonObject, key: str, default: int) -> int:
    return int(cast(bool | int | float | str, entry.get(key, default)))


def _load_inventory_manifest(path: Path, root: Path) -> JsonObject:
    try:
        decoded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataRetentionError(f"cannot read data inventory manifest: {path}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise DataRetentionError("unsupported data inventory manifest schema")
    manifest = cast(JsonObject, decoded)
    if manifest.get("schema_version") != DATA_INVENTORY_SCHEMA:
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
    _manifest_files(manifest)
    return manifest


def _safe_data_path(root: Path, relative: str) -> Path:
    if (root / "data").is_symlink():
        raise DataRetentionError("symlink data root is not supported")
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


def _selected_entries(manifest: JsonObject, root: Path) -> list[_CacheEntry]:
    selected: list[_CacheEntry] = []
    seen: set[str] = set()
    for entry in _manifest_files(manifest):
        if not isinstance(entry, dict) or entry.get("classification") != _REBUILDABLE_CACHE:
            continue
        relative = entry.get("path")
        digest = entry.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise DataRetentionError("rebuildable-cache entry lacks a valid path or SHA-256")
        if relative in seen:
            raise DataRetentionError(f"duplicate rebuildable-cache path in manifest: {relative}")
        source = _safe_data_path(root, relative)
        if not source.is_file():
            raise DataRetentionError(f"manifest cache file is missing: {relative}")
        if _policy(relative, _inspect_file(source)).classification != _REBUILDABLE_CACHE:
            raise DataRetentionError(f"manifest path is not a rebuildable cache: {relative}")
        if sha256_file(source) != digest:
            raise DataRetentionError(f"manifest cache hash no longer matches: {relative}")
        selected.append(
            _CacheEntry(
                path=relative,
                sha256=digest,
                logical_bytes=_manifest_int(entry, "logical_bytes", source.stat().st_size),
                allocated_bytes=_manifest_int(entry, "allocated_bytes", source.stat().st_size),
                dataset=entry.get("dataset"),
            )
        )
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


def plan_rebuildable_cache_archive(
    root: Path, manifest_path: Path, archive_path: Path
) -> dict[str, Any]:
    resolved_root = root.resolve()
    manifest = _load_inventory_manifest(manifest_path, resolved_root)
    archive = _validate_archive_path(resolved_root, archive_path)
    if archive.exists():
        raise DataRetentionError(f"archive destination already exists: {archive}")
    entries = _selected_entries(manifest, resolved_root)
    plan: _ArchivePlan = {
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
    return cast(dict[str, Any], plan)


def _archive_manifest(plan: _ArchivePlan) -> dict[str, Any]:
    return {
        "schema_version": DATA_ARCHIVE_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "root": plan["root"],
        "inventory_manifest": plan["inventory_manifest"],
        "classification": plan["classification"],
        "files": plan["files"],
    }


def write_tar(
    handle: tarfile.TarFile,
    root: Path,
    files: Sequence[Mapping[str, Any]],
    manifest_name: str,
    manifest_bytes: bytes,
) -> None:
    info = tarfile.TarInfo(manifest_name)
    info.size = len(manifest_bytes)
    handle.addfile(info, io.BytesIO(manifest_bytes))
    for entry in files:
        info = tarfile.TarInfo("data/" + entry["path"])
        info.size = entry["logical_bytes"]
        with _safe_data_path(root, entry["path"]).open("rb") as source:
            handle.addfile(info, source)


def _verify_tar_member(
    handle: tarfile.TarFile,
    member: tarfile.TarInfo,
    expected: Mapping[str, Mapping[str, Any]],
    manifest_name: str,
    manifest_bytes: bytes,
) -> None:
    body = handle.extractfile(member)
    if body is None:
        raise DataRetentionError("cannot read regular archive member")
    if member.name == manifest_name:
        if member.size != len(manifest_bytes) or body.read() != manifest_bytes:
            raise DataRetentionError("embedded manifest mismatch")
        return
    entry = expected.get(member.name)
    if entry is None or member.size != entry["logical_bytes"]:
        raise DataRetentionError("unexpected archive member or size")
    digest = hashlib.sha256()
    while block := body.read(1024 * 1024):
        digest.update(block)
    if digest.hexdigest() != entry["sha256"]:
        raise DataRetentionError("archive member hash mismatch")


def verify_tar(
    handle: tarfile.TarFile,
    files: Sequence[Mapping[str, Any]],
    manifest_name: str,
    manifest_bytes: bytes,
) -> None:
    expected = {"data/" + entry["path"]: entry for entry in files}
    seen: set[str] = set()
    for member in handle:
        if member.name in seen or not member.isfile():
            raise DataRetentionError("duplicate or non-regular archive member")
        seen.add(member.name)
        _verify_tar_member(handle, member, expected, manifest_name, manifest_bytes)
    if seen != set(expected) | {manifest_name}:
        raise DataRetentionError("missing archive member")


def _prune_empty_parents(root: Path, files: Sequence[Mapping[str, object]]) -> None:
    """Remove now-empty cache directories; callers pass any entry carrying ``path``."""
    data_root = (root / "data").resolve()
    candidates = {_safe_data_path(root, cast(str, entry["path"])).parent for entry in files}
    for directory in sorted(candidates, key=lambda item: len(item.parts), reverse=True):
        current = directory
        while current != data_root:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent


def archive_rebuildable_caches(
    root: Path, manifest_path: Path, archive_path: Path
) -> dict[str, Any]:
    """Archive verified page caches, then remove only the verified originals."""
    plan = cast(_ArchivePlan, plan_rebuildable_cache_archive(root, manifest_path, archive_path))
    files = plan["files"]
    if not files:
        return {**plan, "status": "nothing_to_archive", "requires_apply": False}
    archive = Path(plan["archive"])
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{archive.name}.",
            suffix=".tmp",
            dir=archive.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        manifest_bytes = canonical_bytes(_archive_manifest(plan)) + b"\n"
        with tarfile.open(temporary, "w:gz", compresslevel=6) as handle:
            write_tar(handle, root, files, _ARCHIVE_MANIFEST_NAME, manifest_bytes)
        with tarfile.open(temporary, "r:gz") as handle:
            verify_tar(handle, files, _ARCHIVE_MANIFEST_NAME, manifest_bytes)
        for entry in files:
            source = _safe_data_path(root, entry["path"])
            if not source.is_file() or sha256_file(source) != entry["sha256"]:
                raise DataRetentionError(f"source changed before publication: {entry['path']}")
        os.link(temporary, archive)
        temporary.unlink()
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
