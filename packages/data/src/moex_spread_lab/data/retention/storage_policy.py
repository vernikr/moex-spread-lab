"""Read-only storage monitoring; no deletion API."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from moex_spread_lab.core.w0 import W0Error

from ..lake.data_lake import RawResponseArchive
from .data_inventory import _policy


def _validate_walk_entry(path: Path, is_file_entry: bool) -> None:
    if path.is_symlink():
        raise W0Error(f"symbolic storage path refused: {path}")
    if is_file_entry and not path.is_file():
        raise W0Error(f"nonregular storage path refused: {path}")


def _files(folder: Path) -> Iterator[Path]:
    if folder.is_symlink():
        raise W0Error(f"symbolic storage root refused: {folder}")
    if not folder.exists():
        return
    if not folder.is_dir():
        raise W0Error(f"storage root is not a directory: {folder}")
    for base, dirs, names in os.walk(folder, followlinks=False):
        name_set = set(names)
        for entry_name in sorted(dirs + names):
            path = Path(base) / entry_name
            is_file_entry = entry_name in name_set
            _validate_walk_entry(path, is_file_entry)
            if is_file_entry:
                yield path


def _size(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {
        "file_count": 1,
        "logical_bytes": stat.st_size,
        "allocated_bytes": getattr(stat, "st_blocks", (stat.st_size + 511) // 512) * 512,
    }


def _resolve_budget_scopes(
    resolved_root: Path, archives_root: Path | None
) -> list[tuple[str, Path]]:
    scopes: list[tuple[str, Path]] = [
        ("data", resolved_root / "data"),
        ("lake", resolved_root / "data-lake"),
    ]
    if archives_root is None:
        return scopes
    if archives_root.is_symlink():
        raise W0Error("symbolic archive root refused")
    resolved_archives = archives_root.resolve()
    if not resolved_archives.is_dir():
        raise W0Error("archive root must exist")
    if any(
        resolved_archives.is_relative_to(p) or p.is_relative_to(resolved_archives)
        for _, p in scopes
    ):
        raise W0Error("archive scope overlaps data/lake scope")
    scopes.append(("archives_and_evidence", resolved_archives))
    return scopes


def _classify_scope_file(scope: str, folder: Path, path: Path) -> str:
    if scope == "data":
        relative = path.relative_to(folder).as_posix()
        if relative.startswith("analytics/runs/"):
            return "run_artifacts"
        return _policy(relative, {}).classification
    if scope == "lake":
        return "raw" if "raw" in path.relative_to(folder).parts[:-1] else "retained_lake"
    return scope


def storage_budget(
    root: Path,
    archives_root: Path | None = None,
    max_allocated_bytes: int | None = None,
) -> dict[str, Any]:
    resolved_root = root.resolve()
    if max_allocated_bytes is not None and max_allocated_bytes < 0:
        raise W0Error("budget must not be negative")
    scopes = _resolve_budget_scopes(resolved_root, archives_root)
    groups: dict[str, dict[str, int]] = {}
    for scope, folder in scopes:
        for path in _files(folder):
            category = _classify_scope_file(scope, folder, path)
            group = groups.setdefault(
                category, {"file_count": 0, "logical_bytes": 0, "allocated_bytes": 0}
            )
            for key, value in _size(path).items():
                group[key] += value
    total = {
        key: sum(g[key] for g in groups.values())
        for key in ("file_count", "logical_bytes", "allocated_bytes")
    }
    ttl = {
        "raw_default_lake": RawResponseArchive(resolved_root / "data-lake").gc(dry_run=True),
    }
    is_over = max_allocated_bytes is not None and total["allocated_bytes"] > max_allocated_bytes
    return {
        "schema_version": "storage-budget.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "status": "over_budget" if is_over else "reported",
        "max_allocated_bytes": max_allocated_bytes,
        "scopes": {s: str(p) for s, p in scopes},
        "classes": dict(sorted(groups.items())),
        "total": total,
        "ttl_dry_run": ttl,
        "deletion_authorized": False,
        "notes": [
            "Classification is path-based; candidates are not deletion permission.",
            "Allocated totals sum file st_blocks; hardlinks/clones may be counted more than once.",
            "Live monitoring is not an atomic snapshot; quiesce writers for comparisons.",
            "Archives excluded unless --archives-root is provided.",
        ],
    }


def archive_catalog(archives: list[Path], review_date: str) -> dict[str, Any]:
    """Fail closed while the retired command remains in the CLI."""
    del archives, review_date
    raise W0Error("archive catalog has been retired; verify archives with their original tool")


def publish_report(path: Path, report: dict[str, Any]) -> None:
    """Exclusive output: never replace an existing report, data file or symlink."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
