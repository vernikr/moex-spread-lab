"""Explicit, verified archival of non-runtime research candidates.

Run in a maintenance window: the advisory lock excludes this CLI, not arbitrary
research writers. Active data, compact run artifacts and control files are excluded.
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
from datetime import UTC, datetime
from pathlib import Path

import zstandard as zstd

from moex_spread_lab.core.w0 import canonical_bytes, sha256_file

from .data_inventory import _inspect_file, _policy, build_data_inventory
from .data_retention import (
    DataRetentionError,
    _prune_empty_parents,
    _safe_data_path,
    _validate_archive_path,
)

SCHEMA = "data-archive-candidates.v1"
GROUPS = {
    "analytics/derived",
    "w1/candles-common",
    "w1/candles/10m",
    "w1/candles/unknown",
    "w1/perpetual-history-discovery",
}


def _eligible(path: str, metadata: dict) -> bool:
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


def _fingerprint(files: list[dict]) -> str:
    return hashlib.sha256(canonical_bytes(files)).hexdigest()


def _json_new(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _outside_data(root: Path, path: Path) -> Path:
    return _validate_archive_path(root, path)


def build_plan(root: Path, archive: Path) -> dict:
    root = root.resolve()
    archive = _outside_data(root, archive)
    if archive.exists() or archive.is_symlink():
        raise DataRetentionError("archive destination already exists")
    inventory = build_data_inventory(root, include_hashes=True)
    files = sorted(
        (
            {key: entry[key] for key in ("path", "logical_bytes", "sha256")}
            for entry in inventory["files"]
            if _eligible(entry["path"], entry.get("metadata", {}))
        ),
        key=lambda entry: entry["path"],
    )
    return {
        "schema_version": SCHEMA,
        "root": str(root),
        "archive": str(archive),
        "files": files,
        "source_fingerprint": _fingerprint(files),
        "file_count": len(files),
        "logical_bytes": sum(e["logical_bytes"] for e in files),
    }


def _validate(plan: dict, root: Path) -> None:
    if plan.get("schema_version") != SCHEMA or plan.get("root") != str(root.resolve()):
        raise DataRetentionError(
            "unsupported plan or different repository root; regenerate dry-run"
        )
    files = plan.get("files")
    if not isinstance(files, list) or not files:
        raise DataRetentionError("plan must contain candidates")
    seen = set()
    for entry in files:
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
    if files != sorted(files, key=lambda e: e["path"]) or _fingerprint(files) != plan.get(
        "source_fingerprint"
    ):
        raise DataRetentionError("source fingerprint mismatch")
    _outside_data(root, Path(plan["archive"]))


def verify_archive(path: Path, manifest: dict) -> None:
    expected = {"data/" + e["path"]: e for e in manifest["files"]}
    seen = set()
    with path.open("rb") as raw, zstd.ZstdDecompressor().stream_reader(raw) as stream:
        with tarfile.open(fileobj=stream, mode="r|") as tar:
            for member in tar:
                if member.name in seen or not member.isfile():
                    raise DataRetentionError("duplicate or non-regular archive member")
                seen.add(member.name)
                body = tar.extractfile(member)
                if member.name == "manifest.json":
                    if member.size != len(
                        canonical_bytes(manifest)
                    ) or body.read() != canonical_bytes(manifest):
                        raise DataRetentionError("embedded manifest mismatch")
                else:
                    entry = expected.get(member.name)
                    if entry is None or member.size != entry["logical_bytes"]:
                        raise DataRetentionError("unexpected archive member or size")
                    digest = hashlib.sha256()
                    for block in iter(lambda: body.read(1024 * 1024), b""):
                        digest.update(block)
                    if digest.hexdigest() != entry["sha256"]:
                        raise DataRetentionError("archive member hash mismatch")
        # Drain the frame to detect compression checksum/truncation errors.
        while stream.read(1024 * 1024):
            pass
    if seen != set(expected) | {"manifest.json"}:
        raise DataRetentionError("missing archive member")


def _publish(root: Path, plan: dict, archive: Path, *, validate_sources=None) -> dict:
    if archive.exists() or archive.is_symlink():
        raise DataRetentionError("archive destination already exists")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    manifest = {**plan, "git_commit": commit, "created_at": datetime.now(UTC).isoformat()}
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
                for entry in plan["files"]:
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


def execute(root: Path, plan_path: Path, mode: str, archive: Path | None = None) -> dict:
    """Create a deterministic plan, publish only, or explicitly remove verified sources."""
    root = root.resolve()
    plan_path = _outside_data(root, plan_path)
    if (root / "data").is_symlink():
        raise DataRetentionError("symlink data root is not supported")
    with (root / ".data-archive-candidates.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DataRetentionError("another candidate archive operation is running") from exc
        if mode == "dry-run":
            if archive is None:
                raise DataRetentionError("--archive is required for dry-run")
            plan = build_plan(root, archive)
            _json_new(plan_path, plan)
            return {"status": "planned", **plan}
        if mode not in {"archive-only", "apply"}:
            raise DataRetentionError("unknown archive mode")
        plan = json.loads(plan_path.read_text())
        _validate(plan, root)
        archive = Path(plan["archive"])
        receipt = archive.with_name(archive.name + ".verified.json")
        report = archive.with_name(archive.name + ".apply.json")
        if plan_path in {archive, receipt, report}:
            raise DataRetentionError("plan and archive output paths must be distinct")
        if mode == "archive-only":
            if receipt.exists() or receipt.is_symlink():
                raise DataRetentionError("verification receipt already exists")
            manifest = _publish(root, plan, archive)
            result = {
                "status": "verified",
                "archive_sha256": sha256_file(archive),
                "manifest": manifest,
            }
            _json_new(receipt, result)
            return result
        record = json.loads(receipt.read_text())
        manifest = record["manifest"]
        if (
            record.get("status") != "verified"
            or any(manifest.get(k) != v for k, v in plan.items())
            or sha256_file(archive) != record["archive_sha256"]
        ):
            raise DataRetentionError("verified archive does not match plan")
        verify_archive(archive, manifest)
        _validate(plan, root)  # Check ALL sources before the first unlink.
        result = {
            "status": "prepared",
            "archive": str(archive),
            "archive_sha256": record["archive_sha256"],
            "source_fingerprint": plan["source_fingerprint"],
            "deleted": [],
        }
        _json_new(report, result)  # Refuse duplicate apply; establish writable journal first.
        try:
            for entry in plan["files"]:
                source = _safe_data_path(root, entry["path"])
                if sha256_file(source) != entry["sha256"]:
                    raise DataRetentionError("source changed during apply; stop concurrent writers")
                source.unlink()
                result["deleted"].append(entry["path"])
            _prune_empty_parents(root, plan["files"])
            result["status"] = "applied"
        except Exception:
            result["status"] = "interrupted"
            raise
        finally:
            temporary = report.with_name(report.name + ".tmp")
            _json_new(temporary, result)
            os.replace(temporary, report)
        return result
