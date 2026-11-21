"""CLI commands owned by the core package."""

from __future__ import annotations

import json
from pathlib import Path

from moex_spread_lab.core.w0 import replay, scan_secrets, validate_manifest, write_json


def register(subparsers):
    replay_parser = subparsers.add_parser("replay", help="replay an offline fixture")
    replay_parser.add_argument("--manifest", type=Path, required=True)
    replay_parser.add_argument("--fixture", type=Path, required=True)
    replay_parser.add_argument("--output", type=Path, required=True)
    replay_parser.set_defaults(_msl_cli_handler=run)

    validate_parser = subparsers.add_parser("validate-manifest", help="validate a run manifest")
    validate_parser.add_argument("--manifest", type=Path, required=True)
    validate_parser.set_defaults(_msl_cli_handler=run)

    scan_parser = subparsers.add_parser("secret-scan", help="scan repository files for credential patterns")
    scan_parser.add_argument("--root", type=Path, default=Path.cwd())
    scan_parser.add_argument("--tracked-only", action="store_true")
    scan_parser.set_defaults(_msl_cli_handler=run)



def run(args):
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
    return 2
