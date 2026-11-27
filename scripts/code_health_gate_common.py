"""Shared policy, repository paths, and validated file operations for the code-health gate."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "config" / "code-health-baseline.json"
SOURCE_ROOTS = (ROOT / "packages", ROOT / "src", ROOT / "scripts")
SOURCE_SUFFIXES = {".py", ".c", ".js", ".css", ".html"}
EXCLUDED_PARTS = {"vendor", "__pycache__"}
ALLOW_MARKER = "code-health: allow broad exception:"

# Tiered file-size policy (TD-1): soft target (green), advisory band (yellow,
# logged, never fails) and hard ceiling (red, fails CI). Cognitive complexity and
# the import DAG stay strictly hard; they, not file length, drive reasoning cost.
FILE_BUDGETS: dict[str, dict[str, int]] = {
    "production": {"soft_loc": 400, "soft_tokens": 4000, "hard_loc": 600, "hard_tokens": 6000},
    "declarative": {"soft_loc": 550, "soft_tokens": 5500, "hard_loc": 800, "hard_tokens": 8000},
    "tests": {"soft_loc": 600, "soft_tokens": 6500, "hard_loc": 900, "hard_tokens": 9500},
    # The C99 kernel is one atomically compiled translation unit: LOC only.
    "kernel": {"soft_loc": 900, "hard_loc": 1350},
}
DECLARATIVE_STEMS = ("contracts", "universe", "catalog", "cli_parsers", "registry")
DECLARATIVE_COMPOUND_STEMS = tuple(f"_{stem}" for stem in DECLARATIVE_STEMS)
DECLARATIVE_SUFFIXES = (".html", ".css")
DECLARATIVE_DIRS = ("universe",)
# Anti-fragmentation guardrail: token-driven splits produce shallow pass-through
# shards (Ousterhout; arXiv:2610.21914).
FORBIDDEN_FRAGMENT_PATTERNS: dict[str, str] = {
    "p72 overflow shard": r"p72",
    "issue-numbered shard": r"(?:^|[_-])\d{2,4}(?:$|[_-])",
    "path-encoded shard": r"(?:^|[_-])(?:packages|src|tests|moex_spread_lab)(?:$|[_-])",
}
SHALLOW_SHARD_MAX_LOC = 40
SHALLOW_SHARD_MAX_CALLERS = 1
# Package totals are a split-vs-cohesion signal: only material regressions report.
PACKAGE_TOKEN_TOLERANCE_PCT = 2

POLICY: dict[str, Any] = {
    "declarative_dirs": list(DECLARATIVE_DIRS),
    "declarative_stems": list(DECLARATIVE_STEMS),
    "declarative_suffixes": list(DECLARATIVE_SUFFIXES),
    "file_budgets": FILE_BUDGETS,
    "forbidden_fragment_patterns": FORBIDDEN_FRAGMENT_PATTERNS,
    "max_c_function_ccn": 15,
    "max_c_function_nloc": 100,
    "max_c_function_parameters": 7,
    "max_cognitive_complexity": 15,
    "package_token_tolerance_pct": PACKAGE_TOKEN_TOLERANCE_PCT,
    "shallow_shard_max_callers": SHALLOW_SHARD_MAX_CALLERS,
    "shallow_shard_max_loc": SHALLOW_SHARD_MAX_LOC,
    "token_estimator": "utf8_bytes * 10 // 42 (~4.2 bytes/token, o200k_base-aligned for code)",
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
        # ~4.2 bytes/token matches o200k_base on ASCII code within ~3%.
        "tokens": len(raw) * 10 // 42,
    }


def _archetype_for(relative: str) -> str:
    """Classify a repository-relative source path into its budget archetype."""
    path = PurePosixPath(relative)
    suffix = path.suffix.lower()
    if suffix == ".c":
        return "kernel"
    if "tests" in path.parts:
        return "tests"
    if suffix in DECLARATIVE_SUFFIXES or path.stem in DECLARATIVE_STEMS:
        return "declarative"
    if path.stem.endswith(DECLARATIVE_COMPOUND_STEMS):
        return "declarative"
    if set(DECLARATIVE_DIRS) & set(path.parts):
        return "declarative"
    return "production"


def _budget_for(relative: str) -> tuple[str, dict[str, int]]:
    archetype = _archetype_for(relative)
    return archetype, FILE_BUDGETS[archetype]


def _package_group(relative: str) -> str | None:
    """Return the workspace package (or top-level source root) a path belongs to."""
    parts = relative.split("/")
    if not parts or parts[0] not in {"packages", "src", "scripts"}:
        return None
    return "/".join(parts[:2]) if parts[0] == "packages" and len(parts) > 1 else parts[0]


def _package_root(relative: str) -> Path | None:
    group = _package_group(relative)
    return ROOT / group if group is not None else None


def _is_compatibility_shim(relative: str) -> bool:
    """Root ``src/moex_spread_lab/*.py`` shims re-export a canonical module."""
    parts = PurePosixPath(relative).parts
    return parts[:2] == ("src", "moex_spread_lab") and len(parts) == 3


def _fragment_name_problem(name: str) -> str | None:
    """Return the forbidden-filename rule a source filename violates, if any."""
    stem = PurePosixPath(name).stem
    for label, pattern in FORBIDDEN_FRAGMENT_PATTERNS.items():
        if re.search(pattern, stem):
            return label
    return None


def _load_baseline() -> dict[str, Any]:
    try:
        value = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateError(f"cannot read {BASELINE_PATH.relative_to(ROOT)}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        raise GateError("unsupported or malformed code-health baseline")
    if value.get("policy") != POLICY:
        raise GateError("baseline policy does not match code_health_gate_common.py")
    for key in ("files", "complexity", "c_functions", "packages"):
        if not isinstance(value.get(key), dict):
            raise GateError(f"baseline field {key!r} must be an object")
    for key in ("guard_files", "legacy_fragment_files", "legacy_shallow_shards"):
        if not isinstance(value.get(key), list):
            raise GateError(f"baseline field {key!r} must be a list")
    return value
