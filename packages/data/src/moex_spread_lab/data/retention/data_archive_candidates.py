"""Explicit, verified archival of non-runtime research candidates.

Maintenance-window tool: the advisory lock excludes this CLI, not arbitrary writers;
active data, compact artifacts and control files are excluded.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, cast

import zstandard as zstd

from moex_spread_lab.core.json_types import JsonObject
from moex_spread_lab.core.w0 import as_str_dict, canonical_bytes, sha256_file

from . import data_retention as _data_retention
from .data_inventory import _inspect_file, _policy, build_data_inventory
from .data_retention import DataRetentionError

SCHEMA = "data-archive-candidates.v1"
GROUPS = {
    "analytics/derived",
    "w1/candles-common",
    "w1/candles/10m",
    "w1/candles/unknown",
    "w1/perpetual-history-discovery",
}

_SafeDataPath = Callable[[Path, str], Path]
_ValidateArchivePath = Callable[[Path, Path], Path]
_PruneEmptyParents = Callable[[Path, Sequence[Mapping[str, object]]], None]


class _CandidateEntry(TypedDict):
    """One verified candidate file as stored in a plan, receipt or manifest."""

    path: str
    logical_bytes: int
    sha256: str


__all__ = [
    "_CandidateEntry",
    "_json_new",
    "_publish",
    "read_payload_object",
    "retention_hooks",
    "verify_archive",
]


def retention_hooks() -> tuple[_SafeDataPath, _ValidateArchivePath, _PruneEmptyParents]:
    """Return the owning module's path-safety hooks in their historical order."""
    return _data_retention.archive_candidate_hooks()


def read_payload_object(path: Path, label: str) -> JsonObject:
    """Read a plan or receipt as a JSON object; a missing file still raises ``OSError``."""
    try:
        decoded: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _data_retention.DataRetentionError(f"cannot read {label}: {path}: {exc}") from exc
    return as_str_dict(decoded, label=label, error=_data_retention.DataRetentionError)


_safe_data_path, _validate_archive_path, _prune_empty_parents = retention_hooks()


def _eligible(path: str, metadata: dict[str, Any]) -> bool:
    policy = _policy(path, metadata)
    if policy.dataset not in GROUPS or policy.classification not in {
        "archive_candidate",
        "derived_archive_candidate",
    }:
        return False
    if path.startswith("analytics/"):
        # Never collect compact artifacts, inventory, plans, journals or settings.
        return (
            len(Path(path).parts) == 2
            and path.endswith(".json")
            and not Path(path).name.startswith(("data-", "archive-"))
            and metadata.get("schema_version") is not None
        )
    return True


def _entries(payload: JsonObject) -> list[_CandidateEntry]:
    return cast(list[_CandidateEntry], payload.get("files", []))


def _fingerprint(files: list[_CandidateEntry]) -> str:
    return hashlib.sha256(canonical_bytes(files)).hexdigest()


