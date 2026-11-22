"""CLI commands owned by the data package."""

from __future__ import annotations

import json
from collections import Counter
from functools import partial
from pathlib import Path

from moex_spread_lab.core.w0 import W0Error, write_json
from moex_spread_lab.data.iss.catalog import (
    DEFAULT_FAR,
    DEFAULT_ISS_BASE_URL,
    DEFAULT_NEAR,
    fetch_candle_coverage,
    fetch_iss_catalog,
    write_timeframe_inventory,
)


def register(subparsers):
    candidates = subparsers.add_parser(
        "data-archive-candidates",
        help="plan, verify or explicitly apply research candidate archival",
    )
    candidates.add_argument("--root", type=Path, default=Path("."))
    candidates.add_argument(
        "--plan",
        type=Path,
        required=True,
        help="new dry-run plan outside data/; required input for other modes",
    )
    candidates.add_argument("--archive", type=Path, help="new tar.zst destination, dry-run only")
    mode = candidates.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--archive-only", action="store_true")
    mode.add_argument("--apply", action="store_true")
    candidates.set_defaults(_msl_cli_handler=run)

    budget = subparsers.add_parser(
        "storage-budget", help="read-only allocated storage classes and TTL dry-runs"
    )
    budget.add_argument("--root", type=Path, default=Path.cwd())
    budget.add_argument("--archives-root", type=Path)
    budget.add_argument("--max-allocated-bytes", type=int)
    budget.add_argument("--output", type=Path)
    budget.set_defaults(_msl_cli_handler=run)

    catalog = subparsers.add_parser(
        "archive-catalog", help="verify archives and publish a no-clobber recovery catalog"
    )
    catalog.add_argument("--archive", type=Path, action="append", required=True)
    catalog.add_argument("--review-date", required=True)
    catalog.add_argument("--output", type=Path, required=True)
    catalog.set_defaults(_msl_cli_handler=run)

    inventory_parser = subparsers.add_parser(
        "data-inventory",
        help="build a read-only local-data inventory and retention manifest",
    )
    inventory_parser.add_argument("--root", type=Path, default=Path.cwd())
    inventory_parser.add_argument("--output", type=Path)
    inventory_parser.add_argument(
        "--hashes", action="store_true", help="include SHA-256 for every local file"
    )
    inventory_parser.add_argument(
        "--fast", action="store_true", help="skip JSON payload inspection"
    )
    inventory_parser.add_argument("--format", choices=("summary", "json"), default="summary")
    inventory_parser.set_defaults(_msl_cli_handler=run)

    archive_parser = subparsers.add_parser(
        "data-archive",
        help="archive verified rebuildable data caches outside the data directory",
    )
    archive_parser.add_argument("--root", type=Path, default=Path.cwd())
    archive_parser.add_argument("--manifest", type=Path, required=True)
    archive_parser.add_argument("--archive", type=Path, required=True)
    archive_parser.add_argument(
        "--apply",
        action="store_true",
        help="create a verified archive and remove its source caches",
    )
    archive_parser.add_argument("--format", choices=("summary", "json"), default="summary")
    archive_parser.set_defaults(_msl_cli_handler=run)

    h1_archive = subparsers.add_parser(
        "data-h1-archive",
        help="archive/apply exact sealed H1 sources with restore, evidence and 80%% gain gates",
    )
    h1_archive.add_argument("--root", type=Path, default=Path.cwd())
    h1_archive.add_argument("--plan", type=Path, required=True)
    h1_archive.add_argument("--archive", type=Path)
    h1_archive.add_argument("--evidence", type=Path)
    h1_archive.add_argument(
        "--retry-restored",
        action="store_true",
        help="explicitly retry apply only after all originals are restored; preserve prior journal",
    )
    h1_archive_mode = h1_archive.add_mutually_exclusive_group(required=True)
    h1_archive_mode.add_argument("--dry-run", action="store_true")
    h1_archive_mode.add_argument("--archive-only", action="store_true")
    h1_archive_mode.add_argument("--apply", action="store_true")
    h1_archive.set_defaults(_msl_cli_handler=run)

    seal = subparsers.add_parser(
        "data-h1-seal",
        help="activate/verify sealed H1 reads or restore JSON routing; never deletes source data",
    )
    seal.add_argument("--root", type=Path, default=Path.cwd())
    seal.add_argument("--plan", type=Path)
    seal.add_argument("--candidate", type=Path)
    seal_mode = seal.add_mutually_exclusive_group(required=True)
    seal_mode.add_argument("--activate", action="store_true")
    seal_mode.add_argument("--verify", action="store_true")
    seal_mode.add_argument("--deactivate", action="store_true")
    seal.set_defaults(_msl_cli_handler=run)

    h1 = subparsers.add_parser(
        "data-h1-stage",
        help="plan and verify isolated H1 candidate lakes; no runtime activation or deletion",
    )
    h1.add_argument("--root", type=Path, default=Path.cwd())
    h1.add_argument("--plan", type=Path, required=True)
    h1.add_argument("--output-dir", type=Path)
    h1_mode = h1.add_mutually_exclusive_group(required=True)
    h1_mode.add_argument("--dry-run", action="store_true")
    h1_mode.add_argument("--stage", action="store_true")
    h1_mode.add_argument("--verify", action="store_true")
    h1.set_defaults(_msl_cli_handler=run)

    import_parser = subparsers.add_parser(
        "data-import-candles",
        help="import legacy iss-candles JSON into the local Parquet data lake",
    )
    import_parser.add_argument("--root", type=Path, default=Path.cwd())
    import_parser.add_argument("--legacy", type=Path, action="append", required=True)
    import_parser.add_argument("--lake-root", type=Path, default=Path("data-lake"))
    import_parser.add_argument("--replace", action="store_true")
    import_parser.add_argument(
        "--verify", action="store_true", help="verify each Parquet import against its legacy JSON"
    )
    import_parser.add_argument("--format", choices=("summary", "json"), default="summary")
    import_parser.set_defaults(_msl_cli_handler=run)

    refresh_parser = subparsers.add_parser(
        "data-refresh",
        help="incrementally refresh candle series into legacy JSON and the verified Parquet lake",
    )
    refresh_parser.add_argument("--root", type=Path, default=Path.cwd())
    refresh_parser.add_argument("--secid", action="append", required=True)
    refresh_parser.add_argument("--interval", action="append", type=int, required=True)
    refresh_parser.add_argument(
        "--from-date", help="required only for a first dataset or --full-refresh"
    )
    refresh_parser.add_argument("--till-date", required=True)
    refresh_parser.add_argument("--output-dir", type=Path, default=Path("data/w1/candles"))
    refresh_parser.add_argument("--lake-root", type=Path, default=Path("data-lake"))
    refresh_parser.add_argument("--base-url", default=DEFAULT_ISS_BASE_URL)
    refresh_parser.add_argument("--board", default="RFUD")
    refresh_parser.add_argument("--overlap-sessions", type=int, default=5)
    refresh_parser.add_argument("--chunk-days", type=int, default=90)
    refresh_parser.add_argument("--workers", type=int, default=2)
    refresh_parser.add_argument("--full-refresh", action="store_true")
    refresh_parser.add_argument("--raw-ttl-days", type=int, default=30)
    refresh_parser.add_argument("--pin-raw", action="store_true")
    refresh_parser.add_argument("--cache-dir", type=Path)
    refresh_parser.add_argument("--timeout", type=float, default=20.0)
    refresh_parser.add_argument("--delay-seconds", type=float, default=0.1)
    refresh_parser.add_argument("--retries", type=int, default=3)
    refresh_parser.add_argument("--format", choices=("summary", "json"), default="summary")
    refresh_parser.set_defaults(_msl_cli_handler=run)

    verify_parser = subparsers.add_parser(
        "data-verify", help="verify every registered Parquet dataset and retained raw response"
    )
    verify_parser.add_argument("--root", type=Path, default=Path.cwd())
    verify_parser.add_argument("--lake-root", type=Path, default=Path("data-lake"))
    verify_parser.add_argument("--format", choices=("summary", "json"), default="summary")
    verify_parser.set_defaults(_msl_cli_handler=run)

    compact_parser = subparsers.add_parser(
        "data-compact", help="compact registered Parquet datasets into canonical Zstd files"
    )
    compact_parser.add_argument("--root", type=Path, default=Path.cwd())
    compact_parser.add_argument("--lake-root", type=Path, default=Path("data-lake"))
    compact_parser.add_argument(
        "--force", action="store_true", help="rewrite current single-file datasets too"
    )
    compact_parser.add_argument("--format", choices=("summary", "json"), default="summary")
    compact_parser.set_defaults(_msl_cli_handler=run)

    pin_parser = subparsers.add_parser(
        "data-pin-raw", help="pin a raw .json.zst response against TTL collection"
    )
    pin_parser.add_argument("--root", type=Path, default=Path.cwd())
    pin_parser.add_argument("--lake-root", type=Path, default=Path("data-lake"))
    pin_parser.add_argument("--entry-id", required=True)
    pin_parser.add_argument("--format", choices=("summary", "json"), default="summary")
    pin_parser.set_defaults(_msl_cli_handler=run)

    gc_parser = subparsers.add_parser(
        "data-gc", help="plan or collect expired unpinned raw .json.zst responses"
    )
    gc_parser.add_argument("--root", type=Path, default=Path.cwd())
    gc_parser.add_argument("--lake-root", type=Path, default=Path("data-lake"))
    gc_mode = gc_parser.add_mutually_exclusive_group()
    gc_mode.add_argument(
        "--dry-run", action="store_true", help="report files only; this is the default"
    )
    gc_mode.add_argument(
        "--apply", action="store_true", help="delete only expired, unpinned responses and orphans"
    )
    gc_parser.add_argument("--format", choices=("summary", "json"), default="summary")
    gc_parser.set_defaults(_msl_cli_handler=run)

    iss_parser = subparsers.add_parser("iss-intake", help="read a current MOEX ISS futures catalog")
    iss_parser.add_argument("--output", type=Path, required=True)
    iss_parser.add_argument("--base-url", default=DEFAULT_ISS_BASE_URL)
    iss_parser.add_argument("--board", default="RFUD")
    iss_parser.add_argument("--near", default=DEFAULT_NEAR)
    iss_parser.add_argument("--far", default=DEFAULT_FAR)
    iss_parser.set_defaults(_msl_cli_handler=run)

    coverage_parser = subparsers.add_parser(
        "iss-timeframe-inventory", help="build an instrument candle-timeframe inventory"
    )
    coverage_parser.add_argument("--output", type=Path, required=True)
    coverage_parser.add_argument("--csv-output", type=Path)
    coverage_parser.add_argument("--base-url", default=DEFAULT_ISS_BASE_URL)
    coverage_parser.add_argument("--board", default="RFUD")
    coverage_parser.add_argument("--workers", type=int, default=2)
    coverage_parser.add_argument("--delay-seconds", type=float, default=0.1)
    coverage_parser.add_argument("--timeout", type=float, default=8.0)
    coverage_parser.add_argument(
        "--cache-dir", type=Path, default=Path("data/w1/candleborders-cache")
    )
    coverage_parser.set_defaults(_msl_cli_handler=run)

    candles_parser = subparsers.add_parser(
        "iss-candles", help="download paginated ISS candle series"
    )
    candles_parser.add_argument("--secid", action="append", required=True)
    candles_parser.add_argument(
        "--interval", action="append", type=int, required=True, help="ISS interval: 1, 10, 60 or 24"
    )
    candles_parser.add_argument("--from-date", required=True)
    candles_parser.add_argument("--till-date", required=True)
    candles_parser.add_argument("--output-dir", type=Path, required=True)
    candles_parser.add_argument("--cache-dir", type=Path, default=Path("data/w1/candles-cache"))
    candles_parser.add_argument("--base-url", default=DEFAULT_ISS_BASE_URL)
    candles_parser.add_argument("--board", default="RFUD")
    candles_parser.add_argument("--timeout", type=float, default=20.0)
    candles_parser.add_argument("--delay-seconds", type=float, default=0.1)
    candles_parser.add_argument("--max-pages", type=int)
    candles_parser.add_argument("--retries", type=int, default=3)
    candles_parser.set_defaults(_msl_cli_handler=run)

    pair_parser = subparsers.add_parser(
        "iss-candle-pair-check", help="check synchronization and OHLC quality for two candle series"
    )
    pair_parser.add_argument("--near", type=Path, required=True)
    pair_parser.add_argument("--far", type=Path, required=True)
    pair_parser.add_argument("--output", type=Path, required=True)
    pair_parser.set_defaults(_msl_cli_handler=run)

    benchmark_parser = subparsers.add_parser(
        "benchmark-lineage", help="build observed and proxy LQDT benchmark lineage"
    )
    benchmark_parser.add_argument("--from-date", default="2010-01-01")
    benchmark_parser.add_argument("--till-date", required=True)
    benchmark_parser.add_argument("--output", type=Path, required=True)
    benchmark_parser.add_argument("--cache-dir", type=Path, default=Path("data/benchmark-cache"))
    benchmark_parser.add_argument("--delay-seconds", type=float, default=0.1)
    benchmark_parser.set_defaults(_msl_cli_handler=run)


