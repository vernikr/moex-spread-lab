"""Stage-9 W0: canonical hashing, contract intake, JSON boundaries and secret scanning."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Iterable
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from .derived_cache import (
    HashedJSONCache as _HashedJSONCache,
)
from .derived_cache import (
    canonical_bytes,
    sha256_bytes,
)
from .derived_cache import (
    # Redundant alias keeps the historical w0 export for data/retention callers.
    sha256_file as sha256_file,
)

type JsonScalar = bool | int | float | str | None
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
type JsonObject = dict[str, JsonValue]


class JSONBoundaryError(ValueError):
    """Raised when decoded input is not a JSON object with string keys."""


def as_str_dict(
    value: object,
    *,
    label: str = "JSON value",
    error: type[Exception] = JSONBoundaryError,
) -> JsonObject:
    """Validate and narrow a decoded object without allowing non-string keys."""
    if not isinstance(value, dict):
        raise error(f"{label} must be a JSON object with string keys")
    object_value = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in object_value):
        raise error(f"{label} must be a JSON object with string keys")
    return cast(JsonObject, value)


def load_object(
    path: str | Path,
    *,
    label: str | None = None,
    error: type[Exception] = JSONBoundaryError,
) -> JsonObject:
    """Read UTF-8 JSON from ``path`` and require an object root.

    Callers may provide their domain-specific exception class, keeping the
    boundary typed without changing public error contracts.
    """
    source = Path(path)
    context = label or str(source)
    try:
        decoded: object = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise error(f"cannot read {context} as UTF-8 JSON: {exc}") from exc
    return as_str_dict(decoded, label=context, error=error)


SECRET_PATTERNS = (
    re.compile(rb"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
    re.compile(rb"https?://[^/\s:@]+:[^/\s@]+@"),
    re.compile(rb"x-access-token:[A-Za-z0-9_\-]{20,}"),
)


class W0Error(ValueError):
    """Raised when W0 input validation fails."""


# Thread-safe (resolved_path, st_mtime_ns, st_ctime_ns, st_inode, st_size) LRU cache for
# hashed JSON reads. ctime and inode catch atomic replacements and os.utime rewrites that
# restore mtime; the min-age rule refuses to memo-trust freshly written files, since
# coarse clocks can stamp a rewrite with an identical ctime. Callers always receive a
# deep copy, so in-place mutation can never poison the cache.

_HASHED_JSON_CACHE_CAPACITY = int(os.environ.get("MOEX_HASHED_JSON_CACHE_CAPACITY", "256"))
_HASHED_JSON_MIN_AGE_NS = 2 * 1_000_000_000


_HASHED_JSON_CACHE: _HashedJSONCache[tuple[str, int, int, int, int], tuple[dict[str, Any], str]] = (
    _HashedJSONCache(_HASHED_JSON_CACHE_CAPACITY)
)


def read_hashed_json(
    path: Path,
    *,
    error: type[W0Error],
    label: str = "JSON input",
) -> tuple[dict[str, Any], str]:
    resolved = path.resolve()
    try:
        stat = resolved.stat()
    except OSError as exc:
        raise error(f"cannot stat {label} {path}: {exc}") from exc
    key = (str(resolved), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino, stat.st_size)
    fresh = (time.time_ns() - stat.st_ctime_ns) < _HASHED_JSON_MIN_AGE_NS
    if not fresh:
        cached = _HASHED_JSON_CACHE.get(key)
        if cached is not None:
            cached_value, cached_digest = cached
            return deepcopy(cached_value), cached_digest
    try:
        raw = resolved.read_bytes()
        value = as_str_dict(json.loads(raw.decode("utf-8")), label=label, error=error)
    except (OSError, ValueError) as exc:
        raise error(f"cannot read {label} {path}: {exc}") from exc
    result = (cast(dict[str, Any], value), sha256_bytes(raw))
    _HASHED_JSON_CACHE.put(key, result)
    return deepcopy(result[0]), result[1]


def write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    # A sibling temporary file keeps replacement atomic for concurrent readers.
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    # code-health: allow broad exception: a partially written temp file must not mask the original failure
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def _run_id(identity: dict[str, Any]) -> str:
    return sha256_bytes(canonical_bytes(identity))[:16]


# Public name for cross-package use; retain the existing private entry point.
run_id = _run_id


def _validate_contract_number(
    value: Any, field: str, *, error: type[W0Error] = W0Error, positive: bool = False
) -> float:
    if type(value) not in (int, float):
        raise error(f"{field} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:  # a plain int too large for float64
        raise error(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise error(f"{field} must be a finite number")
    if positive and number <= 0:
        raise error(f"{field} must be positive")
    return number


def _instruments_by_secid(
    rows: Any, *, source: str, error: type[W0Error]
) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        raise error(f"{source}: instruments must be a list")
    instruments: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(cast(list[Any], rows)):
        if not isinstance(row, dict):
            raise error(f"{source}: instruments[{index}] must be an object")
        instrument = cast(dict[str, Any], row)
        secid = instrument.get("secid")
        if not isinstance(secid, str) or not secid.strip():
            raise error(f"{source}: instruments[{index}] is missing a secid")
        if secid in instruments:
            raise error(f"{source}: duplicate contract record for secid: {secid}")
        instruments[secid] = instrument
    return instruments


def _merge_historical(
    instruments: dict[str, dict[str, Any]],
    historical_path: Path,
    error: type[W0Error],
) -> tuple[set[str], str | None]:
    """Fill gaps from the historical file. Returns (historical secids, digest if used)."""
    if not historical_path.is_file():
        return set(), None
    historical, historical_digest = read_hashed_json(historical_path, error=error)
    historical_map = _instruments_by_secid(
        historical.get("instruments", []), source="historical contracts", error=error
    )
    historical_only = {k: v for k, v in historical_map.items() if k not in instruments}
    instruments.update(historical_only)
    return set(historical_only), historical_digest if historical_only else None


def _selected_contract(
    secid: str,
    instrument: dict[str, Any] | None,
    historical_secids: set[str],
    error: type[W0Error],
) -> dict[str, Any]:
    if instrument is None:
        raise error(f"contract is missing from intake: {secid}")
    for field in (
        "lot_size",
        "tick_size",
        "tick_value",
        "initial_margin",
        "source_vintage",
    ):
        if field not in instrument:
            raise error(f"contract {secid} is missing {field}")
    vintage = instrument["source_vintage"]
    if not isinstance(vintage, str) or not vintage.strip():
        raise error(f"contract {secid} is missing source_vintage")
    lot_size = _validate_contract_number(
        instrument["lot_size"],
        f"{secid}.lot_size",
        error=error,
        positive=True,
    )
    if not lot_size.is_integer() or lot_size < 1:
        raise error(f"{secid}.lot_size must be a whole number greater than or equal to 1")
    return {
        "secid": secid,
        "stable_id": instrument.get("stable_id"),
        "board": instrument.get("board"),
        "expiry": instrument.get("expiry"),
        "lot_size": int(lot_size),
        "tick_size": _validate_contract_number(
            instrument["tick_size"],
            f"{secid}.tick_size",
            error=error,
            positive=True,
        ),
        "tick_value": _validate_contract_number(
            instrument["tick_value"],
            f"{secid}.tick_value",
            error=error,
            positive=True,
        ),
        "initial_margin": _validate_contract_number(
            instrument["initial_margin"],
            f"{secid}.initial_margin",
            error=error,
            positive=True,
        ),
        "source_vintage": vintage,
        "tariff_group": instrument.get("tariff_group"),
        "historical_evidence": instrument.get("historical_evidence", secid in historical_secids),
        "historical_specification_basis": instrument.get("historical_specification_basis"),
    }


def load_contracts(
    path: Path,
    near: str,
    far: str,
    *,
    error: type[W0Error] = W0Error,
) -> tuple[dict[str, Any], str]:
    if near == far:
        raise error("near and far must be different contracts")
    payload, digest = read_hashed_json(path, error=error)
    instruments = _instruments_by_secid(
        payload.get("instruments", []), source="contracts", error=error
    )
    # Precedence (documented): the current intake is authoritative; the historical
    # file only supplies contracts absent from it. Historical entries keep their
    # provenance explicit via historical_evidence.
    historical_secids, historical_digest = _merge_historical(
        instruments, Path(path).parent / "historical-contracts.json", error
    )
    if historical_digest is not None:
        digest = hashlib.sha256(f"{digest}:{historical_digest}".encode("ascii")).hexdigest()
    selected = {
        secid: _selected_contract(secid, instruments.get(secid), historical_secids, error)
        for secid in (near, far)
    }
    return selected, digest


SCAN_EXCLUDED_DIRS = frozenset({".git", ".venv", "__pycache__", ".opencode", "node_modules"})
SCAN_MAX_FILE_BYTES = 5 * 1024 * 1024


def iter_scan_files(root: Path) -> Iterable[Path]:
    root = Path(root)
    if not root.is_dir():
        raise W0Error(f"secret-scan root does not exist or is not a directory: {root}")
    # Prune excluded directories instead of walking them; the filter compares
    # parts relative to root, so a root living under e.g. node_modules still scans.
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SCAN_EXCLUDED_DIRS)
        base = Path(dirpath)
        for name in sorted(filenames):
            yield base / name


def _scan_relpath(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def scan_secrets_detailed(root: Path, *, tracked_only: bool = False) -> dict[str, Any]:
    root = Path(root)
    findings: list[dict[str, str]] = []
    skipped: list[dict[str, Any]] = []
    paths = git_tracked_files(root) if tracked_only else iter_scan_files(root)
    for path in paths:
        rel = _scan_relpath(path, root)
        try:
            size = path.stat().st_size
        except OSError as exc:
            skipped.append({"path": rel, "reason": f"cannot stat: {exc.strerror or exc}"})
            continue
        if not path.is_file():
            continue
        if size > SCAN_MAX_FILE_BYTES:
            skipped.append({"path": rel, "reason": "file-too-large", "size": size})
            continue
        try:
            content = path.read_bytes()
        except OSError as exc:
            skipped.append({"path": rel, "reason": f"cannot read: {exc.strerror or exc}"})
            continue
        for pattern in SECRET_PATTERNS:
            match = pattern.search(content)
            if match:
                findings.append({"path": rel, "pattern": pattern.pattern.decode()})
    return {"findings": findings, "skipped": skipped}


def scan_secrets(root: Path, *, tracked_only: bool = False) -> list[dict[str, str]]:
    report = scan_secrets_detailed(root, tracked_only=tracked_only)
    return cast(list[dict[str, str]], report["findings"])


def git_tracked_files(root: Path) -> list[Path]:
    try:
        output = subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise W0Error(
            f"cannot list tracked files in {root}: {exc}; refusing to report a clean scan"
        ) from exc
    return [root / item for item in os.fsdecode(output).split("\0") if item]
