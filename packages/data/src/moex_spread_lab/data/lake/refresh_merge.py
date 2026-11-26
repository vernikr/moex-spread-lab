"""Watermark planning, dataset locks, and legacy/Parquet merge helpers for refresh."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from moex_spread_lab.core.w0 import sha256_file

from ..iss.catalog import CANDLE_INTERVAL_LABELS
from .data_lake import DataLakeError, ParquetBarStore

REFRESH_SCHEMA = "market-data-refresh.v1"


@dataclass(frozen=True)
class RefreshTarget:
    secid: str
    interval: int

    @property
    def timeframe(self) -> str:
        return CANDLE_INTERVAL_LABELS.get(self.interval, f"interval_{self.interval}")

    @property
    def id(self) -> str:
        return f"{self.timeframe}/{self.secid}"


@dataclass(frozen=True)
class TargetState:
    target: RefreshTarget
    legacy_path: Path
    payload: dict[str, Any] | None
    watermark: date | None
    requested_from: date


@dataclass(frozen=True)
class RefreshConfig:
    until: date
    till_date: str
    initial_from: date | None
    output_dir: Path
    cache_dir: Path | None
    board: str
    base_url: str
    overlap_sessions: int
    chunk_days: int
    workers: int
    full_refresh: bool
    raw_ttl_days: int
    pin_raw: bool
    timeout: float
    delay_seconds: float
    retries: int
    fetcher: Callable[..., dict[str, Any]]


class DataRefreshError(DataLakeError):
    """Raised when an incremental candle refresh cannot preserve the lake contract."""


def parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DataRefreshError(f"{label} must be an ISO date: {value!r}") from exc


def timestamp_date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _read_legacy_payload(path: Path, target: RefreshTarget) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        decoded: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataRefreshError(f"cannot read existing candle series {path}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise DataRefreshError(f"existing candle file is not iss-candles.v1: {path}")
    payload = cast("dict[str, Any]", decoded)
    if payload.get("schema_version") != "iss-candles.v1":
        raise DataRefreshError(f"existing candle file is not iss-candles.v1: {path}")
    if payload.get("secid") != target.secid or payload.get("interval") != target.interval:
        raise DataRefreshError(
            f"existing candle file does not match refresh target {target.id}: {path}"
        )
    if not isinstance(payload.get("rows"), list):
        raise DataRefreshError(f"existing candle file rows are invalid: {path}")
    return payload


def _payload_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the object rows of a legacy payload, ignoring non-object entries."""
    rows: list[dict[str, Any]] = [row for row in payload.get("rows", []) if isinstance(row, dict)]
    return rows


def _payload_watermark(payload: dict[str, Any] | None) -> date | None:
    if not payload:
        return None
    dates = [timestamp_date(row.get("begin")) for row in _payload_rows(payload)]
    return max((item for item in dates if item is not None), default=None)


def _overlap_start(payload: dict[str, Any] | None, watermark: date, overlap_sessions: int) -> date:
    """Return the Nth preceding observed session date, not a weekday heuristic."""
    if payload:
        sessions = sorted(
            {
                day
                for row in _payload_rows(payload)
                if (day := timestamp_date(row.get("begin"))) is not None and day <= watermark
            }
        )
        if sessions:
            return sessions[max(0, len(sessions) - 1 - overlap_sessions)]
    return watermark - timedelta(days=overlap_sessions * 2)


def chunks(start: date, end: date, chunk_days: int) -> list[tuple[date, date]]:
    if chunk_days < 1:
        raise DataRefreshError("chunk_days must be positive")
    result: list[tuple[date, date]] = []
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=chunk_days - 1), end)
        result.append((current, chunk_end))
        current = chunk_end + timedelta(days=1)
    return result


def _target_path(output_dir: Path, target: RefreshTarget) -> Path:
    return output_dir / f"{target.secid}_{target.timeframe}.json"


def _resolve_target_watermark(
    store: ParquetBarStore, legacy_path: Path, payload: dict[str, Any] | None
) -> date | None:
    watermark = _payload_watermark(payload)
    try:
        record = store.record_for_legacy_path(legacy_path)
    except DataLakeError:
        record = None
    if record and legacy_path.is_file() and sha256_file(legacy_path) == record.get("legacy_sha256"):
        record_date = timestamp_date(record.get("last_begin"))
        if record_date is not None:
            return record_date
    return watermark


def _resolve_requested_from(
    cfg: RefreshConfig,
    target: RefreshTarget,
    payload: dict[str, Any] | None,
    watermark: date | None,
) -> date:
    existing_from = None
    if payload and (raw_from := payload.get("from")) is not None:
        existing_from = parse_date(str(raw_from), "existing payload from")
    if cfg.full_refresh:
        requested_from = cfg.initial_from or existing_from
    elif watermark is not None:
        requested_from = _overlap_start(payload, watermark, cfg.overlap_sessions)
    else:
        requested_from = cfg.initial_from
    if requested_from is None:
        raise DataRefreshError(f"from_date is required for the first refresh of {target.id}")
    if requested_from > cfg.until:
        raise DataRefreshError(f"refresh range is invalid for {target.id}")
    return requested_from


def plan_single_target(
    target: RefreshTarget, store: ParquetBarStore, cfg: RefreshConfig
) -> tuple[TargetState | None, dict[str, Any] | None]:
    if not target.secid or target.interval <= 0:
        raise DataRefreshError(f"invalid refresh target: {target}")
    legacy_path = _target_path(cfg.output_dir, target)
    payload = _read_legacy_payload(legacy_path, target)
    watermark = _resolve_target_watermark(store, legacy_path, payload)
    if not cfg.full_refresh and watermark is not None and watermark >= cfg.until:
        return None, {
            "dataset_id": target.id,
            "status": "up_to_date",
            "watermark": watermark.isoformat(),
            "requested_till": cfg.till_date,
            "network_requests": 0,
        }
    requested_from = _resolve_requested_from(cfg, target, payload, watermark)
    return TargetState(target, legacy_path, payload, watermark, requested_from), None


def build_merged_payload(
    state: TargetState,
    cfg: RefreshConfig,
    *,
    rows: list[dict[str, Any]],
    page_count: int,
    chunk_count: int,
    raw_entry_count: int,
) -> dict[str, Any]:
    existing_from = (
        parse_date(state.payload["from"], "existing payload from")
        if state.payload and isinstance(state.payload.get("from"), str)
        else None
    )
    first_from = (
        min(existing_from, state.requested_from)
        if existing_from is not None
        else state.requested_from
    )
    payload = dict(state.payload or {})
    payload.update(
        {
            "schema_version": "iss-candles.v1",
            "secid": state.target.secid,
            "board": cfg.board,
            "interval": state.target.interval,
            "timeframe": state.target.timeframe,
            "from": first_from.isoformat(),
            "till": cfg.till_date,
            "page_count": page_count,
            "raw_row_count": len(rows),
            "row_count": len(rows),
            "duplicate_count": 0,
            "coverage_status": "available",
            "price_interpretation": "candle_price_proxy_for_medium_term_analysis",
            "rows": rows,
            "refresh": {
                "schema_version": REFRESH_SCHEMA,
                "refreshed_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "watermark_before": state.watermark.isoformat() if state.watermark else None,
                "requested_from": state.requested_from.isoformat(),
                "requested_till": cfg.till_date,
                "overlap_sessions": cfg.overlap_sessions,
                "chunk_count": chunk_count,
                "raw_entry_count": raw_entry_count,
            },
        }
    )
    return payload
