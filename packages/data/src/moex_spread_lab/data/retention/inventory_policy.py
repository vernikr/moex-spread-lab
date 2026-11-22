"""Retention policy table and JSON/JSONL file metadata inspection for inventory."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

_PREFIX_POLICIES: tuple[tuple[str, _Policy], ...] = (
    (
        "w1/candles-screen/",
        _Policy(
            "w1/candles-screen",
            "active_input",
            "keep",
            ("pair screen", "hourly calendar proof"),
            "The screener gives screened candle inputs priority over intake candles.",
        ),
    ),
    (
        "w1/candles-cache/",
        _Policy(
            "w1/candles-cache",
            "rebuildable_cache",
            "ttl_or_rebuild",
            ("iss-candles intake",),
            "Paginated normalized ISS pages avoid refetching but are not read by the offline screener.",
        ),
    ),
    (
        "w1/candles-screen-cache/",
        _Policy(
            "w1/candles-screen-cache",
            "rebuildable_cache",
            "ttl_or_rebuild",
            ("screen candle intake",),
            "Paginated intake cache; final screened candles remain separate.",
        ),
    ),
    (
        "w1/candles-cache-common/",
        _Policy(
            "w1/candles-cache-common",
            "rebuildable_cache",
            "ttl_or_rebuild",
            ("legacy common-candle intake",),
            "Rebuildable page cache for an unregistered experimental timeframe.",
        ),
    ),
    (
        "w1/candles-common/",
        _Policy(
            "w1/candles-common",
            "archive_candidate",
            "pin_or_archive",
            ("legacy minute diagnostics",),
            "No direct runtime reader consumes this experimental common-candle directory.",
        ),
    ),
    (
        "w1/candleborders-cache/",
        _Policy(
            "w1/candleborders-cache",
            "rebuildable_cache",
            "ttl_or_rebuild",
            ("timeframe inventory intake",),
            "Rebuildable ISS availability probes.",
        ),
    ),
    (
        "w1/perpetual-history-discovery/",
        _Policy(
            "w1/perpetual-history-discovery",
            "archive_candidate",
            "pin_or_archive",
            ("historical-pair discovery reproduction",),
            "Discovery artifact; active selectors read copied candle inputs instead.",
        ),
    ),
    (
        "spread-history-cache/",
        _Policy(
            "spread-history-cache",
            "active_input",
            "keep_while_calendar_research_active",
            ("calendar proof", "calendar study"),
            "CalendarData reads this local history in offline mode.",
        ),
    ),
    (
        "proof-funding-verified/",
        _Policy(
            "proof-funding-verified",
            "active_input",
            "keep",
            ("funding proof",),
            "Supplementary published funding evidence can be used by the proof overlay.",
        ),
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
    for prefix, policy_item in _PREFIX_POLICIES:
        if path.startswith(prefix):
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
        for timestamp_key in ("begin", "timestamp", "date"):
            value = row.get(timestamp_key)
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
        values = _extract_rows_timestamps(rows)
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
            return len(value)
    return None


def _inspect_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"inspection_error": type(exc).__name__}
    if not isinstance(payload, dict):
        return {"json_root": type(payload).__name__}
    metadata: dict[str, Any] = {}
    if isinstance(payload.get("schema_version"), str):
        metadata["schema_version"] = payload["schema_version"]
    if isinstance(payload.get("timeframe"), str):
        metadata["timeframe"] = payload["timeframe"]
    row_count = _resolve_payload_row_count(payload)
    if row_count is not None:
        metadata["row_count"] = row_count
    time_range = _json_time_range(payload)
    if time_range is not None:
        metadata["time_range"] = time_range
    source = payload.get("source")
    if isinstance(source, dict):
        metadata["source_metadata_keys"] = sorted(str(key) for key in source)
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
