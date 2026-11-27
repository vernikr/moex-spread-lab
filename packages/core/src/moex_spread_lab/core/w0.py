"""Stage-9 W0: canonical hashing, contract intake, JSON boundaries and secret scanning."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from collections.abc import Iterable
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


# Thread-safe (resolved_path, st_mtime_ns, st_size) LRU cache for hashed JSON reads.
# A file whose mtime_ns and size are unchanged is byte-identical, so its cached
# (value, sha256) is exact; any external edit changes the key and invalidates.

_HASHED_JSON_CACHE_CAPACITY = int(os.environ.get("MOEX_HASHED_JSON_CACHE_CAPACITY", "256"))


_HASHED_JSON_CACHE: _HashedJSONCache[tuple[str, int, int], tuple[dict[str, Any], str]] = (
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
    key = (str(resolved), stat.st_mtime_ns, stat.st_size)
    cached = _HASHED_JSON_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        raw = resolved.read_bytes()
        value = as_str_dict(json.loads(raw.decode("utf-8")), label=label, error=error)
    except (OSError, ValueError) as exc:
        raise error(f"cannot read {label} {path}: {exc}") from exc
    result = (cast(dict[str, Any], value), sha256_bytes(raw))
    _HASHED_JSON_CACHE.put(key, result)
    return result


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _run_id(identity: dict[str, Any]) -> str:
    return sha256_bytes(canonical_bytes(identity))[:16]


# Public name for cross-package use; retain the existing private entry point.
run_id = _run_id


def _validate_contract_number(
    value: Any, field: str, *, error: type[W0Error] = W0Error, positive: bool = False
) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise error(f"{field} must be a finite number")
    number = float(value)
    if positive and number <= 0:
        raise error(f"{field} must be positive")
    return number


def _instruments_by_secid(rows: Any) -> dict[Any, dict[str, Any]]:
    instruments: dict[Any, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict):
            instrument = cast(dict[str, Any], row)
            instruments[instrument.get("secid")] = instrument
    return instruments


def load_contracts(
    path: Path,
    near: str,
    far: str,
    *,
    error: type[W0Error] = W0Error,
) -> tuple[dict[str, Any], str]:
    payload, digest = read_hashed_json(path, error=error)
    instruments = _instruments_by_secid(payload.get("instruments", []))
    historical_path = Path(path).parent / "historical-contracts.json"
    if historical_path.is_file():
        historical, historical_digest = read_hashed_json(historical_path, error=error)
        instruments.update(_instruments_by_secid(historical.get("instruments", [])))
        digest = hashlib.sha256(f"{digest}:{historical_digest}".encode("ascii")).hexdigest()
    selected: dict[str, Any] = {}
    for secid in (near, far):
        instrument = instruments.get(secid)
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
        selected[secid] = {
            "secid": secid,
            "stable_id": instrument.get("stable_id"),
            "board": instrument.get("board"),
            "expiry": instrument.get("expiry"),
            "lot_size": int(
                _validate_contract_number(
                    instrument["lot_size"],
                    f"{secid}.lot_size",
                    error=error,
                    positive=True,
                )
            ),
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
            "source_vintage": instrument["source_vintage"],
            "tariff_group": instrument.get("tariff_group"),
            "historical_evidence": instrument.get("historical_evidence", False),
            "historical_specification_basis": instrument.get("historical_specification_basis"),
        }
    return selected, digest


def iter_scan_files(root: Path) -> Iterable[Path]:
    excluded = {".git", ".venv", "__pycache__", ".opencode", "node_modules"}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in excluded for part in path.parts):
            continue
        yield path


def scan_secrets(root: Path, *, tracked_only: bool = False) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    paths = git_tracked_files(root) if tracked_only else iter_scan_files(root)
    for path in paths:
        if not path.is_file() or path.stat().st_size > 5 * 1024 * 1024:
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        for pattern in SECRET_PATTERNS:
            match = pattern.search(content)
            if match:
                findings.append(
                    {"path": str(path.relative_to(root)), "pattern": pattern.pattern.decode()}
                )
    return findings


def git_tracked_files(root: Path) -> list[Path]:
    try:
        output = subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
    except (OSError, subprocess.CalledProcessError):
        return []
    return [root / item for item in output.decode().split("\0") if item]
