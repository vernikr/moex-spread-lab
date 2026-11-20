"""Bounded, watermark-driven candle refresh into the verified local data lake."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import ExitStack, contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import fcntl
from pathlib import Path
from typing import Any, Callable

from .data_lake import DataLakeError, ParquetBarStore
from .iss import CANDLE_INTERVAL_LABELS, DEFAULT_BOARD, DEFAULT_ISS_BASE_URL, fetch_candle_series
from .w0 import sha256_file


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


@dataclass
class _TargetState:
    target: RefreshTarget
    legacy_path: Path
    payload: dict[str, Any] | None
    watermark: date | None
    requested_from: date | None


class DataRefreshError(DataLakeError):
    """Raised when an incremental candle refresh cannot preserve the lake contract."""


def _date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DataRefreshError(f"{label} must be an ISO date: {value!r}") from exc


def _timestamp_date(value: Any) -> date | None:
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
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataRefreshError(f"cannot read existing candle series {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "iss-candles.v1":
        raise DataRefreshError(f"existing candle file is not iss-candles.v1: {path}")
    if payload.get("secid") != target.secid or payload.get("interval") != target.interval:
        raise DataRefreshError(f"existing candle file does not match refresh target {target.id}: {path}")
    if not isinstance(payload.get("rows"), list):
        raise DataRefreshError(f"existing candle file rows are invalid: {path}")
    return payload


def _payload_watermark(payload: dict[str, Any] | None) -> date | None:
    if not payload:
        return None
    dates = [_timestamp_date(row.get("begin")) for row in payload.get("rows", []) if isinstance(row, dict)]
    return max((item for item in dates if item is not None), default=None)


def _overlap_start(payload: dict[str, Any] | None, watermark: date, overlap_sessions: int) -> date:
    """Return the Nth preceding observed session date, not a weekday heuristic."""
    if payload:
        sessions = sorted({
            item
            for row in payload.get("rows", [])
            if isinstance(row, dict)
            for item in [_timestamp_date(row.get("begin"))]
            if item is not None and item <= watermark
        })
        if sessions:
            return sessions[max(0, len(sessions) - 1 - overlap_sessions)]
    # A retained compatibility series should normally supply the observed dates.
    # Keep a conservative date fallback for a malformed/partial external import.
    return watermark - timedelta(days=overlap_sessions * 2)


@contextmanager
def _dataset_writer_lock(lake_root: Path, target: RefreshTarget):
    # Serialize a dataset from watermark read through verified publication.
    lock_name = hashlib.sha256(target.id.encode("utf-8")).hexdigest() + ".lock"
    lock_path = lake_root / "locks" / lock_name
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _chunks(start: date, end: date, chunk_days: int) -> list[tuple[date, date]]:
    if chunk_days < 1:
        raise DataRefreshError("chunk_days must be positive")
    result = []
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=chunk_days - 1), end)
        result.append((current, chunk_end))
        current = chunk_end + timedelta(days=1)
    return result


def _target_path(output_dir: Path, target: RefreshTarget) -> Path:
    return output_dir / f"{target.secid}_{target.timeframe}.json"


def refresh_candle_datasets(
    *,
    root: Path,
    targets: list[RefreshTarget],
    till_date: str,
    from_date: str | None = None,
    output_dir: Path = Path("data/w1/candles"),
    lake_root: Path | None = None,
    board: str = DEFAULT_BOARD,
    base_url: str = DEFAULT_ISS_BASE_URL,
    overlap_sessions: int = 5,
    chunk_days: int = 90,
    workers: int = 2,
    full_refresh: bool = False,
    raw_ttl_days: int = 30,
    pin_raw: bool = False,
    timeout: float = 20.0,
    delay_seconds: float = 0.1,
    retries: int = 3,
    cache_dir: Path | None = None,
    fetcher: Callable[..., dict[str, Any]] = fetch_candle_series,
) -> dict[str, Any]:
    """Fetch only ``watermark - overlap`` through ``till_date`` and upsert safely.

    The retained JSON compatibility input is atomically merged first. The Parquet
    lake is then replaced and verified from that exact file, retaining the
    Phase-3 read-path identity contract.
    """
    root = root.resolve()
    from .h1_seal import SealedH1Reader
    if any(target.interval == 60 for target in targets) and SealedH1Reader(root).payload() is not None:
        raise DataRefreshError("deactivate the H1 seal with restored JSON before refreshing H1")
    if not targets:
        raise DataRefreshError("at least one refresh target is required")
    if not 1 <= workers <= 4:
        raise DataRefreshError("refresh workers must be between 1 and 4")
    if overlap_sessions < 0 or raw_ttl_days < 0:
        raise DataRefreshError("overlap_sessions and raw_ttl_days must not be negative")
    until = _date(till_date, "till_date")
    initial_from = _date(from_date, "from_date") if from_date else None
    if initial_from is not None and initial_from > until:
        raise DataRefreshError("from_date must not be after till_date")
    output_dir = output_dir if output_dir.is_absolute() else root / output_dir
    cache_dir = cache_dir if cache_dir is None or cache_dir.is_absolute() else root / cache_dir
    store = ParquetBarStore(root, lake_root)
    unique = {(target.secid, target.interval): target for target in targets}
    # Keep locks throughout planning, network work and publication so a second
    # refresh cannot plan from a stale watermark and overwrite a newer dataset.
    with ExitStack() as locks:
        for target in sorted(unique.values(), key=lambda item: (item.secid, item.interval)):
            locks.enter_context(_dataset_writer_lock(store.lake_root, target))

        states: list[_TargetState] = []
        skipped: list[dict[str, Any]] = []
        for target in sorted(unique.values(), key=lambda item: (item.secid, item.interval)):
            if not target.secid or target.interval <= 0:
                raise DataRefreshError(f"invalid refresh target: {target}")
            legacy_path = _target_path(output_dir, target)
            payload = _read_legacy_payload(legacy_path, target)
            watermark = _payload_watermark(payload)
            try:
                record = store.record_for_legacy_path(legacy_path)
            except DataLakeError:
                record = None
            if record and legacy_path.is_file() and sha256_file(legacy_path) == record.get("legacy_sha256"):
                record_date = _timestamp_date(record.get("last_begin"))
                if record_date is not None:
                    watermark = record_date
            if not full_refresh and watermark is not None and watermark >= until:
                skipped.append({
                    "dataset_id": target.id,
                    "status": "up_to_date",
                    "watermark": watermark.isoformat(),
                    "requested_till": till_date,
                    "network_requests": 0,
                })
                continue
            if full_refresh:
                requested_from = initial_from or _date(payload.get("from"), "existing payload from") if payload else initial_from
            elif watermark is not None:
                requested_from = _overlap_start(payload, watermark, overlap_sessions)
            else:
                requested_from = initial_from
            if requested_from is None:
                raise DataRefreshError(f"from_date is required for the first refresh of {target.id}")
            if requested_from > until:
                raise DataRefreshError(f"refresh range is invalid for {target.id}")
            states.append(_TargetState(target, legacy_path, payload, watermark, requested_from))

        fetched: dict[str, list[tuple[date, dict[str, Any], list[tuple[str, bytes, str, str]]]]] = {
            state.target.id: [] for state in states
        }
        task_count = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for state in states:
                assert state.requested_from is not None
                for chunk_from, chunk_till in _chunks(state.requested_from, until, chunk_days):
                    task_count += 1

                    def run_chunk(
                        state: _TargetState = state,
                        chunk_from: date = chunk_from,
                        chunk_till: date = chunk_till,
                    ) -> tuple[str, date, dict[str, Any], list[tuple[str, bytes, str, str]]]:
                        captured: list[tuple[str, bytes, str, str]] = []
                        def raw_sink(url: str, raw: bytes, received_at: str, raw_sha256: str) -> None:
                            captured.append((url, raw, received_at, raw_sha256))
                        result = fetcher(
                            secid=state.target.secid,
                            interval=state.target.interval,
                            from_date=chunk_from.isoformat(),
                            till_date=chunk_till.isoformat(),
                            base_url=base_url,
                            board=board,
                            timeout=timeout,
                            delay_seconds=delay_seconds,
                            cache_dir=cache_dir,
                            retries=retries,
                            raw_sink=raw_sink,
                        )
                        return state.target.id, chunk_from, result, captured

                    future = executor.submit(run_chunk)
                    futures[future] = state.target.id
            for future in as_completed(futures):
                target_id, chunk_from, result, captured = future.result()
                fetched[target_id].append((chunk_from, result, captured))

        refreshed: list[dict[str, Any]] = []
        raw_archive = store.raw_archive
        for state in states:
            chunks = sorted(fetched[state.target.id], key=lambda item: item[0])
            old_rows = state.payload.get("rows", []) if state.payload else []
            rows_by_begin: dict[str, dict[str, Any]] = {
                row["begin"]: dict(row) for row in old_rows
                if isinstance(row, dict) and isinstance(row.get("begin"), str)
            }
            raw_entries = []
            page_count = 0
            for _, result, captured in chunks:
                if result.get("schema_version") != "iss-candles.v1" or result.get("secid") != state.target.secid:
                    raise DataRefreshError(f"fetcher returned an invalid series for {state.target.id}")
                page_count += int(result.get("page_count", 0))
                for row in result.get("rows", []):
                    if not isinstance(row, dict) or not isinstance(row.get("begin"), str):
                        raise DataRefreshError(f"fetcher returned an invalid candle row for {state.target.id}")
                    rows_by_begin[row["begin"]] = dict(row)
                for url, raw, received_at, raw_sha256 in captured:
                    entry = raw_archive.archive(
                        url=url, raw=raw, received_at=received_at, ttl_days=raw_ttl_days, pin=pin_raw,
                    )
                    if entry["raw_response_sha256"] != raw_sha256:
                        raise DataRefreshError("raw response hash changed before archival")
                    raw_entries.append(entry["id"])
            rows = [rows_by_begin[key] for key in sorted(rows_by_begin)]
            if not rows:
                raise DataRefreshError(f"refresh produced no rows for {state.target.id}")
            existing_from = _date(state.payload["from"], "existing payload from") if state.payload and isinstance(state.payload.get("from"), str) else None
            payload = dict(state.payload or {})
            payload.update({
                "schema_version": "iss-candles.v1",
                "secid": state.target.secid,
                "board": board,
                "interval": state.target.interval,
                "timeframe": state.target.timeframe,
                "from": min(item for item in (existing_from, state.requested_from) if item is not None).isoformat(),
                "till": till_date,
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
                    "requested_from": state.requested_from.isoformat() if state.requested_from else None,
                    "requested_till": till_date,
                    "overlap_sessions": overlap_sessions,
                    "chunk_count": len(chunks),
                    "raw_entry_count": len(raw_entries),
                },
            })
            _atomic_write_json(state.legacy_path, payload)
            record = store.upsert_legacy(state.legacy_path)
            store.verify_legacy(state.legacy_path)
            refreshed.append({
                "dataset_id": state.target.id,
                "status": "refreshed",
                "watermark_before": state.watermark.isoformat() if state.watermark else None,
                "watermark_after": _timestamp_date(rows[-1]["begin"]).isoformat(),
                "requested_from": state.requested_from.isoformat() if state.requested_from else None,
                "requested_till": till_date,
                "chunk_count": len(chunks),
                "network_requests": sum(item[1].get("page_count", 0) for item in chunks),
                "raw_entry_count": len(raw_entries),
                "row_count": record["row_count"],
                "legacy_path": str(state.legacy_path),
            })
        return {
            "schema_version": REFRESH_SCHEMA,
            "status": "refreshed" if refreshed else "up_to_date",
            "workers": workers,
            "scheduled_chunk_count": task_count,
            "datasets": [*skipped, *refreshed],
        }
