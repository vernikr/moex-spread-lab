"""Stage-9 W0: offline manifests, fixture replay and secret scanning."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable


MANIFEST_SCHEMA = "run-manifest.v1"
FIXTURE_SCHEMA = "w0.fixture.v1"
REPLAY_SCHEMA = "w0.replay.v1"

SECRET_PATTERNS = (
    re.compile(rb"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
    re.compile(rb"https?://[^/\s:@]+:[^/\s@]+@"),
    re.compile(rb"x-access-token:[A-Za-z0-9_\-]{20,}"),
)


class W0Error(ValueError):
    """Raised when a W0 manifest or fixture is invalid."""


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
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise W0Error(f"cannot read JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise W0Error(f"JSON root must be an object: {path}")
    return value


def read_hashed_json(
    path: Path, *, error: type[W0Error], label: str = "JSON input",
) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise error(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise error(f"{label} is not an object: {path}")
    return value, sha256_bytes(raw)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


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


def validate_manifest(manifest_path: Path) -> dict[str, Any]:
    root = manifest_path.resolve().parents[1]
    manifest = load_json(manifest_path)
    if _require(manifest, "schema_version", "run manifest") != MANIFEST_SCHEMA:
        raise W0Error("unsupported run manifest schema")
    run_id = _require(manifest, "run_id", "run manifest")
    if not isinstance(run_id, str) or not run_id:
        raise W0Error("run_id must be a non-empty string")
    if _require(manifest, "configuration", "run manifest").get("network_access") is not False:
        raise W0Error("W0 requires configuration.network_access=false")
    trial_counter = _require(manifest, "trial_counter", "run manifest")
    if not isinstance(trial_counter, int) or trial_counter < 0:
        raise W0Error("trial_counter must be a non-negative integer")

    expected_hash = manifest_config_hash(manifest)
    if manifest.get("config_hash") != expected_hash:
        raise W0Error("config_hash does not match the canonical run manifest")

    modules = _require(manifest, "modules", "run manifest")
    if not isinstance(modules, list) or not modules:
        raise W0Error("run manifest must pin at least one module")
    for module in modules:
        if not isinstance(module, dict):
            raise W0Error("module pin must be an object")
        module_path = _resolve(root, _require(module, "manifest_path", "module pin"))
        if sha256_file(module_path) != _require(module, "manifest_sha256", "module pin"):
            raise W0Error(f"module manifest hash mismatch: {module_path}")

    artifacts = _require(manifest, "artifacts", "run manifest")
    if not isinstance(artifacts, list):
        raise W0Error("artifacts must be a list")
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise W0Error("artifact pin must be an object")
        artifact_path = _resolve(root, _require(artifact, "path", "artifact pin"))
        if sha256_file(artifact_path) != _require(artifact, "sha256", "artifact pin"):
            raise W0Error(f"artifact hash mismatch: {artifact_path}")
        if not _require(artifact, "rights_status", "artifact pin"):
            raise W0Error(f"artifact rights_status is empty: {artifact_path}")

    requests = _require(manifest, "source_requests", "run manifest")
    if not isinstance(requests, list):
        raise W0Error("source_requests must be a list")
    for request in requests:
        if not isinstance(request, dict):
            raise W0Error("source request must be an object")
        _require(request, "rights_status", "source request")
        if request.get("network_required") is not False:
            raise W0Error("W0 source requests must declare network_required=false")

    return manifest


def normalize_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    if _require(fixture, "schema_version", "fixture") != FIXTURE_SCHEMA:
        raise W0Error("unsupported W0 fixture schema")
    records = _require(fixture, "records", "fixture")
    if not isinstance(records, list):
        raise W0Error("fixture records must be a list")
    normalized_records = []
    for record in records:
        if not isinstance(record, dict):
            raise W0Error("fixture record must be an object")
        record_id = _require(record, "record_id", "fixture record")
        normalized_records.append(dict(record, record_id=record_id))
    normalized_records.sort(key=lambda record: record["record_id"])
    return {
        "schema_version": FIXTURE_SCHEMA,
        "fixture_id": _require(fixture, "fixture_id", "fixture"),
        "source": _require(fixture, "source", "fixture"),
        "records": normalized_records,
    }


def replay(manifest_path: Path, fixture_path: Path) -> dict[str, Any]:
    manifest = validate_manifest(manifest_path)
    fixture = load_json(fixture_path)
    normalized = normalize_fixture(fixture)
    fixture_hash = sha256_file(fixture_path)
    normalized_hash = sha256_bytes(canonical_bytes(normalized))
    return {
        "schema_version": REPLAY_SCHEMA,
        "run_id": manifest["run_id"],
        "config_hash": manifest["config_hash"],
        "trial_counter": manifest["trial_counter"],
        "fixture_hash": fixture_hash,
        "normalized_fixture_hash": normalized_hash,
        "network_access": False,
        "record_count": len(normalized["records"]),
        "normalized_fixture": normalized,
    }


def iter_scan_files(root: Path) -> Iterable[Path]:
    excluded = {".git", ".venv", "__pycache__", ".opencode", "node_modules"}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in excluded for part in path.parts):
            continue
        if path.stat().st_size <= 5 * 1024 * 1024:
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
                findings.append({"path": str(path.relative_to(root)), "pattern": pattern.pattern.decode()})
    return findings


def git_tracked_files(root: Path) -> list[Path]:
    try:
        output = subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
    except (OSError, subprocess.CalledProcessError):
        return []
    return [root / item for item in output.decode().split("\0") if item]
