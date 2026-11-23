"""Fingerprint-keyed derived cache for expensive payloads (agent-performance-2, S4c).

The canonical workstation daemon answers ``/api/proof/catalog`` from a single
process. A cold rebuild after a restart costs seconds on real data, so this
module provides a persistent, fingerprint-keyed derived cache for it.

The fingerprint covers **every file** that can contribute to the cached payload:
all of ``data/`` (excluding the mutable ``data/analytics`` runtime state), all of
``data-lake/`` and every packaged source tree (``src/`` and ``packages/*/src/``).
Files up to 2 MiB are additionally keyed by a SHA-256 of their contents, so any
in-place edit invalidates the cached entry immediately — including same-size
rewrites inside a coarse filesystem timestamp tick (HFS+-style 1 s granularity or
the quantized clocks some sandboxes expose). Larger files are keyed by
``(relpath, mtime_ns, ctime_ns, size)``; ``ctime`` cannot be restored with
``utime(2)``, and APFS records nanosecond timestamps, which is what the
workstation runs.

Entries are stored under ``data/analytics/derived-cache/`` (gitignored, mutable
runtime state) and are scoped to the root they were built from. A stale entry is
never served: the key *is* the freshness proof. Set ``MOEX_DERIVED_CACHE=0`` to
bypass the cache entirely.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from .w0 import canonical_bytes

DERIVED_CACHE_SCHEMA = "msl.derived-cache/2"
_CONTENT_HASH_MAX_BYTES = 2 * 1024 * 1024

_DISABLED = frozenset({"0", "off", "false", "no"})


def derived_cache_enabled() -> bool:
    return os.environ.get("MOEX_DERIVED_CACHE", "1").strip().lower() not in _DISABLED


def _data_roots(root: Path) -> list[Path]:
    bases = [root / "data", root / "data-lake"]
    packages = root / "packages"
    if packages.is_dir():
        for package in sorted(packages.iterdir()):
            src = package / "src"
            if src.is_dir():
                bases.append(src)
    bases.append(root / "src")
    return [base for base in bases if base.is_dir()]


def _excluded(base: Path, path: Path) -> bool:
    if base.name != "data":
        return False
    try:
        relative = path.relative_to(base)
    except ValueError:
        return False
    return relative.parts[:1] == ("analytics",)


def _content_digest(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _entry(root: Path, path: Path) -> list[Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    rel = path.relative_to(root).as_posix()
    entry: list[Any] = [rel, stat.st_mtime_ns, getattr(stat, "st_ctime_ns", 0), stat.st_size]
    if stat.st_size <= _CONTENT_HASH_MAX_BYTES:
        entry.append(_content_digest(path))
    return entry


def contributing_files(root: Path) -> list[list[Any]]:
    """All fingerprint-relevant files: relpath, mtimes, size, content digest."""
    root = root.resolve()
    entries: list[list[Any]] = []
    for base in _data_roots(root):
        for dirpath, dirnames, filenames in os.walk(base):
            current = Path(dirpath)
            dirnames[:] = [
                name
                for name in dirnames
                if not name.startswith((".", "__pycache__")) and not _excluded(base, current / name)
            ]
            for name in filenames:
                entry = _entry(root, current / name)
                if entry is not None:
                    entries.append(entry)
    entries.sort(key=lambda item: item[0])
    return entries


def fingerprint(root: Path, namespace: str, parts: tuple[str, ...] = ()) -> str:
    """Digest binding the schema, the caller-chosen parts and every input file."""
    payload = {
        "schema": DERIVED_CACHE_SCHEMA,
        "namespace": namespace,
        "parts": list(parts),
        "files": contributing_files(root),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def cache_dir(root: Path) -> Path:
    return root.resolve() / "data" / "analytics" / "derived-cache"


def load_bytes(root: Path, key: str) -> bytes | None:
    if not derived_cache_enabled():
        return None
    path = cache_dir(root) / f"{key}.json"
    try:
        data = path.read_bytes()
    except OSError:
        return None
    # A corrupt entry is a miss, never an error and never a stale hit.
    try:
        json.loads(data)
    except ValueError:
        return None
    return data


def store_bytes(root: Path, key: str, payload: bytes) -> Path:
    cache = cache_dir(root)
    cache.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(cache))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        target = cache / f"{key}.json"
        os.replace(tmp, target)
    # code-health: allow broad exception: best-effort cleanup of a partially written cache file must never mask the original failure
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise
    return cache / f"{key}.json"


def get_json(root: Path, namespace: str, parts: tuple[str, ...] = ()) -> Any | None:
    key = fingerprint(root, namespace, parts)
    data = load_bytes(root, key)
    return None if data is None else json.loads(data)


def put_json(root: Path, namespace: str, parts: tuple[str, ...], payload: Any) -> Path | None:
    if not derived_cache_enabled():
        return None
    key = fingerprint(root, namespace, parts)
    return store_bytes(root, key, canonical_bytes(payload))
