"""Shared policy, repository paths, and validated file operations for the code-health gate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "config" / "code-health-baseline.json"
SOURCE_ROOTS = (ROOT / "packages", ROOT / "src", ROOT / "scripts")
SOURCE_SUFFIXES = {".py", ".c", ".js", ".css", ".html"}
EXCLUDED_PARTS = {"vendor", "__pycache__"}
ALLOW_MARKER = "code-health: allow broad exception:"
POLICY = {
    "new_file_max_loc": 400,
    "new_file_max_tokens": 3500,
    "new_c_file_max_loc": 600,
    "max_cognitive_complexity": 15,
    "max_c_function_nloc": 100,
    "max_c_function_ccn": 15,
    "max_c_function_parameters": 7,
    "token_estimator": "utf8_bytes / 3.6 (rounded down)",
}
PROTECTED_EXCEPTION_ROOTS = (
    ROOT / "packages/core/src",
    ROOT / "packages/accounting/src",
    ROOT / "packages/data/src/moex_spread_lab/data/lake",
)


class GateError(RuntimeError):
    """Raised when a required analyzer or baseline cannot be read."""


def _relative(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError as exc:
        raise GateError(f"path is outside the repository: {path}") from exc


def _source_files() -> list[Path]:
    return sorted(
        path
        for root in SOURCE_ROOTS
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in SOURCE_SUFFIXES
        and not EXCLUDED_PARTS.intersection(path.parts)
    )


def _file_metrics(path: Path) -> dict[str, int]:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise GateError(f"cannot read UTF-8 source {path}: {exc}") from exc
    return {
        "loc": sum(bool(line.strip()) for line in text.splitlines()),
        "tokens": len(raw) * 10 // 36,
    }


def _load_baseline() -> dict[str, Any]:
    try:
        value = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateError(f"cannot read {BASELINE_PATH.relative_to(ROOT)}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise GateError("unsupported or malformed code-health baseline")
    if value.get("policy") != POLICY:
        raise GateError("baseline policy does not match code_health_gate.py")
    for key in ("files", "complexity", "c_functions"):
        if not isinstance(value.get(key), dict):
            raise GateError(f"baseline field {key!r} must be an object")
    if not isinstance(value.get("guard_files"), list):
        raise GateError("baseline field 'guard_files' must be a list")
    return value
