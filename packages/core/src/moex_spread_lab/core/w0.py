"""Stage-9 W0: offline run manifests and secret scanning."""

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

from .hashed_json_cache import HashedJSONCache as _HashedJSONCache
from .json_types import as_str_dict, load_object

MANIFEST_SCHEMA = "run-manifest.v1"

SECRET_PATTERNS = (
    re.compile(rb"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
    re.compile(rb"https?://[^/\s:@]+:[^/\s@]+@"),
    re.compile(rb"x-access-token:[A-Za-z0-9_\-]{20,}"),
)


class W0Error(ValueError):
    """Raised when a W0 manifest is invalid."""


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


def load_json(path: Path) -> dict[str, Any]:
    try:
        return cast(dict[str, Any], load_object(path, label=f"JSON: {path}", error=W0Error))
    except FileNotFoundError as exc:
        raise W0Error(f"cannot read JSON: {path}: {exc}") from exc


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


def manifest_config_hash(manifest: dict[str, Any]) -> str:
    payload = dict(manifest)
    payload["config_hash"] = None
    return sha256_bytes(canonical_bytes(payload))


def _require(mapping: dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise W0Error(f"missing {key!r} in {context}")
    return mapping[key]


def _resolve(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise W0Error(f"path escapes repository root: {relative}") from exc
    return path


def _validate_manifest_header(manifest: dict[str, Any]) -> None:
    if _require(manifest, "schema_version", "run manifest") != MANIFEST_SCHEMA:
        raise W0Error("unsupported run manifest schema")
    run_id_value = _require(manifest, "run_id", "run manifest")
    if not isinstance(run_id_value, str) or not run_id_value:
        raise W0Error("run_id must be a non-empty string")
    if _require(manifest, "configuration", "run manifest").get("network_access") is not False:
        raise W0Error("W0 requires configuration.network_access=false")
    trial_counter = _require(manifest, "trial_counter", "run manifest")
    if type(trial_counter) is not int or trial_counter < 0:
        raise W0Error("trial_counter must be a non-negative integer")
    if manifest.get("config_hash") != manifest_config_hash(manifest):
        raise W0Error("config_hash does not match the canonical run manifest")


def _check_manifest_pins(
    root: Path,
    pins: list[object],
    *,
    path_key: str,
    hash_key: str,
    kind: str,
    hash_label: str,
    check_rights: bool = False,
) -> None:
    for pin_value in pins:
        if not isinstance(pin_value, dict):
            raise W0Error(f"{kind} pin must be an object")
        pin = cast(dict[str, Any], pin_value)
        pin_path = _resolve(root, cast(str, _require(pin, path_key, f"{kind} pin")))
        if sha256_file(pin_path) != _require(pin, hash_key, f"{kind} pin"):
            raise W0Error(f"{hash_label} hash mismatch: {pin_path}")
        if check_rights and not _require(pin, "rights_status", f"{kind} pin"):
            raise W0Error(f"{kind} rights_status is empty: {pin_path}")


def _validate_source_requests(manifest: dict[str, Any]) -> None:
    requests = _require(manifest, "source_requests", "run manifest")
    if not isinstance(requests, list):
        raise W0Error("source_requests must be a list")
    for request_value in cast(list[object], requests):
        if not isinstance(request_value, dict):
            raise W0Error("source request must be an object")
        request = cast(dict[str, Any], request_value)
        _require(request, "rights_status", "source request")
        if request.get("network_required") is not False:
            raise W0Error("W0 source requests must declare network_required=false")


def validate_manifest(manifest_path: Path) -> dict[str, Any]:
    root = manifest_path.resolve().parents[1]
    manifest = load_json(manifest_path)
    _validate_manifest_header(manifest)
    modules = _require(manifest, "modules", "run manifest")
    if not isinstance(modules, list) or not modules:
        raise W0Error("run manifest must pin at least one module")
    _check_manifest_pins(
        root,
        cast(list[object], modules),
        path_key="manifest_path",
        hash_key="manifest_sha256",
        kind="module",
        hash_label="module manifest",
    )
    artifacts = _require(manifest, "artifacts", "run manifest")
    if not isinstance(artifacts, list):
        raise W0Error("artifacts must be a list")
    _check_manifest_pins(
        root,
        cast(list[object], artifacts),
        path_key="path",
        hash_key="sha256",
        kind="artifact",
        hash_label="artifact",
        check_rights=True,
    )
    _validate_source_requests(manifest)
    return manifest


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