def run(args):
    if args.command == "data-archive-candidates":
        from moex_spread_lab.data.retention.data_archive_candidates import execute

        try:
            if args.archive and not args.dry_run:
                raise W0Error("--archive is only accepted for --dry-run; other modes use the plan")
            result = execute(
                args.root,
                args.plan,
                "dry-run" if args.dry_run else "archive-only" if args.archive_only else "apply",
                args.archive,
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise W0Error(f"candidate archive failed: {exc}") from exc
        print(
            json.dumps(
                {key: value for key, value in result.items() if key not in {"files", "manifest"}},
                sort_keys=True,
            )
        )
        return 0
    if args.command == "data-h1-archive":
        from moex_spread_lab.data.retention.h1_archive import execute

        try:
            if not args.dry_run and (args.archive or args.evidence):
                raise W0Error("archive/evidence paths must come from the reviewed plan")
            if args.retry_restored and not args.apply:
                raise W0Error("--retry-restored requires --apply")
            result = execute(
                args.root,
                args.plan,
                "dry-run" if args.dry_run else "archive-only" if args.archive_only else "apply",
                archive=args.archive,
                evidence=args.evidence,
                retry_restored=args.retry_restored,
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise W0Error(f"H1 archival failed: {exc}") from exc
        print(json.dumps({k: v for k, v in result.items() if k != "deleted"}, sort_keys=True))
        return 0
    if args.command == "data-h1-seal":
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
    if args.command == "data-h1-stage":
        from moex_spread_lab.data.lake.h1_migration import stage_h1, verify_h1_stage, write_h1_plan

        try:
            if args.dry_run:
                if args.output_dir:
                    raise W0Error("--output-dir is only used for --stage or --verify")
                result = write_h1_plan(args.root, args.plan)
                result = {
                    key: value
                    for key, value in result.items()
                    if key not in {"files", "screen_selection"}
                }
                result["status"] = "planned"
            else:
                if args.output_dir is None:
                    raise W0Error("--output-dir is required for --stage or --verify")
                operation = stage_h1 if args.stage else verify_h1_stage
                result = operation(args.root, args.plan, args.output_dir)
                result = {
                    key: value for key, value in result.items() if key not in {"datasets", "plan"}
                }
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise W0Error(f"H1 candidate operation failed: {exc}") from exc
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command in {"storage-budget", "archive-catalog"}:
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
        print(
            json.dumps(
                result
                if args.command == "storage-budget"
                else {
                    "status": "verified",
                    "archive_count": len(result["entries"]),
                    "output": str(args.output),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2 if result.get("status") == "over_budget" else 0
    if args.command == "data-inventory":
        from moex_spread_lab.data.retention.data_inventory import build_data_inventory

        result = build_data_inventory(
            args.root,
            include_hashes=args.hashes,
            inspect_json=not args.fast,
        )
        if args.output:
            write_json(args.output, result)
        if args.format == "json":
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "schema_version": result["schema_version"],
                        "summary": result["summary"],
                        "output": str(args.output) if args.output else None,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        return 0
    if args.command == "data-archive":
        from moex_spread_lab.data.retention.data_retention import (
            archive_rebuildable_caches,
            plan_rebuildable_cache_archive,
        )

        result = (
            archive_rebuildable_caches(args.root, args.manifest, args.archive)
            if args.apply
            else plan_rebuildable_cache_archive(args.root, args.manifest, args.archive)
        )
        if args.format == "json":
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            print(
                json.dumps(
                    {
                        key: result[key]
                        for key in (
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
                        if key in result
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        return 0
    if args.command == "data-import-candles":
        from moex_spread_lab.data.lake.data_lake import ParquetBarStore

        lake_root = args.lake_root if args.lake_root.is_absolute() else args.root / args.lake_root
        store = ParquetBarStore(args.root, lake_root)
        records = []
        for legacy in args.legacy:
            record = store.import_legacy(legacy, replace=args.replace)
            if args.verify:
                record["verification"] = store.verify_legacy(legacy)
            records.append(record)
        result = {"status": "ok", "datasets": records}
        if args.format == "json":
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "dataset_count": len(records),
                        "row_count": sum(record["row_count"] for record in records),
                        "datasets": [record["dataset_id"] for record in records],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        return 0
    if args.command == "data-refresh":
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
        if args.format == "json":
            print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        else:
            print(
                json.dumps(
                    {
                        "status": output["status"],
                        "dataset_count": len(output["datasets"]),
                        "scheduled_chunk_count": output["scheduled_chunk_count"],
                        "datasets": output["datasets"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        return 0
    if args.command in {"data-verify", "data-compact", "data-pin-raw", "data-gc"}:
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
        if args.format == "json":
            print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        else:
            print(
                json.dumps(
                    {
                        key: output[key]
                        for key in (
                            "status",
                            "dataset_count",
                            "expired_entry_count",
                            "orphan_file_count",
                            "retained_entry_count",
                        )
                        if key in output
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        return 0
    if args.command == "iss-intake":
        result = fetch_iss_catalog(
            base_url=args.base_url,
            board=args.board,
            near_secid=args.near,
            far_secid=args.far,
        )
        write_json(args.output, result)
        print(
            json.dumps(
                {
                    "admission_status": result["admission_status"],
                    "candidate": result["candidate"]["candidate_id"],
                    "instrument_count": result["instrument_count"],
                    "raw_response_sha256": result["source"]["raw_response_sha256"],
                    "rights_status": result["source"]["rights_status"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "iss-timeframe-inventory":
        catalog = fetch_iss_catalog(base_url=args.base_url, board=args.board)
        inventory = fetch_candle_coverage(
            catalog["instruments"],
            base_url=args.base_url,
            board=args.board,
            timeout=args.timeout,
            workers=args.workers,
            delay_seconds=args.delay_seconds,
            cache_dir=args.cache_dir,
        )
        write_timeframe_inventory(inventory, args.output, args.csv_output)
        print(
            json.dumps(
                {
                    "instrument_count": inventory["instrument_count"],
                    "coverage_status_counts": inventory["coverage_status_counts"],
                    "minimum_timeframe_counts": inventory["minimum_timeframe_counts"],
                    "output": str(args.output),
                    "csv_output": str(args.csv_output) if args.csv_output else None,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "iss-candles":
        from moex_spread_lab.data.iss.catalog import fetch_candle_series

        results = []
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
        print(
            json.dumps(
                {"series": results, "summary": str(summary)}, ensure_ascii=False, sort_keys=True
            )
        )
        return 0
    if args.command == "iss-candle-pair-check":
        from moex_spread_lab.data.iss.catalog import analyze_candle_pair

        result = analyze_candle_pair(args.near, args.far)
        write_json(args.output, result)
        print(
            json.dumps(
                {
                    "timeframe": result["timeframe"],
                    "common_timestamp_count": result["synchronization"]["common_timestamp_count"],
                    "near_only_count": result["synchronization"]["near_only_count"],
                    "far_only_count": result["synchronization"]["far_only_count"],
                    "status": result["synchronization"]["pair_analytical_status"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "benchmark-lineage":
        from moex_spread_lab.data.benchmark.lineage import (
            build_lineage,
            fetch_cbr_ruonia_rates,
            fetch_moex_history,
            fetch_rusfar_rates,
            rate_rows_to_price,
        )

        def load_source(ticker, fetch, *, valid=lambda _: True):
            path = args.cache_dir / f"{ticker}.json"
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                cached = None
            if (
                isinstance(cached, dict)
                and cached.get("from") == args.from_date
                and cached.get("till") == args.till_date
                and valid(cached)
            ):
                return cached
            source = fetch(args.from_date, args.till_date, delay_seconds=args.delay_seconds)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(source, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
            )
            return source

        def source_report(source, **details):
            rows = source["rows"]
            boards = Counter(row.get("board") or "not_applicable" for row in rows)
            return {
                "source": source["source"],
                "row_count": len(rows),
                "raw_response_hashes": source["raw_response_hashes"],
                "first_date": rows[0]["date"] if rows else None,
                "last_date": rows[-1]["date"] if rows else None,
                "board_row_counts": dict(boards),
                **details,
            }

        raw_sources, source_reports = {}, {}
        for ticker in ("FXMM", "VTBM", "LQDT"):
            source = load_source(ticker, partial(fetch_moex_history, ticker))
            raw_sources[ticker] = source["rows"]
            source_reports[ticker] = source_report(
                source, board=source.get("board"), page_count=source["page_count"]
            )

        rusfar = load_source("RUSFAR", fetch_rusfar_rates)
        raw_sources["RUSFAR"] = rate_rows_to_price(rusfar["rows"], "RUSFAR")
        source_reports["RUSFAR"] = source_report(rusfar, page_count=rusfar["page_count"])

        ruonia = load_source(
            "CBR_RUONIA",
            fetch_cbr_ruonia_rates,
            valid=lambda value: value.get("status") == "available" and "methods" in value,
        )
        if ruonia["rows"]:
            raw_sources["CBR_RUONIA"] = rate_rows_to_price(ruonia["rows"], "CBR_RUONIA")
        source_reports["CBR_RUONIA"] = source_report(
            ruonia,
            request_count=ruonia["request_count"],
            methods=ruonia.get("methods", []),
            status=ruonia["status"],
        )
        missing_sources = [
            ticker
            for ticker in ("CBR_RUONIA", "RUSFAR", "FXMM", "VTBM", "LQDT")
            if not raw_sources.get(ticker)
        ]
        result = build_lineage(raw_sources)
        result["source_reports"] = source_reports
        result["missing_sources"] = missing_sources
        result["release_state"] = (
            "observed_plus_labelled_proxy" if not missing_sources else "exploratory_proxy_only"
        )
        write_json(args.output, result)
        print(
            json.dumps(
                {
                    "extended_start": result["extended_start"],
                    "extended_end": result["extended_end"],
                    "observed_start": result["observed_start"],
                    "row_count": len(result["rows"]),
                    "source_rows": result["source_rows"],
                    "missing_sources": result["missing_sources"],
                    "release_state": result["release_state"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    return 2
