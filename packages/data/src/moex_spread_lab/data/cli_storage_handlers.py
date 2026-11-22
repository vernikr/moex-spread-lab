"""Retention, storage-policy, H1 seal/stage, and Parquet lake CLI command handlers."""

from __future__ import annotations

import json
from typing import Any

from moex_spread_lab.core.w0 import W0Error, write_json


def _run_archive_candidates(args: Any) -> int:
    from moex_spread_lab.data.retention.data_archive_candidates import execute

    try:
        if args.archive and not args.dry_run:
            raise W0Error("--archive is only accepted for --dry-run; other modes use the plan")
        mode = "dry-run" if args.dry_run else "archive-only" if args.archive_only else "apply"
        result = execute(args.root, args.plan, mode, args.archive)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise W0Error(f"candidate archive failed: {exc}") from exc
    print(
        json.dumps(
            {key: value for key, value in result.items() if key not in {"files", "manifest"}},
            sort_keys=True,
        )
    )
    return 0


def _run_h1_archive(args: Any) -> int:
    from moex_spread_lab.data.retention.h1_archive import execute

    try:
        if not args.dry_run and (args.archive or args.evidence):
            raise W0Error("archive/evidence paths must come from the reviewed plan")
        if args.retry_restored and not args.apply:
            raise W0Error("--retry-restored requires --apply")
        mode = "dry-run" if args.dry_run else "archive-only" if args.archive_only else "apply"
        result = execute(
            args.root,
            args.plan,
            mode,
            archive=args.archive,
            evidence=args.evidence,
            retry_restored=args.retry_restored,
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise W0Error(f"H1 archival failed: {exc}") from exc
    print(json.dumps({k: v for k, v in result.items() if k != "deleted"}, sort_keys=True))
    return 0


def _run_h1_seal(args: Any) -> int:
    from moex_spread_lab.data.lake.h1_seal import activate_h1, deactivate_h1, verify_sealed_h1

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
    print(json.dumps(result, sort_keys=True))
    return 0


def _run_h1_stage(args: Any) -> int:
    from moex_spread_lab.data.lake.h1_migration import stage_h1, verify_h1_stage, write_h1_plan

    try:
        if args.dry_run:
            if args.output_dir:
                raise W0Error("--output-dir is only used for --stage or --verify")
            raw = write_h1_plan(args.root, args.plan)
            result = {
                key: value for key, value in raw.items() if key not in {"files", "screen_selection"}
            }
            result["status"] = "planned"
        else:
            if args.output_dir is None:
                raise W0Error("--output-dir is required for --stage or --verify")
            operation = stage_h1 if args.stage else verify_h1_stage
            raw = operation(args.root, args.plan, args.output_dir)
            result = {key: value for key, value in raw.items() if key not in {"datasets", "plan"}}
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise W0Error(f"H1 candidate operation failed: {exc}") from exc
    print(json.dumps(result, sort_keys=True))
    return 0


def _run_storage_policy(args: Any) -> int:
    from moex_spread_lab.data.retention.storage_policy import (
        archive_catalog,
        publish_report,
        storage_budget,
    )

    try:
        if args.command == "storage-budget":
            result = storage_budget(args.root, args.archives_root, args.max_allocated_bytes)
        else:
            result = archive_catalog(args.archive, args.review_date)
        if args.output:
            publish_report(args.output, result)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise W0Error(f"storage policy report failed: {exc}") from exc
    payload = (
        result
        if args.command == "storage-budget"
        else {
            "status": "verified",
            "archive_count": len(result["entries"]),
            "output": str(args.output),
        }
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 2 if result.get("status") == "over_budget" else 0


def _run_data_inventory(args: Any) -> int:
    from moex_spread_lab.data.retention.data_inventory import build_data_inventory

    result = build_data_inventory(
        args.root,
        include_hashes=args.hashes,
        inspect_json=not args.fast,
    )
    if args.output:
        write_json(args.output, result)
    payload = (
        result
        if args.format == "json"
        else {
            "status": "ok",
            "schema_version": result["schema_version"],
            "summary": result["summary"],
            "output": str(args.output) if args.output else None,
        }
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _run_data_archive(args: Any) -> int:
    from moex_spread_lab.data.retention.data_retention import (
        archive_rebuildable_caches,
        plan_rebuildable_cache_archive,
    )

    result = (
        archive_rebuildable_caches(args.root, args.manifest, args.archive)
        if args.apply
        else plan_rebuildable_cache_archive(args.root, args.manifest, args.archive)
    )
    summary_keys = (
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
    payload = (
        result
        if args.format == "json"
        else {key: result[key] for key in summary_keys if key in result}
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _run_import_candles(args: Any) -> int:
    from moex_spread_lab.data.lake.data_lake import ParquetBarStore

    lake_root = args.lake_root if args.lake_root.is_absolute() else args.root / args.lake_root
    store = ParquetBarStore(args.root, lake_root)
    records: list[dict[str, Any]] = []
    for legacy in args.legacy:
        record = store.import_legacy(legacy, replace=args.replace)
        if args.verify:
            record["verification"] = store.verify_legacy(legacy)
        records.append(record)
    payload: dict[str, Any] = (
        {"status": "ok", "datasets": records}
        if args.format == "json"
        else {
            "status": "ok",
            "dataset_count": len(records),
            "row_count": sum(int(record["row_count"]) for record in records),
            "datasets": [record["dataset_id"] for record in records],
        }
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _run_data_refresh(args: Any) -> int:
    from moex_spread_lab.data.lake.data_refresh import RefreshTarget, refresh_candle_datasets

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
    payload = (
        output
        if args.format == "json"
        else {
            "status": output["status"],
            "dataset_count": len(output["datasets"]),
            "scheduled_chunk_count": output["scheduled_chunk_count"],
            "datasets": output["datasets"],
        }
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _run_lake_maintenance(args: Any) -> int:
    from moex_spread_lab.data.lake.data_lake import ParquetBarStore

    root = args.root.resolve()
    lake_root = args.lake_root if args.lake_root.is_absolute() else root / args.lake_root
    store = ParquetBarStore(root, lake_root)
    if args.command == "data-verify":
        output = store.verify_all()
    elif args.command == "data-compact":
        output = store.compact(force=args.force)
    elif args.command == "data-pin-raw":
        output = {"status": "pinned", "entry": store.raw_archive.pin(args.entry_id)}
    else:
        output = store.raw_archive.gc(dry_run=not args.apply)
    summary_keys = (
        "status",
        "dataset_count",
        "expired_entry_count",
        "orphan_file_count",
        "retained_entry_count",
    )
    payload = (
        output
        if args.format == "json"
        else {key: output[key] for key in summary_keys if key in output}
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


STORAGE_DISPATCH = {
    "data-archive-candidates": _run_archive_candidates,
    "data-h1-archive": _run_h1_archive,
    "data-h1-seal": _run_h1_seal,
    "data-h1-stage": _run_h1_stage,
    "storage-budget": _run_storage_policy,
    "archive-catalog": _run_storage_policy,
    "data-inventory": _run_data_inventory,
    "data-archive": _run_data_archive,
    "data-import-candles": _run_import_candles,
    "data-refresh": _run_data_refresh,
    "data-verify": _run_lake_maintenance,
    "data-compact": _run_lake_maintenance,
    "data-pin-raw": _run_lake_maintenance,
    "data-gc": _run_lake_maintenance,
}
