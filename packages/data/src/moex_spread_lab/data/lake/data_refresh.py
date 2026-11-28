"""Bounded, watermark-driven candle refresh into the verified local data lake."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any, cast

from ..iss.catalog import (
    DEFAULT_BOARD,
    DEFAULT_ISS_BASE_URL,
    fetch_candle_series,
)
from ..iss.iss_http import is_safe_component
from .h1_seal import SealedH1Reader
from .io import atomic_write_json, writer_lock
from .parquet_store import ParquetBarStore
from .refresh_merge import (
    REFRESH_SCHEMA,
    DataRefreshError,
    RefreshConfig,
    RefreshTarget,
    TargetState,
    build_merged_payload,
    chunks,
    parse_date,
    plan_single_target,
    timestamp_date,
)

_REFRESH_OPTIONS = frozenset(
    {
        "from_date",
        "output_dir",
        "lake_root",
        "board",
        "base_url",
        "overlap_sessions",
        "chunk_days",
        "workers",
        "full_refresh",
        "raw_ttl_days",
        "pin_raw",
        "timeout",
        "delay_seconds",
        "retries",
        "cache_dir",
        "fetcher",
    }
)
CapturedRaw = tuple[str, bytes, str, str]
ChunkFetchResult = tuple[date, dict[str, Any], list[CapturedRaw]]
ChunkFutureResult = tuple[str, date, dict[str, Any], list[CapturedRaw]]


def parse_candle_row(row: object) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(row, dict):
        return None
    typed_row = cast(dict[str, Any], row)
    begin = typed_row.get("begin")
    if not isinstance(begin, str):
        return None
    return begin, dict(typed_row)


def _parse_refresh_config(
    resolved_root: Path, till_date: str, options: dict[str, Any]
) -> tuple[RefreshConfig, Path | None]:
    unknown = set(options) - _REFRESH_OPTIONS
    if unknown:
        raise TypeError(f"unexpected keyword argument(s): {sorted(unknown)}")
    board = options.get("board", DEFAULT_BOARD)
    if not is_safe_component(board):
        raise DataRefreshError(f"invalid refresh board: {board!r}")
    workers = int(options.get("workers", 2))
    if not 1 <= workers <= 4:
        raise DataRefreshError("refresh workers must be between 1 and 4")
    overlap_sessions = int(options.get("overlap_sessions", 5))
    raw_ttl_days = int(options.get("raw_ttl_days", 30))
    if overlap_sessions < 0 or raw_ttl_days < 0:
        raise DataRefreshError("overlap_sessions and raw_ttl_days must not be negative")
    until = parse_date(till_date, "till_date")
    from_date = options.get("from_date")
    initial_from = parse_date(from_date, "from_date") if from_date else None
    if initial_from is not None and initial_from > until:
        raise DataRefreshError("from_date must not be after till_date")
    raw_output: Path = options.get("output_dir", Path("data/w1/candles"))
    output_dir = raw_output if raw_output.is_absolute() else resolved_root / raw_output
    raw_cache: Path | None = options.get("cache_dir")
    cache_dir = (
        raw_cache if raw_cache is None or raw_cache.is_absolute() else resolved_root / raw_cache
    )
    cfg = RefreshConfig(
        until=until,
        till_date=till_date,
        initial_from=initial_from,
        output_dir=output_dir,
        cache_dir=cache_dir,
        board=board,
        base_url=options.get("base_url", DEFAULT_ISS_BASE_URL),
        overlap_sessions=overlap_sessions,
        chunk_days=int(options.get("chunk_days", 90)),
        workers=workers,
        full_refresh=bool(options.get("full_refresh", False)),
        raw_ttl_days=raw_ttl_days,
        pin_raw=bool(options.get("pin_raw", False)),
        timeout=float(options.get("timeout", 20.0)),
        delay_seconds=float(options.get("delay_seconds", 0.1)),
        retries=int(options.get("retries", 3)),
        fetcher=options.get("fetcher", fetch_candle_series),
    )
    return cfg, options.get("lake_root")


def _run_single_chunk(
    state: TargetState, chunk_from: date, chunk_till: date, cfg: RefreshConfig
) -> ChunkFutureResult:
    captured: list[CapturedRaw] = []

    def raw_sink(url: str, raw: bytes, received_at: str, raw_sha256: str) -> None:
        captured.append((url, raw, received_at, raw_sha256))

    result = cfg.fetcher(
        secid=state.target.secid,
        interval=state.target.interval,
        from_date=chunk_from.isoformat(),
        till_date=chunk_till.isoformat(),
        base_url=cfg.base_url,
        board=cfg.board,
        timeout=cfg.timeout,
        delay_seconds=cfg.delay_seconds,
        cache_dir=cfg.cache_dir,
        retries=cfg.retries,
        raw_sink=raw_sink,
    )
    return state.target.id, chunk_from, result, captured


def _fetch_target_chunks(
    states: Sequence[TargetState], cfg: RefreshConfig
) -> tuple[dict[str, list[ChunkFetchResult]], int]:
    fetched: dict[str, list[ChunkFetchResult]] = {state.target.id: [] for state in states}
    task_count = 0
    with ThreadPoolExecutor(max_workers=cfg.workers) as executor:
        futures: list[Future[ChunkFutureResult]] = []
        for state in states:
            for chunk_from, chunk_till in chunks(state.requested_from, cfg.until, cfg.chunk_days):
                task_count += 1
                futures.append(
                    executor.submit(_run_single_chunk, state, chunk_from, chunk_till, cfg)
                )
        for future in as_completed(futures):
            target_id, chunk_from, result, captured = future.result()
            fetched[target_id].append((chunk_from, result, captured))
    return fetched, task_count


def _archive_captured_raw(
    captured: list[CapturedRaw], store: ParquetBarStore, cfg: RefreshConfig
) -> list[str]:
    entry_ids: list[str] = []
    for url, raw, received_at, raw_sha256 in captured:
        entry = store.raw_archive.archive(
            url=url,
            raw=raw,
            received_at=received_at,
            ttl_days=cfg.raw_ttl_days,
            pin=cfg.pin_raw,
        )
        if entry["raw_response_sha256"] != raw_sha256:
            raise DataRefreshError("raw response hash changed before archival")
        entry_ids.append(str(entry["id"]))
    return entry_ids


def _merge_chunk_rows(
    result: dict[str, Any], target_id: str, secid: str, rows_by_begin: dict[str, dict[str, Any]]
) -> int:
    if result.get("schema_version") != "iss-candles.v1" or result.get("secid") != secid:
        raise DataRefreshError(f"fetcher returned an invalid series for {target_id}")
    raw_rows: Iterable[object] = result.get("rows", ())
    for row in raw_rows:
        parsed = parse_candle_row(row)
        if parsed is None:
            raise DataRefreshError(f"fetcher returned an invalid candle row for {target_id}")
        begin, copied_row = parsed
        rows_by_begin[begin] = copied_row
    return int(result.get("page_count", 0))


def _ingest_chunk_rows_and_raw(
    state: TargetState,
    chunks: list[ChunkFetchResult],
    store: ParquetBarStore,
    cfg: RefreshConfig,
) -> tuple[list[dict[str, Any]], list[str], int]:
    rows_by_begin: dict[str, dict[str, Any]] = {}
    for row in state.payload.get("rows", ()) if state.payload else ():
        parsed = parse_candle_row(row)
        if parsed is not None:
            begin, copied_row = parsed
            rows_by_begin[begin] = copied_row
    raw_entries: list[str] = []
    page_count = 0
    for _, result, captured in chunks:
        page_count += _merge_chunk_rows(result, state.target.id, state.target.secid, rows_by_begin)
        raw_entries.extend(_archive_captured_raw(captured, store, cfg))
    rows = [rows_by_begin[key] for key in sorted(rows_by_begin)]
    if not rows:
        raise DataRefreshError(f"refresh produced no rows for {state.target.id}")
    return rows, raw_entries, page_count


def _publish_refreshed_target(
    state: TargetState,
    chunks: list[ChunkFetchResult],
    store: ParquetBarStore,
    cfg: RefreshConfig,
    *,
    build_payload: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    ordered_chunks = sorted(chunks, key=lambda item: item[0])
    rows, raw_entries, page_count = _ingest_chunk_rows_and_raw(state, ordered_chunks, store, cfg)
    payload = build_payload(
        rows=rows,
        page_count=page_count,
        chunk_count=len(ordered_chunks),
        raw_entry_count=len(raw_entries),
    )
    atomic_write_json(state.legacy_path, payload)
    record = store.upsert_legacy(state.legacy_path)
    store.verify_legacy(state.legacy_path)
    last_date = timestamp_date(rows[-1]["begin"])
    if last_date is None:
        raise DataRefreshError(f"invalid final timestamp for {state.target.id}")
    return {
        "dataset_id": state.target.id,
        "status": "refreshed",
        "watermark_before": state.watermark.isoformat() if state.watermark else None,
        "watermark_after": last_date.isoformat(),
        "requested_from": state.requested_from.isoformat(),
        "requested_till": cfg.till_date,
        "chunk_count": len(ordered_chunks),
        "network_requests": sum(int(item[1].get("page_count", 0)) for item in ordered_chunks),
        "raw_entry_count": len(raw_entries),
        "row_count": record["row_count"],
        "legacy_path": str(state.legacy_path),
    }


def refresh_candle_datasets(
    *,
    root: Path,
    targets: list[RefreshTarget],
    till_date: str,
    **options: Any,
) -> dict[str, Any]:
    """Fetch only ``watermark - overlap`` through ``till_date`` and upsert safely."""
    resolved_root = root.resolve()
    if (
        any(target.interval == 60 for target in targets)
        and SealedH1Reader(resolved_root).payload() is not None
    ):
        raise DataRefreshError("deactivate the H1 seal with restored JSON before refreshing H1")
    if not targets:
        raise DataRefreshError("at least one refresh target is required")
    cfg, lake_root = _parse_refresh_config(resolved_root, till_date, options)
    store = ParquetBarStore(resolved_root, lake_root)
    ordered_targets = sorted(
        {(target.secid, target.interval): target for target in targets}.values(),
        key=lambda item: (item.secid, item.interval),
    )
    with ExitStack() as locks:
        for target in ordered_targets:
            lock_name = hashlib.sha256(target.id.encode("utf-8")).hexdigest() + ".lock"
            locks.enter_context(writer_lock(store.lake_root / "locks" / lock_name))
        planned = [plan_single_target(target, store, cfg) for target in ordered_targets]
        skipped = [skip_entry for _, skip_entry in planned if skip_entry is not None]
        states = [
            state for state, skip_entry in planned if skip_entry is None and state is not None
        ]
        fetched, task_count = _fetch_target_chunks(states, cfg)
        refreshed = [
            _publish_refreshed_target(
                state,
                fetched[state.target.id],
                store,
                cfg,
                build_payload=partial(build_merged_payload, state, cfg),
            )
            for state in states
        ]
    return {
        "schema_version": REFRESH_SCHEMA,
        "status": "refreshed" if refreshed else "up_to_date",
        "workers": cfg.workers,
        "scheduled_chunk_count": task_count,
        "datasets": [*skipped, *refreshed],
    }


__all__ = [
    "REFRESH_SCHEMA",
    "DataRefreshError",
    "RefreshTarget",
    "refresh_candle_datasets",
]
