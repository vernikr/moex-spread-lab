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
            findings = scan_secrets(args.root.resolve())
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
    except W0Error as exc:
        print(f"W0 validation failed: {exc}")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
