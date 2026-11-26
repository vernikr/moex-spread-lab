"""Read-only inventory and retention classification for local market data."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from moex_spread_lab.core.w0 import canonical_bytes, sha256_bytes, sha256_file

DATA_INVENTORY_SCHEMA = "data-inventory.v1"
_ACTIVE_TIMEFRAMES = frozenset(("1d", "1h"))
_TIMEFRAME_SUFFIXES = ("1m", "10m", "1h", "1d")
_RUNTIME_ANALYTICS = frozenset(
    (
        "research-settings.json",
        "screener-runs.jsonl",
        "screener-screens.jsonl",
        "screener-searches.jsonl",
        "proof-searches.jsonl",
    )
)


@dataclass(frozen=True)
class _Policy:
    dataset: str
    classification: str
    retention: str
    consumers: tuple[str, ...]
    rationale: str


_EXACT_POLICIES: dict[str, _Policy] = {
    "w1/current-intake.json": _Policy(
        "w1/current-intake",
        "active_input",
        "keep",
        ("pair screen", "calendar proof", "contract terms"),
        "Current local contract snapshot used by offline readers.",
    ),
    "w1/historical-contracts.json": _Policy(
        "w1/historical-contracts",
        "active_input",
        "keep",
        ("historical pair proof",),
        "Fallback terms for historical contracts are hashed into proof provenance.",
    ),
}

_PREFIX_POLICIES: tuple[_Policy, ...] = (
    _Policy(
        "w1/candles-screen",
        "active_input",
        "keep",
        ("pair screen", "hourly calendar proof"),
        "The screener gives screened candle inputs priority over intake candles.",
    ),
    _Policy(
        "w1/candles-cache",
        "rebuildable_cache",
        "ttl_or_rebuild",
        ("iss-candles intake",),
        "Paginated normalized ISS pages avoid refetching but are not read by the offline screener.",
    ),
    _Policy(
        "w1/candles-screen-cache",
        "rebuildable_cache",
        "ttl_or_rebuild",
        ("screen candle intake",),
        "Paginated intake cache; final screened candles remain separate.",
    ),
    _Policy(
        "w1/candles-cache-common",
        "rebuildable_cache",
        "ttl_or_rebuild",
        ("legacy common-candle intake",),
        "Rebuildable page cache for an unregistered experimental timeframe.",
    ),
    _Policy(
        "w1/candles-common",
        "archive_candidate",
        "pin_or_archive",
        ("legacy minute diagnostics",),
        "No direct runtime reader consumes this experimental common-candle directory.",
    ),
    _Policy(
        "w1/candleborders-cache",
        "rebuildable_cache",
        "ttl_or_rebuild",
        ("timeframe inventory intake",),
        "Rebuildable ISS availability probes.",
    ),
    _Policy(
        "w1/perpetual-history-discovery",
        "archive_candidate",
        "pin_or_archive",
        ("historical-pair discovery reproduction",),
        "Discovery artifact; active selectors read copied candle inputs instead.",
    ),
    _Policy(
        "spread-history-cache",
        "active_input",
        "keep_while_calendar_research_active",
        ("calendar proof", "calendar study"),
        "CalendarData reads this local history in offline mode.",
    ),
    _Policy(
        "proof-funding-verified",
        "active_input",
        "keep",
        ("funding proof",),
        "Supplementary published funding evidence can be used by the proof overlay.",
    ),
)


def _path_timeframe(path: str, metadata: dict[str, Any]) -> str | None:
    timeframe = metadata.get("timeframe")
    if isinstance(timeframe, str) and timeframe:
        return timeframe
    stem = Path(path).stem
    for suffix in _TIMEFRAME_SUFFIXES:
        if stem.endswith(f"_{suffix}"):
            return suffix
    return None


def _candles_policy(path: str, metadata: dict[str, Any]) -> _Policy:
    timeframe = _path_timeframe(path, metadata)
    if timeframe in _ACTIVE_TIMEFRAMES:
        return _Policy(
            f"w1/candles/{timeframe}",
            "active_input",
            "keep",
            ("pair screen", "hourly calendar proof"),
            "Registered runtime timeframe in timeframes.py.",
        )
    return _Policy(
        f"w1/candles/{timeframe or 'unknown'}",
        "archive_candidate",
        "pin_or_archive",
        ("legacy analytical reproduction",),
        "The current runtime registry exposes only 1d and 1h candle timeframes.",
    )


def _analytics_policy(path: str) -> _Policy:
    if Path(path).name in _RUNTIME_ANALYTICS:
        return _Policy(
            "analytics/runtime",
            "derived_runtime",
            "keep_with_rotation",
            ("screener settings", "local run history"),
            "Small settings and local UI histories are read by the running application.",
        )
    return _Policy(
        "analytics/derived",
        "derived_archive_candidate",
        "summary_then_archive",
        ("analytical reproduction",),
        "Generated research result; retain its manifest and summary before archiving detail.",
    )


def _dynamic_prefix_policy(path: str) -> _Policy | None:
    if path.startswith(("carry-history-cache/", "carry-screen-history-cache/")):
        return _Policy(
            path.split("/", 1)[0],
            "active_input",
            "keep_while_funding_research_active",
            ("funding proof", "funding screen"),
            "Offline funding readers use these cached FORTS EOD histories.",
        )
    if path.startswith(("benchmark/", "benchmark-cache/")):
        return _Policy(
            path.split("/", 1)[0],
            "active_input",
            "keep",
            ("LQDT benchmark", "calendar proof", "pair screen"),
            "Local benchmark lineage and supporting inputs are used by offline calculations.",
        )
    return None


def _policy(path: str, metadata: dict[str, Any]) -> _Policy:
    """Classify a path without making a deletion decision."""
    exact = _EXACT_POLICIES.get(path)
    if exact is not None:
        return exact
    if path.startswith("w1/candles/"):
        return _candles_policy(path, metadata)
    if path.startswith("analytics/"):
        return _analytics_policy(path)
    for policy_item in _PREFIX_POLICIES:
        if path.startswith(policy_item.dataset + "/"):
            return policy_item
    dynamic = _dynamic_prefix_policy(path)
    if dynamic is not None:
        return dynamic
    return _Policy(
        path.split("/", 1)[0] if "/" in path else "root",
        "unclassified",
        "review",
        (),
        "No explicit consumer policy is registered for this local data path.",
    )


def _extract_rows_timestamps(rows: list[Any]) -> list[str]:
    values: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        typed_row = cast(dict[str, Any], row)
        for timestamp_key in ("begin", "timestamp", "date"):
            value = typed_row.get(timestamp_key)
            if isinstance(value, str) and value:
                values.append(value)
                break
    return values


def _json_time_range(payload: dict[str, Any]) -> dict[str, str] | None:
    start = payload.get("from")
    end = payload.get("till")
    if isinstance(start, str) or isinstance(end, str):
        return {
            key: value for key, value in (("from", start), ("till", end)) if isinstance(value, str)
        }
    for key in ("rows", "daily"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        values = _extract_rows_timestamps(cast(list[Any], rows))
        if values:
            return {"from": min(values), "till": max(values)}
    return None


def _resolve_payload_row_count(payload: dict[str, Any]) -> int | None:
    row_count = payload.get("row_count")
    if isinstance(row_count, int):
        return row_count if row_count >= 0 else None
    for key in ("rows", "daily", "trades", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(cast(list[Any], value))
    return None


def _inspect_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"inspection_error": type(exc).__name__}
    if not isinstance(payload, dict):
        return {"json_root": type(payload).__name__}
    data = cast(dict[str, Any], payload)
    metadata = {
        key: data[key] for key in ("schema_version", "timeframe") if isinstance(data.get(key), str)
    }
    row_count = _resolve_payload_row_count(data)
    if row_count is not None:
        metadata["row_count"] = row_count
    time_range = _json_time_range(data)
    if time_range is not None:
        metadata["time_range"] = time_range
    source = data.get("source")
    if isinstance(source, dict):
        source_metadata = cast(dict[str, Any], source)
        metadata["source_metadata_keys"] = sorted(str(key) for key in source_metadata)
    return metadata


def _inspect_file(path: Path) -> dict[str, Any]:
    if path.suffix == ".json":
        return _inspect_json_file(path)
    if path.suffix == ".jsonl":
        try:
            with path.open("rb") as handle:
                return {"record_count": sum(1 for line in handle if line.strip())}
        except OSError as exc:
            return {"inspection_error": type(exc).__name__}
    return {}


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
    bounds = cast("dict[str, Any]", time_range)
    window_start = bounds.get("from")
    if isinstance(window_start, str):
        starts.append(window_start)
    window_end = bounds.get("till")
    if isinstance(window_end, str):
        ends.append(window_end)


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


def _file_totals(entries: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "file_count": len(entries),
        "logical_bytes": sum(int(entry["logical_bytes"]) for entry in entries),
        "allocated_bytes": sum(int(entry["allocated_bytes"]) for entry in entries),
    }


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
        **_file_totals(entries),
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


def _build_inventory_summary(files: list[dict[str, Any]], exists: bool) -> dict[str, Any]:
    classification_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in files:
        classification_groups[str(entry["classification"])].append(entry)
    return {
        "data_root_exists": exists,
        **_file_totals(files),
        "by_classification": {
            classification: _file_totals(entries)
            for classification, entries in sorted(classification_groups.items())
        },
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
    exists = data_root.is_dir()
    files = (
        _scan_inventory_files(data_root, include_hashes=include_hashes, inspect_json=inspect_json)
        if exists
        else []
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
        "summary": _build_inventory_summary(files, exists),
    }


__all__ = [
    "DATA_INVENTORY_SCHEMA",
    "_Policy",
    "_inspect_file",
    "_path_timeframe",
    "_policy",
    "build_data_inventory",
]
