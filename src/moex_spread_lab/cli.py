"""Command-line entry points for the W0 reproducibility package."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

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
    except W0Error as exc:
        print(f"W0 validation failed: {exc}")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
