"""Command-line entry points for offline research."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from functools import partial
from pathlib import Path

from .iss import (
    DEFAULT_FAR,
    DEFAULT_ISS_BASE_URL,
    DEFAULT_NEAR,
    fetch_candle_coverage,
    fetch_iss_catalog,
    write_timeframe_inventory,
)
from .research import run_research_pipeline
from .w0 import W0Error, replay, scan_secrets, validate_manifest, write_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moex-spread-lab")
    subparsers = parser.add_subparsers(dest="command", required=True)

    replay_parser = subparsers.add_parser("replay", help="replay an offline fixture")
    replay_parser.add_argument("--manifest", type=Path, required=True)
    replay_parser.add_argument("--fixture", type=Path, required=True)
    replay_parser.add_argument("--output", type=Path, required=True)

    validate_parser = subparsers.add_parser("validate-manifest", help="validate a run manifest")
    validate_parser.add_argument("--manifest", type=Path, required=True)

    scan_parser = subparsers.add_parser("secret-scan", help="scan repository files for credential patterns")
    scan_parser.add_argument("--root", type=Path, default=Path.cwd())
    scan_parser.add_argument("--tracked-only", action="store_true")

    inventory_parser = subparsers.add_parser(
        "data-inventory", help="build a read-only local-data inventory and retention manifest",
    )
    inventory_parser.add_argument("--root", type=Path, default=Path.cwd())
    inventory_parser.add_argument("--output", type=Path)
    inventory_parser.add_argument("--hashes", action="store_true", help="include SHA-256 for every local file")
    inventory_parser.add_argument("--fast", action="store_true", help="skip JSON payload inspection")
    inventory_parser.add_argument("--format", choices=("summary", "json"), default="summary")

    archive_parser = subparsers.add_parser(
        "data-archive", help="archive verified rebuildable data caches outside the data directory",
    )
    archive_parser.add_argument("--root", type=Path, default=Path.cwd())
    archive_parser.add_argument("--manifest", type=Path, required=True)
    archive_parser.add_argument("--archive", type=Path, required=True)
    archive_parser.add_argument("--apply", action="store_true", help="create a verified archive and remove its source caches")
    archive_parser.add_argument("--format", choices=("summary", "json"), default="summary")

    import_parser = subparsers.add_parser(
        "data-import-candles", help="import legacy iss-candles JSON into the local Parquet data lake",
    )
    import_parser.add_argument("--root", type=Path, default=Path.cwd())
    import_parser.add_argument("--legacy", type=Path, action="append", required=True)
    import_parser.add_argument("--lake-root", type=Path, default=Path("data-lake"))
    import_parser.add_argument("--replace", action="store_true")
    import_parser.add_argument("--verify", action="store_true", help="verify each Parquet import against its legacy JSON")
    import_parser.add_argument("--format", choices=("summary", "json"), default="summary")

    refresh_parser = subparsers.add_parser(
        "data-refresh", help="incrementally refresh candle series into legacy JSON and the verified Parquet lake",
    )
    refresh_parser.add_argument("--root", type=Path, default=Path.cwd())
    refresh_parser.add_argument("--secid", action="append", required=True)
    refresh_parser.add_argument("--interval", action="append", type=int, required=True)
    refresh_parser.add_argument("--from-date", help="required only for a first dataset or --full-refresh")
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

    verify_parser = subparsers.add_parser("data-verify", help="verify every registered Parquet dataset and retained raw response")
    verify_parser.add_argument("--root", type=Path, default=Path.cwd())
    verify_parser.add_argument("--lake-root", type=Path, default=Path("data-lake"))
    verify_parser.add_argument("--format", choices=("summary", "json"), default="summary")

    compact_parser = subparsers.add_parser("data-compact", help="compact registered Parquet datasets into canonical Zstd files")
    compact_parser.add_argument("--root", type=Path, default=Path.cwd())
    compact_parser.add_argument("--lake-root", type=Path, default=Path("data-lake"))
    compact_parser.add_argument("--force", action="store_true", help="rewrite current single-file datasets too")
    compact_parser.add_argument("--format", choices=("summary", "json"), default="summary")

    pin_parser = subparsers.add_parser("data-pin-raw", help="pin a raw .json.zst response against TTL collection")
    pin_parser.add_argument("--root", type=Path, default=Path.cwd())
    pin_parser.add_argument("--lake-root", type=Path, default=Path("data-lake"))
    pin_parser.add_argument("--entry-id", required=True)
    pin_parser.add_argument("--format", choices=("summary", "json"), default="summary")

    gc_parser = subparsers.add_parser("data-gc", help="plan or collect expired unpinned raw .json.zst responses")
    gc_parser.add_argument("--root", type=Path, default=Path.cwd())
    gc_parser.add_argument("--lake-root", type=Path, default=Path("data-lake"))
    gc_mode = gc_parser.add_mutually_exclusive_group()
    gc_mode.add_argument("--dry-run", action="store_true", help="report files only; this is the default")
    gc_mode.add_argument("--apply", action="store_true", help="delete only expired, unpinned responses and orphans")
    gc_parser.add_argument("--format", choices=("summary", "json"), default="summary")

    artifact_pin_parser = subparsers.add_parser("run-artifact-pin", help="pin or unpin a compact evidence run's Parquet details")
    artifact_pin_parser.add_argument("--artifact-dir", type=Path, required=True)
    artifact_pin_group = artifact_pin_parser.add_mutually_exclusive_group()
    artifact_pin_group.add_argument("--pin", action="store_true", help="keep details without TTL; this is the default")
    artifact_pin_group.add_argument("--unpin", action="store_true", help="restart the artifact's configured detail TTL")
    artifact_pin_parser.add_argument("--format", choices=("summary", "json"), default="summary")

    artifact_gc_parser = subparsers.add_parser("run-artifact-gc", help="plan or collect expired unpinned Parquet run details")
    artifact_gc_parser.add_argument("--root", type=Path, default=Path.cwd())
    artifact_gc_parser.add_argument("--runs-root", type=Path, default=Path("data/analytics/runs"))
    artifact_gc_mode = artifact_gc_parser.add_mutually_exclusive_group()
    artifact_gc_mode.add_argument("--dry-run", action="store_true", help="report detail files only; this is the default")
    artifact_gc_mode.add_argument("--apply", action="store_true", help="remove expired detail files while preserving summaries and manifests")
    artifact_gc_parser.add_argument("--format", choices=("summary", "json"), default="summary")

    pipeline_parser = subparsers.add_parser("research-pipeline", help="run offline W1-W7 research waves")
    pipeline_parser.add_argument("--manifest", type=Path, required=True)
    pipeline_parser.add_argument("--fixture", type=Path, required=True)
    pipeline_parser.add_argument("--output", type=Path, required=True)

    iss_parser = subparsers.add_parser("iss-intake", help="read a current MOEX ISS futures catalog")
    iss_parser.add_argument("--output", type=Path, required=True)
    iss_parser.add_argument("--base-url", default=DEFAULT_ISS_BASE_URL)
    iss_parser.add_argument("--board", default="RFUD")
    iss_parser.add_argument("--near", default=DEFAULT_NEAR)
    iss_parser.add_argument("--far", default=DEFAULT_FAR)

    coverage_parser = subparsers.add_parser("iss-timeframe-inventory", help="build an instrument candle-timeframe inventory")
    coverage_parser.add_argument("--output", type=Path, required=True)
    coverage_parser.add_argument("--csv-output", type=Path)
    coverage_parser.add_argument("--base-url", default=DEFAULT_ISS_BASE_URL)
    coverage_parser.add_argument("--board", default="RFUD")
    coverage_parser.add_argument("--workers", type=int, default=2)
    coverage_parser.add_argument("--delay-seconds", type=float, default=0.1)
    coverage_parser.add_argument("--timeout", type=float, default=8.0)
    coverage_parser.add_argument("--cache-dir", type=Path, default=Path("data/w1/candleborders-cache"))

    candles_parser = subparsers.add_parser("iss-candles", help="download paginated ISS candle series")
    candles_parser.add_argument("--secid", action="append", required=True)
    candles_parser.add_argument("--interval", action="append", type=int, required=True, help="ISS interval: 1, 10, 60 or 24")
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

    pair_parser = subparsers.add_parser("iss-candle-pair-check", help="check synchronization and OHLC quality for two candle series")
    pair_parser.add_argument("--near", type=Path, required=True)
    pair_parser.add_argument("--far", type=Path, required=True)
    pair_parser.add_argument("--output", type=Path, required=True)

    benchmark_parser = subparsers.add_parser("benchmark-lineage", help="build observed and proxy LQDT benchmark lineage")
    benchmark_parser.add_argument("--from-date", default="2010-01-01")
    benchmark_parser.add_argument("--till-date", required=True)
    benchmark_parser.add_argument("--output", type=Path, required=True)
    benchmark_parser.add_argument("--cache-dir", type=Path, default=Path("data/benchmark-cache"))
    benchmark_parser.add_argument("--delay-seconds", type=float, default=0.1)

    baseline_parser = subparsers.add_parser("analytical-baseline", help="run a delayed candle-close spread baseline")
    baseline_parser.add_argument("--near-candles", type=Path, required=True)
    baseline_parser.add_argument("--far-candles", type=Path, required=True)
    baseline_parser.add_argument("--benchmark", type=Path, required=True)
    baseline_parser.add_argument("--contracts", type=Path, required=True)
    baseline_parser.add_argument("--output-dir", type=Path, required=True, help="new directory for summary.json and Parquet details")
    baseline_parser.add_argument("--legacy-json", type=Path, help="explicit compatibility export of the full pretty JSON report")
    baseline_parser.add_argument("--retention-days", type=int, default=30, help="retain unpinned Parquet details for this many days")
    baseline_parser.add_argument("--pin", action="store_true", help="retain this evidence run's Parquet details without TTL")
    baseline_parser.add_argument("--near", default="CNYRUBF")
    baseline_parser.add_argument("--far", default="CRZ6")
    baseline_parser.add_argument("--initial-capital", type=float, default=30000.0)
    baseline_parser.add_argument("--lookback", type=int, default=20)
    baseline_parser.add_argument("--entry-z", type=float, default=2.0)
    baseline_parser.add_argument("--exit-z", type=float, default=0.5)
    baseline_parser.add_argument("--quantity", type=int, default=1)
    baseline_parser.add_argument("--interval", type=int, default=24, choices=(1, 10, 60, 24))
    baseline_parser.add_argument("--decision-delay-bars", type=int, default=1)
    baseline_parser.add_argument("--analysis-role", choices=("primary_baseline", "diagnostic_only"), default="primary_baseline")
    cost_group = baseline_parser.add_mutually_exclusive_group()
    cost_group.add_argument("--cost-scenario", choices=("low", "base", "stress"))
    cost_group.add_argument("--cost-profile", type=Path)

    ledger_parser = subparsers.add_parser("execution-ledger", help="build a rebuildable analytical execution and portfolio ledger")
    ledger_parser.add_argument("--baseline", type=Path, required=True)
    ledger_parser.add_argument("--output", type=Path, required=True)

    validation_parser = subparsers.add_parser("validate-analytics", help="reproduce the historical W7 protocol (not the interactive screener split)")
    validation_parser.add_argument("--baseline", type=Path, required=True)
    validation_parser.add_argument("--ledger", type=Path, required=True)
    validation_parser.add_argument("--sensitivity", type=Path, action="append", default=[])
    validation_parser.add_argument("--output", type=Path, required=True)
    validation_parser.add_argument("--train-fraction", type=float, default=0.6)
    validation_parser.add_argument("--confirmation-fraction", type=float, default=0.2)

    carry_parser = subparsers.add_parser("carry-study", help="build a funding-aware EOD carry decomposition")
    carry_parser.add_argument("--near", default="CNYRUBF")
    carry_parser.add_argument("--far", default="CRZ6")
    carry_parser.add_argument("--from-date", required=True)
    carry_parser.add_argument("--till-date", required=True)
    carry_parser.add_argument("--contracts", type=Path, required=True)
    carry_parser.add_argument("--output", type=Path, required=True)
    carry_parser.add_argument("--cache-dir", type=Path, default=Path("data/carry-history-cache"))
    carry_parser.add_argument("--delay-seconds", type=float, default=0.1)
    carry_parser.add_argument("--timeout", type=float, default=30.0)
    carry_strategy_parser = subparsers.add_parser("carry-strategy", help="run the frozen funding-aware carry proxy strategy")
    carry_strategy_parser.add_argument("--carry-study", type=Path, required=True)
    carry_strategy_parser.add_argument("--benchmark", type=Path, required=True)
    carry_strategy_parser.add_argument("--contracts", type=Path, required=True)
    carry_strategy_parser.add_argument("--output", type=Path, required=True)
    carry_strategy_parser.add_argument("--cost-scenario", choices=("low", "base", "stress", "moex_current_taker", "vtb_online_prime_current", "tariff"), default="base")
    carry_strategy_parser.add_argument("--initial-capital", type=float, default=30000.0)
    carry_strategy_parser.add_argument("--hypothesis", choices=("h2-s1", "h3-funding-only"), default="h2-s1")
    account_risk_parser = subparsers.add_parser("account-risk-check", help="run the working VTB EBS/KPUR margin stress screen")
    account_risk_parser.add_argument("--near", default="CNYRUBF")
    account_risk_parser.add_argument("--far", default="CRZ6")
    account_risk_parser.add_argument("--contracts", type=Path, required=True)
    account_risk_parser.add_argument("--carry-study", type=Path)
    account_risk_parser.add_argument("--output", type=Path, required=True)
    account_risk_parser.add_argument("--initial-capital", type=float, default=30000.0)
    account_risk_parser.add_argument("--stress-vm-buffer", type=float, default=5000.0)
    account_risk_parser.add_argument("--uncertainty-buffer", type=float, default=5000.0)
    account_risk_parser.add_argument("--key-rate", type=float, default=0.14)
    screen_parser = subparsers.add_parser("pair-screen", help="screen documented futures pairs with the fixed H1 control and account stress gate")
    screen_parser.add_argument("--candles-dir", type=Path, required=True)
    screen_parser.add_argument("--benchmark", type=Path, required=True)
    screen_parser.add_argument("--contracts", type=Path, required=True)
    screen_parser.add_argument("--output", type=Path, required=True)
    screen_parser.add_argument("--initial-capital", type=float, default=30000.0)
    funding_screen_parser = subparsers.add_parser("funding-screen", help="screen flagship perpetual-to-quarterly pairs with the frozen funding-only hypothesis")
    funding_screen_parser.add_argument("--from-date", required=True)
    funding_screen_parser.add_argument("--till-date", required=True)
    funding_screen_parser.add_argument("--contracts", type=Path, required=True)
    funding_screen_parser.add_argument("--benchmark", type=Path, required=True)
    funding_screen_parser.add_argument("--cache-dir", type=Path, default=Path("data/carry-screen-history-cache"))
    funding_screen_parser.add_argument("--output", type=Path, required=True)
    funding_screen_parser.add_argument("--initial-capital", type=float, default=30000.0)
    funding_screen_parser.add_argument("--cost-scenario", choices=("low", "base", "stress", "tariff"), default="stress")
    funding_screen_parser.add_argument("--delay-seconds", type=float, default=0.2)
    funding_screen_parser.add_argument("--timeout", type=float, default=60.0)

    audit_parser = subparsers.add_parser("audit-pairs", help="independently recompute and audit the screener's price-only control for every local pair")
    audit_parser.add_argument("--root", type=Path, default=Path.cwd())
    audit_parser.add_argument("--output", type=Path, required=True)

    s3_parser = subparsers.add_parser("s3-reference", help="reproduce the reference SBRF calendar backtest and stress its fills and liquidity")
    s3_parser.add_argument("--reference-dir", type=Path, required=True, help="checkout of the reference calendar-spread project")
    s3_parser.add_argument("--benchmark", type=Path, default=Path("data/benchmark/lqdt-lineage.json"))
    s3_parser.add_argument("--output", type=Path, required=True)

    calendar_parser = subparsers.add_parser("calendar-study", help="study native MOEX calendar spreads over front windows (fetches and caches ISS history)")
    calendar_parser.add_argument("--root", type=Path, default=Path.cwd())
    calendar_parser.add_argument("--till-date", required=True)
    calendar_parser.add_argument("--slippage-ticks", type=float, default=1.0)
    calendar_parser.add_argument("--assets", nargs="+", default=None, help="asset prefixes; default is every registered asset")
    calendar_parser.add_argument("--output", type=Path, required=True)

    local_screener_parser = subparsers.add_parser("screener", help="serve the private offline research screener on loopback")
    local_screener_parser.add_argument("--root", type=Path, default=Path.cwd(), help="repository with local ignored research inputs")
    local_screener_parser.add_argument("--port", type=int, default=8765)

    screener_run_parser = subparsers.add_parser("screener-run", help="replay one local screener configuration as JSON")
    screener_run_parser.add_argument("--root", type=Path, default=Path.cwd())
    screener_run_parser.add_argument("--pair", required=True)
    screener_run_parser.add_argument("--strategy", choices=("mean_reversion", "funding_only"), required=True)
    screener_run_parser.add_argument("--slippage-ticks", type=float, default=1.0)
    screener_run_parser.add_argument("--quantity", type=int, default=1)
    screener_run_parser.add_argument("--lookback", type=int, default=20)
    screener_run_parser.add_argument("--entry-z", type=float, default=2.0)
    screener_run_parser.add_argument("--exit-z", type=float, default=0.5)
    screener_run_parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "replay":
            result = replay(args.manifest, args.fixture)
            write_json(args.output, result)
            print(json.dumps({key: result[key] for key in result if key != "normalized_fixture"}, sort_keys=True))
            return 0
        if args.command == "validate-manifest":
            manifest = validate_manifest(args.manifest)
            print(json.dumps({"run_id": manifest["run_id"], "config_hash": manifest["config_hash"]}, sort_keys=True))
            return 0
        if args.command == "secret-scan":
            findings = scan_secrets(args.root.resolve(), tracked_only=args.tracked_only)
            if findings:
                print(json.dumps({"status": "failed", "findings": findings}, sort_keys=True))
                return 1
            print(json.dumps({"status": "passed", "findings": []}, sort_keys=True))
            return 0
        if args.command == "data-inventory":
            from .data_inventory import build_data_inventory

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
                print(json.dumps({
                    "status": "ok",
                    "schema_version": result["schema_version"],
                    "summary": result["summary"],
                    "output": str(args.output) if args.output else None,
                }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "data-archive":
            from .data_retention import archive_rebuildable_caches, plan_rebuildable_cache_archive

            result = (
                archive_rebuildable_caches(args.root, args.manifest, args.archive)
                if args.apply
                else plan_rebuildable_cache_archive(args.root, args.manifest, args.archive)
            )
            if args.format == "json":
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                print(json.dumps({
                    key: result[key]
                    for key in (
                        "schema_version", "status", "archive", "classification", "file_count",
                        "logical_bytes", "allocated_bytes", "archive_logical_bytes", "deleted_file_count",
                        "requires_apply",
                    )
                    if key in result
                }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "data-import-candles":
            from .data_lake import ParquetBarStore

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
                print(json.dumps({
                    "status": "ok",
                    "dataset_count": len(records),
                    "row_count": sum(record["row_count"] for record in records),
                    "datasets": [record["dataset_id"] for record in records],
                }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "data-refresh":
            from .data_refresh import RefreshTarget, refresh_candle_datasets

            root = args.root.resolve()
            lake_root = args.lake_root if args.lake_root.is_absolute() else root / args.lake_root
            output = refresh_candle_datasets(
                root=root,
                targets=[RefreshTarget(secid, interval) for secid in args.secid for interval in args.interval],
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
                print(json.dumps({
                    "status": output["status"],
                    "dataset_count": len(output["datasets"]),
                    "scheduled_chunk_count": output["scheduled_chunk_count"],
                    "datasets": output["datasets"],
                }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command in {"data-verify", "data-compact", "data-pin-raw", "data-gc"}:
            from .data_lake import ParquetBarStore

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
                print(json.dumps({
                    key: output[key]
                    for key in ("status", "dataset_count", "expired_entry_count", "orphan_file_count", "retained_entry_count")
                    if key in output
                }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "run-artifact-pin":
            from .run_artifacts import pin_run_artifact

            manifest = pin_run_artifact(args.artifact_dir, pinned=not args.unpin)
            output = {"status": "pinned" if not args.unpin else "unpinned", "manifest": manifest}
            if args.format == "json":
                print(json.dumps(output, ensure_ascii=False, sort_keys=True))
            else:
                print(json.dumps({
                    "status": output["status"],
                    "artifact_id": manifest["artifact_id"],
                    "pinned": manifest["retention"]["pinned"],
                    "expires_at": manifest["retention"]["expires_at"],
                }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "run-artifact-gc":
            from .run_artifacts import gc_run_artifacts

            root = args.root.resolve()
            runs_root = args.runs_root if args.runs_root.is_absolute() else root / args.runs_root
            output = gc_run_artifacts(runs_root, dry_run=not args.apply)
            if args.format == "json":
                print(json.dumps(output, ensure_ascii=False, sort_keys=True))
            else:
                print(json.dumps({
                    key: output[key]
                    for key in ("status", "expired_artifact_count", "retained_artifact_count")
                }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "research-pipeline":
            result = run_research_pipeline(args.manifest, args.fixture)
            write_json(args.output, result)
            print(json.dumps({
                "all_fixture_gates_passed": result["all_fixture_gates_passed"],
                "release_state": result["release_state"],
                "report_hash": result["report_hash"],
            }, sort_keys=True))
            return 0
        if args.command == "iss-intake":
            result = fetch_iss_catalog(
                base_url=args.base_url,
                board=args.board,
                near_secid=args.near,
                far_secid=args.far,
            )
            write_json(args.output, result)
            print(json.dumps({
                "admission_status": result["admission_status"],
                "candidate": result["candidate"]["candidate_id"],
                "instrument_count": result["instrument_count"],
                "raw_response_sha256": result["source"]["raw_response_sha256"],
                "rights_status": result["source"]["rights_status"],
            }, ensure_ascii=False, sort_keys=True))
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
            print(json.dumps({
                "instrument_count": inventory["instrument_count"],
                "coverage_status_counts": inventory["coverage_status_counts"],
                "minimum_timeframe_counts": inventory["minimum_timeframe_counts"],
                "output": str(args.output),
                "csv_output": str(args.csv_output) if args.csv_output else None,
            }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "iss-candles":
            from .iss import fetch_candle_series

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
                    results.append({
                        "secid": secid,
                        "timeframe": result["timeframe"],
                        "row_count": result["row_count"],
                        "page_count": result["page_count"],
                        "duplicate_count": result["duplicate_count"],
                        "output": str(output),
                    })
            summary = args.output_dir / "summary.json"
            write_json(summary, {
                "schema_version": "iss-candle-intake-summary.v1",
                "from": args.from_date,
                "till": args.till_date,
                "price_interpretation": "candle_price_proxy_for_medium_term_analysis",
                "series": results,
            })
            print(json.dumps({"series": results, "summary": str(summary)}, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "iss-candle-pair-check":
            from .iss import analyze_candle_pair

            result = analyze_candle_pair(args.near, args.far)
            write_json(args.output, result)
            print(json.dumps({
                "timeframe": result["timeframe"],
                "common_timestamp_count": result["synchronization"]["common_timestamp_count"],
                "near_only_count": result["synchronization"]["near_only_count"],
                "far_only_count": result["synchronization"]["far_only_count"],
                "status": result["synchronization"]["pair_analytical_status"],
            }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "benchmark-lineage":
            from .benchmark import (
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
                path.write_text(json.dumps(source, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
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
                source_reports[ticker] = source_report(source, board=source.get("board"), page_count=source["page_count"])

            rusfar = load_source("RUSFAR", fetch_rusfar_rates)
            raw_sources["RUSFAR"] = rate_rows_to_price(rusfar["rows"], "RUSFAR")
            source_reports["RUSFAR"] = source_report(rusfar, page_count=rusfar["page_count"])

            ruonia = load_source(
                "CBR_RUONIA", fetch_cbr_ruonia_rates,
                valid=lambda value: value.get("status") == "available" and "methods" in value,
            )
            if ruonia["rows"]:
                raw_sources["CBR_RUONIA"] = rate_rows_to_price(ruonia["rows"], "CBR_RUONIA")
            source_reports["CBR_RUONIA"] = source_report(
                ruonia, request_count=ruonia["request_count"], methods=ruonia.get("methods", []), status=ruonia["status"],
            )
            missing_sources = [ticker for ticker in ("CBR_RUONIA", "RUSFAR", "FXMM", "VTBM", "LQDT") if not raw_sources.get(ticker)]
            result = build_lineage(raw_sources)
            result["source_reports"] = source_reports
            result["missing_sources"] = missing_sources
            result["release_state"] = "observed_plus_labelled_proxy" if not missing_sources else "exploratory_proxy_only"
            write_json(args.output, result)
            print(json.dumps({
                "extended_start": result["extended_start"],
                "extended_end": result["extended_end"],
                "observed_start": result["observed_start"],
                "row_count": len(result["rows"]),
                "source_rows": result["source_rows"],
                "missing_sources": result["missing_sources"],
                "release_state": result["release_state"],
            }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "analytical-baseline":
            from .analytics import build_series, get_cost_scenario, run_mean_reversion_baseline

            series = build_series(
                args.near_candles,
                args.far_candles,
                args.benchmark,
                args.contracts,
                near=args.near,
                far=args.far,
                interval=args.interval,
            )
            cost_profile = None
            if args.cost_scenario:
                cost_profile = get_cost_scenario(args.cost_scenario)
            elif args.cost_profile:
                try:
                    cost_profile = json.loads(args.cost_profile.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise W0Error(f"cannot read cost profile {args.cost_profile}: {exc}") from exc
                if not isinstance(cost_profile, dict):
                    raise W0Error("cost profile must be a JSON object")
            result = run_mean_reversion_baseline(
                series,
                initial_capital=args.initial_capital,
                lookback=args.lookback,
                entry_z=args.entry_z,
                exit_z=args.exit_z,
                quantity=args.quantity,
                decision_delay_bars=args.decision_delay_bars,
                analysis_role=args.analysis_role,
                cost_profile=cost_profile,
            )
            from .run_artifacts import publish_baseline_artifact, write_legacy_json

            artifact = publish_baseline_artifact(
                args.output_dir, result, retention_days=args.retention_days, pin=args.pin,
            )
            if args.legacy_json is not None:
                write_legacy_json(args.legacy_json, result)
            print(json.dumps({
                "release_state": result["release_state"],
                "timeframe": result["timeframe"],
                "analysis_role": result["analysis_role"],
                "first_date": result["summary"]["first_date"],
                "last_date": result["summary"]["last_date"],
                "common_row_count": result["summary"]["common_row_count"],
                "trade_count": result["summary"]["trade_count"],
                "gross_pnl": result["summary"]["gross_pnl"],
                "final_X": result["summary"]["final_X"],
                "max_drawdown_D": result["summary"]["max_drawdown_D"],
                "cost_status": result["costs"]["status"],
                "artifact_dir": artifact["artifact_dir"],
                "artifact_id": artifact["artifact_id"],
                "legacy_json": str(args.legacy_json) if args.legacy_json else None,
            }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "execution-ledger":
            from .ledger import build_ledger

            result = build_ledger(args.baseline)
            write_json(args.output, result)
            print(json.dumps({
                "release_state": result["release_state"],
                "timeframe": result["timeframe"],
                "execution_attempt_count": result["summary"]["execution_attempt_count"],
                "final_X": result["summary"]["final_X"],
                "executable_value_available": result["summary"]["executable_value_available"],
                "output": str(args.output),
            }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "validate-analytics":
            from .validation import build_validation

            result = build_validation(
                args.baseline,
                args.ledger,
                args.sensitivity,
                train_fraction=args.train_fraction,
                confirmation_fraction=args.confirmation_fraction,
            )
            write_json(args.output, result)
            print(json.dumps({
                "release_state": result["release_state"],
                "verdict": result["verdict"],
                "timeframe": result["timeframe"],
                "sealed_confirmation_rows": result["splits"]["sealed_confirmation"]["row_count"],
                "final_X": result["splits"]["sealed_confirmation"]["final_X"],
                "blocker_count": len(result["conclusion_blockers"]),
                "output": str(args.output),
            }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "carry-study":
            from .carry import build_carry_study, fetch_history

            near_source, far_source = (
                fetch_history(secid, args.from_date, args.till_date, cache_dir=args.cache_dir,
                              delay_seconds=args.delay_seconds, timeout=args.timeout)
                for secid in (args.near, args.far)
            )
            result = build_carry_study(
                near_source,
                far_source,
                args.contracts,
                near=args.near,
                far=args.far,
                funding_regimes=None if args.near == "CNYRUBF" else (),
            )
            write_json(args.output, result)
            print(json.dumps({
                "release_state": result["release_state"],
                "study_role": result["study_role"],
                "near": result["near"],
                "far": result["far"],
                "common_row_count": result["summary"]["common_row_count"],
                "total_basis_pnl": result["summary"]["total_basis_pnl"],
                "total_funding_received": result["summary"]["total_funding_received"],
                "total_gross_carry_pnl": result["summary"]["total_gross_carry_pnl"],
                "output": str(args.output),
            }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "carry-strategy":
            from .carry import build_carry_strategy

            try:
                carry_study = json.loads(args.carry_study.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise W0Error(f"cannot read carry study {args.carry_study}: {exc}") from exc
            result = build_carry_strategy(
                carry_study,
                args.benchmark,
                args.contracts,
                transaction_cost_scenario=args.cost_scenario,
                initial_capital=args.initial_capital,
                strategy_variant="funding_only" if args.hypothesis == "h3-funding-only" else "basis_median",
            )
            write_json(args.output, result)
            print(json.dumps({
                "rule_id": result["rule_id"],
                "release_state": result["release_state"],
                "closed_trade_count": result["summary"]["closed_trade_count"],
                "open_trade_count": result["summary"]["open_trade_count"],
                "final_X": result["summary"]["final_X"],
                "gross_pnl": result["summary"]["gross_pnl"],
                "transaction_costs": result["summary"]["transaction_costs"],
                "output": str(args.output),
            }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "account-risk-check":
            from .account_risk import assess_vtb_pair

            observed_daily_pnl = None
            if args.carry_study:
                try:
                    carry_study = json.loads(args.carry_study.read_text(encoding="utf-8"))
                    observed_daily_pnl = [
                        float(row["daily_gross_carry_pnl"])
                        for row in carry_study.get("rows", [])[1:]
                    ]
                except (OSError, KeyError, TypeError, ValueError) as exc:
                    raise W0Error(f"cannot read carry study for risk screen: {exc}") from exc
            result = assess_vtb_pair(
                args.contracts,
                args.near,
                args.far,
                initial_capital=args.initial_capital,
                stress_vm_buffer=args.stress_vm_buffer,
                uncertainty_buffer=args.uncertainty_buffer,
                key_rate=args.key_rate,
                observed_daily_pnl=observed_daily_pnl,
            )
            write_json(args.output, result)
            print(json.dumps({
                "schema_version": result["schema_version"],
                "current_snapshot_passed": result["summary"]["current_snapshot_passed"],
                "all_working_scenarios_passed": result["summary"]["all_scenarios_passed"],
                "pair_margin_no_offset_rub": result["pair"]["pair_margin_no_offset_rub"],
                "most_severe_remaining_capital_rub": result["summary"]["most_severe_remaining_capital_rub"],
                "output": str(args.output),
            }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "pair-screen":
            from .screener import screen_pairs

            result = screen_pairs(
                args.candles_dir,
                args.benchmark,
                args.contracts,
                initial_capital=args.initial_capital,
            )
            write_json(args.output, result)
            print(json.dumps({
                "schema_version": result["schema_version"],
                "candidate_count": result["summary"]["candidate_count"],
                "stress_pass_count": result["summary"]["stress_pass_count"],
                "positive_h1_control_count": result["summary"]["positive_h1_control_count"],
                "output": str(args.output),
            }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "screener":
            from .screener_app import ScreenerInputs, serve

            serve(ScreenerInputs(args.root.resolve()), args.port)
            return 0
        if args.command == "screener-run":
            from .screener_app import ScreenerInputs, build_result

            parameters = {"lookback": args.lookback}
            if args.strategy == "mean_reversion":
                parameters.update({"entry_z": args.entry_z, "exit_z": args.exit_z})
            result = build_result(ScreenerInputs(args.root.resolve()), {
                "pair": args.pair, "strategy": args.strategy, "slippage_ticks": args.slippage_ticks,
                "quantity": args.quantity, "parameters": parameters,
            })
            write_json(args.output, result)
            print(json.dumps({"run_id": result["run_id"], "status": result["status"], "output": str(args.output)}, sort_keys=True))
            return 0
        if args.command == "audit-pairs":
            from .audit import audit_pairs
            from .screener_app import ScreenerInputs

            result = audit_pairs(ScreenerInputs(args.root.resolve()))
            write_json(args.output, result)
            print(json.dumps({**result["summary"], "output": str(args.output)}, sort_keys=True))
            return 0
        if args.command == "calendar-study":
            from .calendar_spreads import ASSETS, run_calendar_study

            root = args.root.resolve()
            result = run_calendar_study(
                root / "data/spread-history-cache", root / "data/benchmark/lqdt-lineage.json", root / "data/benchmark-cache",
                root / "data/w1/current-intake.json", assets=tuple(args.assets or ASSETS), slippage_ticks=args.slippage_ticks, till_date=args.till_date,
            )
            write_json(args.output, result)
            for asset, item in result["assets"].items():
                for variant in item.get("variants", []):
                    print(json.dumps({"asset": asset, **{k: variant[k] for k in (
                        "strategy", "variant", "trades", "windows_with_trades", "windows_net_positive", "gross_rub", "costs_rub",
                        "net_rub", "excess_over_lqdt_on_margin_rub", "excess_pp_per_year_on_allocation")}}, sort_keys=True))
            return 0
        if args.command == "s3-reference":
            from .s3_reference import run_s3

            result = run_s3(args.reference_dir, args.benchmark)
            write_json(args.output, result)
            for variant in result["variants"]:
                print(json.dumps({key: variant[key] for key in ("strategy", "variant", "cost_model", "trades", "gross_rub", "net_rub", "excess_over_lqdt_on_margin_rub")}, sort_keys=True))
            return 0
        if args.command == "funding-screen":
            from .funding_screen import screen_funding_pairs

            result = screen_funding_pairs(
                contracts_path=args.contracts,
                benchmark_path=args.benchmark,
                cache_dir=args.cache_dir,
                from_date=args.from_date,
                till_date=args.till_date,
                initial_capital=args.initial_capital,
                cost_scenario=args.cost_scenario,
                delay_seconds=args.delay_seconds,
                timeout=args.timeout,
            )
            write_json(args.output, result)
            print(json.dumps({
                "schema_version": result["schema_version"],
                "candidate_count": result["summary"]["candidate_count"],
                "stress_pass_count": result["summary"]["stress_pass_count"],
                "positive_final_X_count": result["summary"]["positive_final_X_count"],
                "output": str(args.output),
            }, ensure_ascii=False, sort_keys=True))
            return 0
    except W0Error as exc:
        print(f"W0 validation failed: {exc}")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
