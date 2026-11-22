"""Read-only inventory and retention classification for local market data."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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


def _path_timeframe(path: str, metadata: dict[str, Any]) -> str | None:
    timeframe = metadata.get("timeframe")
    if isinstance(timeframe, str) and timeframe:
        return timeframe
    stem = Path(path).stem
    for suffix in _TIMEFRAME_SUFFIXES:
        if stem.endswith(f"_{suffix}"):
            return suffix
    return None


def _policy(path: str, metadata: dict[str, Any]) -> _Policy:
    """Classify a path without making a deletion decision."""
    if path == "w1/current-intake.json":
        return _Policy(
            "w1/current-intake",
            "active_input",
            "keep",
            ("pair screen", "calendar proof", "contract terms"),
            "Current local contract snapshot used by offline readers.",
        )
    if path == "w1/historical-contracts.json":
        return _Policy(
            "w1/historical-contracts",
            "active_input",
            "keep",
            ("historical pair proof",),
            "Fallback terms for historical contracts are hashed into proof provenance.",
        )
    if path.startswith("w1/candles-screen/"):
        return _Policy(
            "w1/candles-screen",
            "active_input",
            "keep",
            ("pair screen", "hourly calendar proof"),
            "The screener gives screened candle inputs priority over intake candles.",
        )
    if path.startswith("w1/candles/"):
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
    if path.startswith("w1/candles-cache/"):
        return _Policy(
            "w1/candles-cache",
            "rebuildable_cache",
            "ttl_or_rebuild",
            ("iss-candles intake",),
            "Paginated normalized ISS pages avoid refetching but are not read by the offline screener.",
        )
    if path.startswith("w1/candles-screen-cache/"):
        return _Policy(
            "w1/candles-screen-cache",
            "rebuildable_cache",
            "ttl_or_rebuild",
            ("screen candle intake",),
            "Paginated intake cache; final screened candles remain separate.",
        )
    if path.startswith("w1/candles-cache-common/"):
        return _Policy(
            "w1/candles-cache-common",
            "rebuildable_cache",
            "ttl_or_rebuild",
            ("legacy common-candle intake",),
            "Rebuildable page cache for an unregistered experimental timeframe.",
        )
    if path.startswith("w1/candles-common/"):
        return _Policy(
            "w1/candles-common",
            "archive_candidate",
            "pin_or_archive",
            ("legacy minute diagnostics",),
            "No direct runtime reader consumes this experimental common-candle directory.",
        )
    if path.startswith("w1/candleborders-cache/"):
        return _Policy(
            "w1/candleborders-cache",
            "rebuildable_cache",
            "ttl_or_rebuild",
            ("timeframe inventory intake",),
            "Rebuildable ISS availability probes.",
        )
    if path.startswith("w1/perpetual-history-discovery/"):
        return _Policy(
            "w1/perpetual-history-discovery",
            "archive_candidate",
            "pin_or_archive",
            ("historical-pair discovery reproduction",),
            "Discovery artifact; active selectors read copied candle inputs instead.",
        )
    if path.startswith("spread-history-cache/"):
        return _Policy(
            "spread-history-cache",
            "active_input",
            "keep_while_calendar_research_active",
            ("calendar proof", "calendar study"),
            "CalendarData reads this local history in offline mode.",
        )
    if path.startswith("carry-history-cache/") or path.startswith("carry-screen-history-cache/"):
        return _Policy(
            path.split("/", 1)[0],
            "active_input",
            "keep_while_funding_research_active",
            ("funding proof", "funding screen"),
            "Offline funding readers use these cached FORTS EOD histories.",
        )
    if path.startswith("benchmark/") or path.startswith("benchmark-cache/"):
        return _Policy(
            path.split("/", 1)[0],
            "active_input",
            "keep",
            ("LQDT benchmark", "calendar proof", "pair screen"),
            "Local benchmark lineage and supporting inputs are used by offline calculations.",
        )
    if path.startswith("proof-funding-verified/"):
        return _Policy(
            "proof-funding-verified",
            "active_input",
            "keep",
            ("funding proof",),
            "Supplementary published funding evidence can be used by the proof overlay.",
        )
    if path.startswith("analytics/"):
        name = Path(path).name
        if name in _RUNTIME_ANALYTICS:
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
    return _Policy(
        path.split("/", 1)[0] if "/" in path else "root",
        "unclassified",
        "review",
        (),
        "No explicit consumer policy is registered for this local data path.",
    )


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
        values: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            for timestamp_key in ("begin", "timestamp", "date"):
                value = row.get(timestamp_key)
                if isinstance(value, str) and value:
                    values.append(value)
                    break
        if values:
            return {"from": min(values), "till": max(values)}
    return None


def _inspect_file(path: Path) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if path.suffix == ".json":
        try:
            payload = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {"inspection_error": type(exc).__name__}
        if not isinstance(payload, dict):
            return {"json_root": type(payload).__name__}
        schema = payload.get("schema_version")
        if isinstance(schema, str):
            metadata["schema_version"] = schema
        timeframe = payload.get("timeframe")
        if isinstance(timeframe, str):
            metadata["timeframe"] = timeframe
        row_count = payload.get("row_count")
        if not isinstance(row_count, int):
            for key in ("rows", "daily", "trades", "records"):
                value = payload.get(key)
                if isinstance(value, list):
                    row_count = len(value)
                    break
        if isinstance(row_count, int) and row_count >= 0:
            metadata["row_count"] = row_count
        time_range = _json_time_range(payload)
        if time_range is not None:
            metadata["time_range"] = time_range
        source = payload.get("source")
        if isinstance(source, dict):
            metadata["source_metadata_keys"] = sorted(str(key) for key in source)
        return metadata
    if path.suffix == ".jsonl":
        try:
            with path.open("rb") as handle:
                metadata["record_count"] = sum(1 for line in handle if line.strip())
        except OSError as exc:
            metadata["inspection_error"] = type(exc).__name__
    return metadata


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

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in files:
        grouped[entry["dataset"]].append(entry)
    datasets: list[dict[str, Any]] = []
    for dataset, entries in sorted(grouped.items()):
        first = entries[0]
        schemas = sorted(
            {
                entry.get("metadata", {}).get("schema_version")
                for entry in entries
                if isinstance(entry.get("metadata", {}).get("schema_version"), str)
            }
        )
        starts: list[str] = []
        ends: list[str] = []
        row_count = 0
        row_count_known = False
        for entry in entries:
            metadata = entry.get("metadata", {})
            if isinstance(metadata.get("row_count"), int):
                row_count += metadata["row_count"]
                row_count_known = True
            time_range = metadata.get("time_range")
            if isinstance(time_range, dict):
                if isinstance(time_range.get("from"), str):
                    starts.append(time_range["from"])
                if isinstance(time_range.get("till"), str):
                    ends.append(time_range["till"])
        item: dict[str, Any] = {
            "dataset": dataset,
            "classification": first["classification"],
            "retention": first["retention"],
            "consumers": first["consumers"],
            "rationale": first["rationale"],
            "file_count": len(entries),
            "logical_bytes": sum(entry["logical_bytes"] for entry in entries),
            "allocated_bytes": sum(entry["allocated_bytes"] for entry in entries),
        }
        if schemas:
            item["schema_versions"] = schemas
        if row_count_known:
            item["row_count"] = row_count
        if starts or ends:
            item["time_range"] = {
                **({"from": min(starts)} if starts else {}),
                **({"till": max(ends)} if ends else {}),
            }
        if include_hashes:
            item["content_sha256"] = sha256_bytes(
                canonical_bytes(
                    [{"path": entry["path"], "sha256": entry["sha256"]} for entry in entries]
                )
            )
        datasets.append(item)

    classification_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in files:
        classification_groups[entry["classification"]].append(entry)
    by_classification = {
        classification: {
            "file_count": len(entries),
            "logical_bytes": sum(entry["logical_bytes"] for entry in entries),
            "allocated_bytes": sum(entry["allocated_bytes"] for entry in entries),
        }
        for classification, entries in sorted(classification_groups.items())
    }
    candidate_classes = {"rebuildable_cache", "archive_candidate", "derived_archive_candidate"}
    summary = {
        "data_root_exists": True,
        "file_count": len(files),
        "logical_bytes": sum(entry["logical_bytes"] for entry in files),
        "allocated_bytes": sum(entry["allocated_bytes"] for entry in files),
        "by_classification": by_classification,
        "candidate_reclaimable_bytes": sum(
            entry["allocated_bytes"]
            for entry in files
            if entry["classification"] in candidate_classes
        ),
        "candidate_reclaimable_requires_review": True,
    }
    return {
        "schema_version": DATA_INVENTORY_SCHEMA,
        "created_at": created_at,
        "root": str(resolved_root),
        "data_root": str(data_root),
        "hashes_included": include_hashes,
        "json_inspection": inspect_json,
        "datasets": datasets,
        "files": files,
        "summary": summary,
    }
