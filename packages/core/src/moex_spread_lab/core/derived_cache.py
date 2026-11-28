"""Fingerprint-keyed derived cache for expensive payloads, plus the shared
content-hash primitives and request-local cache telemetry.

The fingerprint covers ``data/`` (excluding the mutable ``data/analytics`` state),
``data-lake/`` and every packaged source tree (``src/``, ``packages/*/src/``). Files
up to 2 MiB are keyed by a SHA-256 of their contents, so any in-place edit
invalidates the entry; larger files are keyed by ``(relpath, mtime_ns, ctime_ns,
size)``. Entries live under ``data/analytics/derived-cache/`` (gitignored runtime
state). A stale entry is never served: the key is the freshness proof. Set
``MOEX_DERIVED_CACHE=0`` to bypass the cache entirely.

``canonical_bytes`` / ``sha256_bytes`` / ``sha256_file`` are the canonical
serialization and digest helpers every artifact hash is built from; ``w0``
re-exports them for artifact-level callers. ``HashedJSONCache`` and the opt-in
``collect_cache_metrics`` counters serve the hashed-JSON readers.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Generator, Hashable
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from typing import Any

DERIVED_CACHE_SCHEMA = "msl.derived-cache/2"


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


_COUNTERS: ContextVar[dict[str, int] | None] = ContextVar("msl_cache_metrics", default=None)


def record_cache_lookup(name: str, hit: bool) -> None:
    counters = _COUNTERS.get()
    if counters is not None:
        key = f"{name}_{'hits' if hit else 'misses'}"
        counters[key] = counters.get(key, 0) + 1


@contextmanager
def collect_cache_metrics() -> Generator[dict[str, int]]:
    counters = {"json_hits": 0, "json_misses": 0, "zscore_hits": 0, "zscore_misses": 0}
    token = _COUNTERS.set(counters)
    try:
        yield counters
    finally:
        _COUNTERS.reset(token)


class HashedJSONCache[Key: Hashable, Value]:
    """Small LRU for immutable file snapshots, typed by each cache's key/value."""

    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, capacity)
        self._data: OrderedDict[Key, Value] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Key) -> Value | None:
        with self._lock:
            entry = self._data.get(key)
            record_cache_lookup("json", entry is not None)
            if entry is not None:
                self._data.move_to_end(key)
            return entry

    def put(self, key: Key, value: Value) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._capacity:
                self._data.popitem(last=False)


_CONTENT_HASH_MAX_BYTES = 2 * 1024 * 1024

_DISABLED = frozenset({"0", "off", "false", "no"})

_DIGEST_MEMO_CAPACITY = 2048
# Coarse clocks (1 s-class ticks, quantized sandboxes) can stamp a same-second
# rewrite with an identical ctime_ns, so a fresh entry is never memo-trusted;
# any real write bumps ctime to ~now, which always falls inside this window.
_DIGEST_MEMO_MIN_AGE_NS = 2 * 1_000_000_000
_digest_memo: dict[tuple[str, int, int, int], str] = {}
_digest_memo_lock = threading.Lock()


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


def _hash_or_none(path: Path) -> str | None:
    try:
        return sha256_file(path)
    except OSError:
        return None


def _content_digest(path: Path, stat: os.stat_result) -> str | None:
    # Keyed by (path, mtime_ns, ctime_ns, size). Failures are not memoized.
    key = (str(path), stat.st_mtime_ns, getattr(stat, "st_ctime_ns", 0), stat.st_size)
    if time.time_ns() - key[2] < _DIGEST_MEMO_MIN_AGE_NS:
        return _hash_or_none(path)
    with _digest_memo_lock:
        cached = _digest_memo.get(key)
        if cached is not None:
            return cached
    digest = _hash_or_none(path)
    if digest is None:
        return None
    with _digest_memo_lock:
        if len(_digest_memo) >= _DIGEST_MEMO_CAPACITY:
            _digest_memo.clear()
        _digest_memo[key] = digest
    return digest


def _prune_walked_dirs(base: Path, current: Path, dirnames: list[str], seen_dirs: set[str]) -> None:
    kept: list[str] = []
    for name in dirnames:
        target = current / name
        real = os.path.realpath(target)
        if name.startswith((".", "__pycache__")) or real in seen_dirs or _excluded(base, target):
            continue
        seen_dirs.add(real)
        kept.append(name)
    dirnames[:] = kept


def _entry(root: Path, path: Path) -> list[Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    rel = path.relative_to(root).as_posix()
    entry: list[Any] = [rel, stat.st_mtime_ns, getattr(stat, "st_ctime_ns", 0), stat.st_size]
    if stat.st_size <= _CONTENT_HASH_MAX_BYTES:
        entry.append(_content_digest(path, stat))
    return entry


def contributing_files(root: Path) -> list[list[Any]]:
    """All fingerprint-relevant files: relpath, mtimes, size, content digest.

    Symlinked directories are followed (market data may live outside the repo,
    e.g. under data/market -> /external); a realpath guard keeps symlinked
    directory cycles from being walked twice.
    """
    root = root.resolve()
    entries: list[list[Any]] = []
    for base in _data_roots(root):
        seen_dirs: set[str] = {os.path.realpath(base)}
        for dirpath, dirnames, filenames in os.walk(base, followlinks=True):
            current = Path(dirpath)
            _prune_walked_dirs(base, current, dirnames, seen_dirs)
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
    return sha256_bytes(canonical_bytes(payload))


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
