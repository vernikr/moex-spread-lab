"""Read-only inventory and retention classification for local market data."""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from moex_spread_lab.core.w0 import canonical_bytes, sha256_bytes, sha256_file

from .inventory_policy import (
    DATA_INVENTORY_SCHEMA,
    _inspect_file,
    _path_timeframe,
    _Policy,
    _policy,
)

_CANDIDATE_CLASSES = frozenset(
    {"rebuildable_cache", "archive_candidate", "derived_archive_candidate"}
)


def _iter_files(data_root: Path) -> Iterator[Path]:
    for base, directories, names in os.walk(data_root, followlinks=False):
        directories[:] = sorted(
            name for name in directories if not (Path(base) / name).is_symlink()
        )
        for name in sorted(names):
            path = Path(base) / name
            if path.is_symlink() or not path.is_file():
                continue
            yield path


def _allocated_bytes(stat_result: os.stat_result) -> int:
    blocks = getattr(stat_result, "st_blocks", 0)
    allocated = int(blocks) * 512
    return allocated if allocated > 0 else int(stat_result.st_size)


def _empty_summary(data_root_exists: bool) -> dict[str, Any]:
    return {
        "data_root_exists": data_root_exists,
        "file_count": 0,
        "logical_bytes": 0,
        "allocated_bytes": 0,
        "by_classification": {},
        "candidate_reclaimable_bytes": 0,
        "candidate_reclaimable_requires_review": True,
    }


def _scan_inventory_files(
    data_root: Path, *, include_hashes: bool, inspect_json: bool
) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in _iter_files(data_root):
        relative = path.relative_to(data_root).as_posix()
        stat_result = path.stat()
        metadata = _inspect_file(path) if inspect_json else {}
        policy = _policy(relative, metadata)
        entry: dict[str, Any] = {
            "path": relative,
            "dataset": policy.dataset,
            "classification": policy.classification,
            "retention": policy.retention,
            "consumers": list(policy.consumers),
            "rationale": policy.rationale,
            "logical_bytes": int(stat_result.st_size),
            "allocated_bytes": _allocated_bytes(stat_result),
            "mtime_ns": int(stat_result.st_mtime_ns),
        }
        if metadata:
            entry["metadata"] = metadata
        if include_hashes:
            entry["sha256"] = sha256_file(path)
        files.append(entry)
    return files


def _collect_entry_bounds(metadata: dict[str, Any], starts: list[str], ends: list[str]) -> None:
    time_range = metadata.get("time_range")
    if not isinstance(time_range, dict):
        return
    if isinstance(time_range.get("from"), str):
        starts.append(time_range["from"])
    if isinstance(time_range.get("till"), str):
        ends.append(time_range["till"])


def _aggregate_dataset_metadata(
    entries: list[dict[str, Any]],
) -> tuple[list[str], int | None, dict[str, str] | None]:
    schemas: set[str] = set()
    starts: list[str] = []
    ends: list[str] = []
    row_count = 0
    row_count_known = False
    for entry in entries:
        metadata = entry.get("metadata", {})
        schema = metadata.get("schema_version")
        if isinstance(schema, str):
            schemas.add(schema)
        if isinstance(metadata.get("row_count"), int):
            row_count += int(metadata["row_count"])
            row_count_known = True
        _collect_entry_bounds(metadata, starts, ends)
    merged_range: dict[str, str] | None = None
    if starts or ends:
        merged_range = {
            **({"from": min(starts)} if starts else {}),
            **({"till": max(ends)} if ends else {}),
        }
    return sorted(schemas), (row_count if row_count_known else None), merged_range


def _summarize_dataset_group(
    dataset: str, entries: list[dict[str, Any]], *, include_hashes: bool
) -> dict[str, Any]:
    first = entries[0]
    schemas, row_count, time_range = _aggregate_dataset_metadata(entries)
    item: dict[str, Any] = {
        "dataset": dataset,
        "classification": first["classification"],
        "retention": first["retention"],
        "consumers": first["consumers"],
        "rationale": first["rationale"],
        "file_count": len(entries),
        "logical_bytes": sum(int(entry["logical_bytes"]) for entry in entries),
        "allocated_bytes": sum(int(entry["allocated_bytes"]) for entry in entries),
    }
    if schemas:
        item["schema_versions"] = schemas
    if row_count is not None:
        item["row_count"] = row_count
    if time_range is not None:
        item["time_range"] = time_range
    if include_hashes:
        item["content_sha256"] = sha256_bytes(
            canonical_bytes(
                [{"path": entry["path"], "sha256": entry["sha256"]} for entry in entries]
            )
        )
    return item


def _build_inventory_summary(files: list[dict[str, Any]]) -> dict[str, Any]:
    classification_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in files:
        classification_groups[str(entry["classification"])].append(entry)
    by_classification = {
        classification: {
            "file_count": len(entries),
            "logical_bytes": sum(int(entry["logical_bytes"]) for entry in entries),
            "allocated_bytes": sum(int(entry["allocated_bytes"]) for entry in entries),
        }
        for classification, entries in sorted(classification_groups.items())
    }
    return {
        "data_root_exists": True,
        "file_count": len(files),
        "logical_bytes": sum(int(entry["logical_bytes"]) for entry in files),
        "allocated_bytes": sum(int(entry["allocated_bytes"]) for entry in files),
        "by_classification": by_classification,
        "candidate_reclaimable_bytes": sum(
            int(entry["allocated_bytes"])
            for entry in files
            if entry["classification"] in _CANDIDATE_CLASSES
        ),
        "candidate_reclaimable_requires_review": True,
    }


def build_data_inventory(
    root: Path,
    *,
    include_hashes: bool = False,
    inspect_json: bool = True,
) -> dict[str, Any]:
    """Return a deterministic local-data inventory without modifying the project."""
    resolved_root = root.resolve()
    data_root = resolved_root / "data"
    created_at = datetime.now(UTC).isoformat(timespec="seconds")
    if not data_root.is_dir():
        return {
            "schema_version": DATA_INVENTORY_SCHEMA,
            "created_at": created_at,
            "root": str(resolved_root),
            "data_root": str(data_root),
            "hashes_included": include_hashes,
            "json_inspection": inspect_json,
            "datasets": [],
            "files": [],
            "summary": _empty_summary(False),
        }
    files = _scan_inventory_files(
        data_root, include_hashes=include_hashes, inspect_json=inspect_json
    )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in files:
        grouped[str(entry["dataset"])].append(entry)
    datasets = [
        _summarize_dataset_group(dataset, entries, include_hashes=include_hashes)
        for dataset, entries in sorted(grouped.items())
    ]
    return {
        "schema_version": DATA_INVENTORY_SCHEMA,
        "created_at": created_at,
        "root": str(resolved_root),
        "data_root": str(data_root),
        "hashes_included": include_hashes,
        "json_inspection": inspect_json,
        "datasets": datasets,
        "files": files,
        "summary": _build_inventory_summary(files),
    }


__all__ = [
    "DATA_INVENTORY_SCHEMA",
    "_Policy",
    "_inspect_file",
    "_path_timeframe",
    "_policy",
    "build_data_inventory",
]