def _json_new(path: Path, value: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def build_plan(root: Path, archive: Path) -> dict[str, Any]:
    root = root.resolve()
    archive = _validate_archive_path(root, archive)
    if archive.exists() or archive.is_symlink():
        raise DataRetentionError("archive destination already exists")
    inventory = build_data_inventory(root, include_hashes=True)
    entries = cast(list[JsonObject], inventory["files"])
    files: list[dict[str, Any]] = sorted(
        (
            {
                "path": str(entry["path"]),
                "logical_bytes": cast(int, entry["logical_bytes"]),
                "sha256": str(entry["sha256"]),
            }
            for entry in entries
            if _eligible(str(entry["path"]), cast(dict[str, Any], entry.get("metadata", {})))
        ),
        key=lambda entry: entry["path"],
    )
    return {
        "schema_version": SCHEMA,
        "root": str(root),
        "archive": str(archive),
        "files": files,
        "source_fingerprint": _fingerprint(cast(list[_CandidateEntry], files)),
        "file_count": len(files),
        "logical_bytes": sum(e["logical_bytes"] for e in cast(list[_CandidateEntry], files)),
    }


def _validate(plan: JsonObject, root: Path) -> None:
    if plan.get("schema_version") != SCHEMA or plan.get("root") != str(root.resolve()):
        raise DataRetentionError(
            "unsupported plan or different repository root; regenerate dry-run"
        )
    files = plan.get("files")
    if not isinstance(files, list) or not files:
        raise DataRetentionError("plan must contain candidates")
    seen: set[str] = set()
    for entry in cast(list[JsonObject], files):
        relative = entry.get("path")
        if (
            not isinstance(relative, str)
            or not relative
            or relative == "."
            or Path(relative).as_posix() != relative
            or relative in seen
        ):
            raise DataRetentionError("invalid or duplicate candidate path")
        source = _safe_data_path(root, relative)
        if (
            not source.is_file()
            or not _eligible(relative, _inspect_file(source))
            or source.stat().st_size != entry.get("logical_bytes")
            or sha256_file(source) != entry.get("sha256")
        ):
            raise DataRetentionError(f"stale or ineligible candidate: {relative}")
        seen.add(relative)
    entries = _entries(plan)
    if entries != sorted(entries, key=lambda e: e["path"]) or _fingerprint(entries) != plan.get(
        "source_fingerprint"
    ):
        raise DataRetentionError("source fingerprint mismatch")
    _validate_archive_path(root, Path(cast(str, plan["archive"])))


def _verify_tar_member(
    tar: tarfile.TarFile,
    member: tarfile.TarInfo,
    expected: dict[str, _CandidateEntry],
    manifest_bytes: bytes,
) -> None:
    body = tar.extractfile(member)
    if body is None:
        raise DataRetentionError("cannot read regular archive member")
    if member.name == "manifest.json":
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


def verify_archive(path: Path, manifest: JsonObject) -> None:
    expected = {"data/" + entry["path"]: entry for entry in _entries(manifest)}
    manifest_bytes = canonical_bytes(manifest)
    seen: set[str] = set()
    with path.open("rb") as raw, zstd.ZstdDecompressor().stream_reader(raw) as stream:
        with tarfile.open(fileobj=stream, mode="r|") as tar:
            for member in tar:
                if member.name in seen or not member.isfile():
                    raise DataRetentionError("duplicate or non-regular archive member")
                seen.add(member.name)
                _verify_tar_member(tar, member, expected, manifest_bytes)
        while stream.read(1024 * 1024):
            pass
    if seen != set(expected) | {"manifest.json"}:
        raise DataRetentionError("missing archive member")


def _publish(
    root: Path,
    plan: JsonObject,
    archive: Path,
    *,
    validate_sources: Callable[[JsonObject, Path], None] | None = None,
) -> JsonObject:
    if archive.exists() or archive.is_symlink():
        raise DataRetentionError("archive destination already exists")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    manifest: JsonObject = {
        **plan,
        "git_commit": commit,
        "created_at": datetime.now(UTC).isoformat(),
    }
    archive.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{archive.name}.", dir=archive.parent)
    staging = Path(name)
    try:
        with os.fdopen(fd, "wb") as raw:
            with (
                zstd.ZstdCompressor(level=19, write_checksum=True).stream_writer(
                    raw, closefd=False
                ) as stream,
                tarfile.open(fileobj=stream, mode="w|") as tar,
            ):
                body = canonical_bytes(manifest)
                info = tarfile.TarInfo("manifest.json")
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
                for entry in _entries(manifest):
                    source = _safe_data_path(root, entry["path"])
                    info = tarfile.TarInfo("data/" + entry["path"])
                    info.size = entry["logical_bytes"]
                    with source.open("rb") as handle:
                        tar.addfile(info, handle)
            raw.flush()
            os.fsync(raw.fileno())
        verify_archive(staging, manifest)
        (_validate if validate_sources is None else validate_sources)(plan, root)
        # Atomic, no-clobber publication on the same filesystem.
        os.link(staging, archive)
    finally:
        staging.unlink(missing_ok=True)
    return manifest


def _archive_only_candidates(
    root: Path, plan: JsonObject, archive: Path, receipt: Path
) -> JsonObject:
    if receipt.exists() or receipt.is_symlink():
        raise DataRetentionError("verification receipt already exists")
    manifest = _publish(root, plan, archive)
    result: JsonObject = {
        "status": "verified",
        "archive_sha256": sha256_file(archive),
        "manifest": manifest,
    }
    _json_new(receipt, result)
    return result


def _delete_verified_sources(root: Path, files: list[_CandidateEntry], deleted: list[str]) -> None:
    for entry in files:
        source = _safe_data_path(root, entry["path"])
        if sha256_file(source) != entry["sha256"]:
            raise DataRetentionError("source changed during apply; stop concurrent writers")
        source.unlink()
        deleted.append(entry["path"])
    _prune_empty_parents(root, files)


def _apply_candidates(
    root: Path, plan: JsonObject, archive: Path, receipt: Path, report: Path
) -> JsonObject:
    record = read_payload_object(receipt, "verification receipt")
    manifest = cast(JsonObject, record["manifest"])
    if (
        record.get("status") != "verified"
        or any(manifest.get(k) != v for k, v in plan.items())
        or sha256_file(archive) != record["archive_sha256"]
    ):
        raise DataRetentionError("verified archive does not match plan")
    verify_archive(archive, manifest)
    _validate(plan, root)
    deleted: list[str] = []
    result: JsonObject = {
        "status": "prepared",
        "archive": str(archive),
        "archive_sha256": record["archive_sha256"],
        "source_fingerprint": plan["source_fingerprint"],
        "deleted": [],
    }
    _json_new(report, result)
    try:
        _delete_verified_sources(root, _entries(plan), deleted)
        result["status"] = "applied"
        result["deleted"] = list(deleted)
    except Exception:
        result["status"] = "interrupted"
        result["deleted"] = list(deleted)
        raise
    finally:
        temporary = report.with_name(report.name + ".tmp")
        _json_new(temporary, result)
        os.replace(temporary, report)
    return result


def execute(root: Path, plan_path: Path, mode: str, archive: Path | None = None) -> JsonObject:
    """Create a deterministic plan, publish only, or explicitly remove verified sources."""
    resolved_root = root.resolve()
    plan_path = _validate_archive_path(resolved_root, plan_path)
    if (resolved_root / "data").is_symlink():
        raise DataRetentionError("symlink data root is not supported")
    with (resolved_root / ".data-archive-candidates.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DataRetentionError("another candidate archive operation is running") from exc
        if mode == "dry-run":
            if archive is None:
                raise DataRetentionError("--archive is required for dry-run")
            plan = build_plan(resolved_root, archive)
            _json_new(plan_path, cast(JsonObject, dict(plan)))
            return cast(JsonObject, {"status": "planned", **plan})
        if mode not in {"archive-only", "apply"}:
            raise DataRetentionError("unknown archive mode")
        plan = read_payload_object(plan_path, "candidate plan")
        _validate(plan, resolved_root)
        archive_path = Path(cast(str, plan["archive"]))
        receipt = archive_path.with_name(archive_path.name + ".verified.json")
        report = archive_path.with_name(archive_path.name + ".apply.json")
        if plan_path in {archive_path, receipt, report}:
            raise DataRetentionError("plan and archive output paths must be distinct")
        if mode == "archive-only":
            return _archive_only_candidates(resolved_root, plan, archive_path, receipt)
        return _apply_candidates(resolved_root, plan, archive_path, receipt, report)
