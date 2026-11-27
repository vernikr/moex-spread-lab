"""CLI commands owned by the data package."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from moex_spread_lab.core.w0 import W0Error, write_json
from moex_spread_lab.data.benchmark.lineage import build_lineage, collect_benchmark_sources
from moex_spread_lab.data.iss.catalog import fetch_candle_series, fetch_iss_catalog
from moex_spread_lab.data.lake.data_refresh import RefreshTarget, refresh_candle_datasets
from moex_spread_lab.data.lake.h1_seal import activate_h1, deactivate_h1, verify_sealed_h1
from moex_spread_lab.data.lake.parquet_store import ParquetBarStore
from moex_spread_lab.data.retention.data_inventory import build_data_inventory
from moex_spread_lab.data.retention.data_retention import (
    archive_rebuildable_caches,
    plan_rebuildable_cache_archive,
)
from moex_spread_lab.data.retention.storage_policy import publish_report, storage_budget

from .cli_parsers import (
    register_h1_parsers,
    register_iss_and_benchmark_parsers,
    register_lake_parsers,
    register_retention_parsers,
)


def register(subparsers: Any) -> None:
    register_retention_parsers(subparsers, run)
    register_h1_parsers(subparsers, run)
    register_lake_parsers(subparsers, run)
    register_iss_and_benchmark_parsers(subparsers, run)


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _run_iss_intake(args: Any) -> int:
    result = fetch_iss_catalog(
        base_url=args.base_url, board=args.board, near_secid=args.near, far_secid=args.far
    )
    write_json(args.output, result)
    _emit(
        {
            "admission_status": result["admission_status"],
            "candidate": result["candidate"]["candidate_id"],
            "instrument_count": result["instrument_count"],
            "raw_response_sha256": result["source"]["raw_response_sha256"],
            "rights_status": result["source"]["rights_status"],
        }
    )
    return 0


def _run_iss_candles(args: Any) -> int:
    results: list[dict[str, Any]] = []
    for secid in args.secid:
        for interval in args.interval:
            result = fetch_candle_series(
                secid=secid,
                interval=interval,
                from_date=args.from_date,
                till_date=args.till_date,
                base_url=args.base_url,
                board=args.board,
                timeout=args.timeout,
                delay_seconds=args.delay_seconds,
                cache_dir=args.cache_dir,
                max_pages=args.max_pages,
                retries=args.retries,
            )
            output = args.output_dir / f"{secid}_{result['timeframe']}.json"
            write_json(output, result)
            results.append(
                {
                    "secid": secid,
                    "timeframe": result["timeframe"],
                    "row_count": result["row_count"],
                    "page_count": result["page_count"],
                    "duplicate_count": result["duplicate_count"],
                    "output": str(output),
                }
            )
    summary = args.output_dir / "summary.json"
    write_json(
        summary,
        {
            "schema_version": "iss-candle-intake-summary.v1",
            "from": args.from_date,
            "till": args.till_date,
            "price_interpretation": "candle_price_proxy_for_medium_term_analysis",
            "series": results,
        },
    )
    _emit({"series": results, "summary": str(summary)})
    return 0


def _run_benchmark_lineage(args: Any) -> int:
    raw_sources, source_reports = collect_benchmark_sources(
        args.cache_dir, args.from_date, args.till_date, args.delay_seconds
    )
    missing = [
        t for t in ("CBR_RUONIA", "RUSFAR", "FXMM", "VTBM", "LQDT") if not raw_sources.get(t)
    ]
    result = build_lineage(raw_sources)
    result["source_reports"] = source_reports
    result["missing_sources"] = missing
    result["release_state"] = (
        "observed_plus_labelled_proxy" if not missing else "exploratory_proxy_only"
    )
    write_json(args.output, result)
    _emit(
        {
            "extended_start": result["extended_start"],
            "extended_end": result["extended_end"],
            "observed_start": result["observed_start"],
            "row_count": len(result["rows"]),
            "source_rows": result["source_rows"],
            "missing_sources": missing,
            "release_state": result["release_state"],
        }
    )
    return 0


def _run_h1_seal(args: Any) -> int:
    try:
        if args.activate:
            if args.plan is None or args.candidate is None:
                raise W0Error("activation requires --plan and --candidate")
            result = activate_h1(args.root, args.plan, args.candidate)
        else:
            if args.plan is not None or args.candidate is not None:
                raise W0Error("--plan/--candidate apply only to activation")
            result = verify_sealed_h1(args.root) if args.verify else deactivate_h1(args.root)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise W0Error(f"H1 seal operation failed: {exc}") from exc
    _emit(result)
    return 0


def _run_data_inventory(args: Any) -> int:
    result = build_data_inventory(args.root, include_hashes=args.hashes, inspect_json=not args.fast)
    if args.output:
        write_json(args.output, result)
    summary = (
        result
        if args.format == "json"
        else {
            "status": "ok",
            "schema_version": result["schema_version"],
            "summary": result["summary"],
            "output": str(args.output) if args.output else None,
        }
    )
    _emit(summary)
    return 0


def _run_storage_policy(args: Any) -> int:
    try:
        result = storage_budget(args.root, args.archives_root, args.max_allocated_bytes)
        if args.output:
            publish_report(args.output, result)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise W0Error(f"storage policy report failed: {exc}") from exc
    _emit(result)
    return 0


_ARCHIVE_KEYS = (
    "schema_version",
    "status",
    "archive",
    "classification",
    "file_count",
    "logical_bytes",
    "allocated_bytes",
    "archive_logical_bytes",
    "deleted_file_count",
    "requires_apply",
)


def _run_data_archive(args: Any) -> int:
    result = (
        archive_rebuildable_caches(args.root, args.manifest, args.archive)
        if args.apply
        else plan_rebuildable_cache_archive(args.root, args.manifest, args.archive)
    )
    _emit(result if args.format == "json" else {k: result[k] for k in _ARCHIVE_KEYS if k in result})
    return 0


def _run_import_candles(args: Any) -> int:
    lake_root = args.lake_root if args.lake_root.is_absolute() else args.root / args.lake_root
    store = ParquetBarStore(args.root, lake_root)
    records: list[dict[str, Any]] = []
    for legacy in args.legacy:
        record = store.import_legacy(legacy, replace=args.replace)
        if args.verify:
            record["verification"] = store.verify_legacy(legacy)
        records.append(record)
    payload = (
        records
        if args.format == "json"
        else {
            "status": "ok",
            "dataset_count": len(records),
            "row_count": sum(int(record["row_count"]) for record in records),
            "datasets": [record["dataset_id"] for record in records],
        }
    )
    _emit(payload)
    return 0


def _run_data_refresh(args: Any) -> int:
    root = args.root.resolve()
    lake_root = args.lake_root if args.lake_root.is_absolute() else root / args.lake_root
    output = refresh_candle_datasets(
        root=root,
        targets=[
            RefreshTarget(secid, interval) for secid in args.secid for interval in args.interval
        ],
        till_date=args.till_date,
        from_date=args.from_date,
        output_dir=args.output_dir,
        lake_root=lake_root,
        board=args.board,
        base_url=args.base_url,
        overlap_sessions=args.overlap_sessions,
        chunk_days=args.chunk_days,
        workers=args.workers,
        full_refresh=args.full_refresh,
        raw_ttl_days=args.raw_ttl_days,
        pin_raw=args.pin_raw,
        timeout=args.timeout,
        delay_seconds=args.delay_seconds,
        retries=args.retries,
        cache_dir=args.cache_dir,
    )
    summary = (
        output
        if args.format == "json"
        else {
            "status": output["status"],
            "dataset_count": len(output["datasets"]),
            "scheduled_chunk_count": output["scheduled_chunk_count"],
            "datasets": output["datasets"],
        }
    )
    _emit(summary)
    return 0


_LAKE_KEYS = (
    "status",
    "dataset_count",
    "expired_entry_count",
    "orphan_file_count",
    "retained_entry_count",
)


def _run_lake_maintenance(args: Any) -> int:
    root = args.root.resolve()
    lake_root = args.lake_root if args.lake_root.is_absolute() else root / args.lake_root
    store = ParquetBarStore(root, lake_root)
    output = (
        store.verify_all() if args.command == "data-verify" else store.compact(force=args.force)
    )
    _emit(output if args.format == "json" else {k: output[k] for k in _LAKE_KEYS if k in output})
    return 0


_DISPATCH: dict[str, Callable[[Any], int]] = {
    "data-inventory": _run_data_inventory,
    "storage-budget": _run_storage_policy,
    "data-archive": _run_data_archive,
    "data-h1-seal": _run_h1_seal,
    "data-import-candles": _run_import_candles,
    "data-refresh": _run_data_refresh,
    "data-verify": _run_lake_maintenance,
    "data-compact": _run_lake_maintenance,
    "iss-intake": _run_iss_intake,
    "iss-candles": _run_iss_candles,
    "benchmark-lineage": _run_benchmark_lineage,
}


def run(args: Any) -> int:
    handler = _DISPATCH.get(args.command)
    return handler(args) if handler is not None else 2
