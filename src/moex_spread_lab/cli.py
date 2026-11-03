"""Command-line entry points for the W0 reproducibility package."""

from __future__ import annotations

import argparse
import json
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
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
            from .iss import fetch_candle_series, write_candle_series

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
                    write_candle_series(result, output)
                    results.append({
                        "secid": secid,
                        "timeframe": result["timeframe"],
                        "row_count": result["row_count"],
                        "page_count": result["page_count"],
                        "duplicate_count": result["duplicate_count"],
                        "output": str(output),
                    })
            summary = args.output_dir / "summary.json"
            summary.parent.mkdir(parents=True, exist_ok=True)
            summary.write_text(json.dumps({
                "schema_version": "iss-candle-intake-summary.v1",
                "from": args.from_date,
                "till": args.till_date,
                "price_interpretation": "candle_price_proxy_for_medium_term_analysis",
                "series": results,
            }, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(json.dumps({"series": results, "summary": str(summary)}, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "iss-candle-pair-check":
            from .iss import analyze_candle_pair, write_pair_quality

            result = analyze_candle_pair(args.near, args.far)
            write_pair_quality(result, args.output)
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
                write_benchmark,
            )

            def cached_source(path: Path) -> dict | None:
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    return None
                return value if isinstance(value, dict) else None

            raw_sources = {}
            source_reports = {}

            def provenance(rows: list[dict]) -> dict:
                boards = {}
                for row in rows:
                    board = row.get("board") or "not_applicable"
                    boards[board] = boards.get(board, 0) + 1
                return {
                    "first_date": rows[0]["date"] if rows else None,
                    "last_date": rows[-1]["date"] if rows else None,
                    "board_row_counts": boards,
                }

            for ticker in ("FXMM", "VTBM", "LQDT"):
                cache_path = args.cache_dir / f"{ticker}.json"
                cached = cached_source(cache_path) if cache_path.exists() else None
                if cached and cached.get("from") == args.from_date and cached.get("till") == args.till_date:
                    source = cached
                else:
                    source = fetch_moex_history(ticker, args.from_date, args.till_date, delay_seconds=args.delay_seconds)
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(json.dumps(source, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
                raw_sources[ticker] = source["rows"]
                source_reports[ticker] = {
                    "source": source["source"],
                    "board": source.get("board"),
                    "page_count": source["page_count"],
                    "row_count": len(source["rows"]),
                    "raw_response_hashes": source["raw_response_hashes"],
                    **provenance(source["rows"]),
                }

            rusfar_cache = args.cache_dir / "RUSFAR.json"
            cached = cached_source(rusfar_cache) if rusfar_cache.exists() else None
            if cached and cached.get("from") == args.from_date and cached.get("till") == args.till_date:
                rusfar_source = cached
            else:
                rusfar_source = fetch_rusfar_rates(args.from_date, args.till_date, delay_seconds=args.delay_seconds)
                rusfar_cache.parent.mkdir(parents=True, exist_ok=True)
                rusfar_cache.write_text(json.dumps(rusfar_source, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            raw_sources["RUSFAR"] = rate_rows_to_price(rusfar_source["rows"], "RUSFAR")
            source_reports["RUSFAR"] = {
                "source": rusfar_source["source"],
                "page_count": rusfar_source["page_count"],
                "row_count": len(rusfar_source["rows"]),
                "raw_response_hashes": rusfar_source["raw_response_hashes"],
                **provenance(rusfar_source["rows"]),
            }

            ruonia_cache = args.cache_dir / "CBR_RUONIA.json"
            cached = cached_source(ruonia_cache) if ruonia_cache.exists() else None
            if (
                cached
                and cached.get("from") == args.from_date
                and cached.get("till") == args.till_date
                and cached.get("status") == "available"
                and "methods" in cached
            ):
                ruonia_source = cached
            else:
                ruonia_source = fetch_cbr_ruonia_rates(args.from_date, args.till_date, delay_seconds=args.delay_seconds)
                ruonia_cache.parent.mkdir(parents=True, exist_ok=True)
                ruonia_cache.write_text(json.dumps(ruonia_source, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            if ruonia_source["rows"]:
                raw_sources["CBR_RUONIA"] = rate_rows_to_price(ruonia_source["rows"], "CBR_RUONIA")
            source_reports["CBR_RUONIA"] = {
                "source": ruonia_source["source"],
                "request_count": ruonia_source["request_count"],
                "methods": ruonia_source.get("methods", []),
                "row_count": len(ruonia_source["rows"]),
                "status": ruonia_source["status"],
                "raw_response_hashes": ruonia_source["raw_response_hashes"],
                **provenance(ruonia_source["rows"]),
            }
            missing_sources = [ticker for ticker in ("CBR_RUONIA", "RUSFAR", "FXMM", "VTBM", "LQDT") if not raw_sources.get(ticker)]
            result = build_lineage(raw_sources)
            result["source_reports"] = source_reports
            result["missing_sources"] = missing_sources
            result["release_state"] = "observed_plus_labelled_proxy" if not missing_sources else "exploratory_proxy_only"
            write_benchmark(result, args.output)
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
    except W0Error as exc:
        print(f"W0 validation failed: {exc}")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
