"""Retention-managed Zstandard archive of raw ISS HTTP response bodies."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa

from .bar_types import DataLakeError, _atomic_write_json

RAW_ARCHIVE_SCHEMA = "market-data-raw.v1"


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(value)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _is_entry_expired(entry: dict[str, Any], now: datetime) -> bool:
    expires_at = entry.get("expires_at")
    if entry.get("pinned") is True or not isinstance(expires_at, str):
        return False
    try:
        return datetime.fromisoformat(expires_at).astimezone(UTC) <= now
    except ValueError as exc:
        raise DataLakeError(f"raw response expiry is invalid: {entry.get('id')}") from exc


def _verify_single_entry(root: Path, codec: pa.Codec, entry: dict[str, Any], name: str) -> int:
    try:
        raw = codec.decompress((root / name).read_bytes(), int(entry["raw_bytes"]))
    except (OSError, ValueError, KeyError) as exc:
        raise DataLakeError(f"cannot verify raw response {entry.get('id')}: {exc}") from exc
    url = entry.get("url")
    received_at = entry.get("received_at")
    if not isinstance(url, str) or not isinstance(received_at, str):
        raise DataLakeError(f"raw response provenance is invalid: {entry.get('id')}")
    request_sha256 = hashlib.sha256(url.encode("utf-8")).hexdigest()
    received_sha256 = hashlib.sha256(received_at.encode("utf-8")).hexdigest()
    expected_id = f"{request_sha256}-{entry.get('raw_response_sha256')}-{received_sha256}"
    if (
        entry.get("request_sha256") != request_sha256
        or entry.get("received_at_sha256") != received_sha256
        or entry.get("id") != expected_id
    ):
        raise DataLakeError(
            f"raw response provenance hash differs from manifest: {entry.get('id')}"
        )
    if hashlib.sha256(raw).hexdigest() != entry.get("raw_response_sha256"):
        raise DataLakeError(f"raw response SHA-256 differs from manifest: {entry.get('id')}")
    return len(raw)


class RawResponseArchive:
    """Retention-managed exact ISS response bodies, compressed as ``.json.zst``."""

    def __init__(self, lake_root: Path) -> None:
        self.lake_root = lake_root.resolve()
        self.root = self.lake_root / "raw"
        self.manifest_path = self.root / "manifest.json"
        self._codec = pa.Codec("zstd")

    def _read_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.is_file():
            return {"schema_version": RAW_ARCHIVE_SCHEMA, "entries": []}
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataLakeError(f"cannot read raw response manifest: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != RAW_ARCHIVE_SCHEMA:
            raise DataLakeError("unsupported raw response manifest schema")
        if not isinstance(value.get("entries"), list):
            raise DataLakeError("raw response manifest entries must be a list")
        return value

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        _atomic_write_json(self.manifest_path, manifest)

    @staticmethod
    def _entry_path(entry: dict[str, Any]) -> str:
        value = entry.get("path")
        if (
            not isinstance(value, str)
            or Path(value).name != value
            or not value.endswith(".json.zst")
        ):
            raise DataLakeError("raw response manifest contains an unsafe path")
        return value

    def archive(
        self,
        *,
        url: str,
        raw: bytes,
        received_at: str,
        ttl_days: int = 30,
        pin: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(url, str) or not url or not isinstance(raw, bytes):
            raise DataLakeError("raw response requires a URL and exact bytes")
        if ttl_days < 0:
            raise DataLakeError("raw response TTL must not be negative")
        now = datetime.now(UTC)
        request_sha256 = hashlib.sha256(url.encode("utf-8")).hexdigest()
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        received_sha256 = hashlib.sha256(received_at.encode("utf-8")).hexdigest()
        entry_id = f"{request_sha256}-{raw_sha256}-{received_sha256}"
        filename = f"{entry_id}.json.zst"
        compressed = self._codec.compress(raw).to_pybytes()
        _atomic_write_bytes(self.root / filename, compressed)
        manifest = self._read_manifest()
        old = next(
            (
                item
                for item in manifest["entries"]
                if isinstance(item, dict) and item.get("id") == entry_id
            ),
            None,
        )
        pinned = bool(pin or (old and old.get("pinned") is True))
        stored_at = now.isoformat(timespec="seconds")
        entry = {
            "id": entry_id,
            "path": filename,
            "request_sha256": request_sha256,
            "raw_response_sha256": raw_sha256,
            "received_at_sha256": received_sha256,
            "url": url,
            "received_at": received_at,
            "stored_at": stored_at,
            "last_seen_at": stored_at,
            "expires_at": None
            if pinned
            else (now + timedelta(days=ttl_days)).isoformat(timespec="seconds"),
            "pinned": pinned,
            "raw_bytes": len(raw),
            "compressed_bytes": len(compressed),
        }
        entries = [
            item
            for item in manifest["entries"]
            if not (isinstance(item, dict) and item.get("id") == entry_id)
        ]
        entries.append(entry)
        manifest["entries"] = sorted(entries, key=lambda item: item["id"])
        self._write_manifest(manifest)
        return entry

    def pin(self, entry_id: str, *, pinned: bool = True) -> dict[str, Any]:
        manifest = self._read_manifest()
        for entry in manifest["entries"]:
            if isinstance(entry, dict) and entry.get("id") == entry_id:
                entry["pinned"] = pinned
                entry["expires_at"] = (
                    None
                    if pinned
                    else entry.get("expires_at") or datetime.now(UTC).isoformat(timespec="seconds")
                )
                self._write_manifest(manifest)
                return entry
        raise DataLakeError(f"raw response entry is not registered: {entry_id}")

    def verify(self) -> dict[str, Any]:
        manifest = self._read_manifest()
        entries = [item for item in manifest["entries"] if isinstance(item, dict)]
        verified_bytes = 0
        registered_paths: set[str] = set()
        for entry in entries:
            name = self._entry_path(entry)
            registered_paths.add(name)
            verified_bytes += _verify_single_entry(self.root, self._codec, entry, name)
        orphans = sorted(
            path.name for path in self.root.glob("*.json.zst") if path.name not in registered_paths
        )
        return {
            "entry_count": len(entries),
            "verified_raw_bytes": verified_bytes,
            "orphan_files": orphans,
        }

    def gc(self, *, dry_run: bool = True) -> dict[str, Any]:
        manifest = self._read_manifest()
        now = datetime.now(UTC)
        expired: list[dict[str, Any]] = []
        keep: list[dict[str, Any]] = []
        for entry in manifest["entries"]:
            if not isinstance(entry, dict):
                raise DataLakeError("raw response manifest entry is invalid")
            (expired if _is_entry_expired(entry, now) else keep).append(entry)
        registered_paths = {self._entry_path(entry) for entry in manifest["entries"]}
        orphans = sorted(
            path for path in self.root.glob("*.json.zst") if path.name not in registered_paths
        )
        planned = [self._entry_path(entry) for entry in expired] + [path.name for path in orphans]
        if not dry_run:
            for name in planned:
                (self.root / name).unlink(missing_ok=True)
            manifest["entries"] = keep
            self._write_manifest(manifest)
        return {
            "status": "dry_run" if dry_run else "collected",
            "expired_entry_count": len(expired),
            "orphan_file_count": len(orphans),
            "planned_files": sorted(planned),
            "retained_entry_count": len(keep),
        }
