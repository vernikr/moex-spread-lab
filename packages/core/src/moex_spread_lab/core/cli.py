"""CLI commands owned by the core package."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .w0 import scan_secrets, validate_manifest


def register(subparsers: Any) -> None:
    validate_parser = subparsers.add_parser("validate-manifest", help="validate a run manifest")
    validate_parser.add_argument("--manifest", type=Path, required=True)
    validate_parser.set_defaults(_msl_cli_handler=run)

    scan_parser = subparsers.add_parser(
        "secret-scan", help="scan repository files for credential patterns"
    )
    scan_parser.add_argument("--root", type=Path, default=Path.cwd())
    scan_parser.add_argument("--tracked-only", action="store_true")
    scan_parser.set_defaults(_msl_cli_handler=run)


def run(args: Any) -> int:
    if args.command == "validate-manifest":
        manifest = validate_manifest(args.manifest)
        print(
            json.dumps(
                {"run_id": manifest["run_id"], "config_hash": manifest["config_hash"]},
                sort_keys=True,
            )
        )
        return 0
    if args.command == "secret-scan":
        findings = scan_secrets(args.root.resolve(), tracked_only=args.tracked_only)
        if findings:
            print(json.dumps({"status": "failed", "findings": findings}, sort_keys=True))
            return 1
        print(json.dumps({"status": "passed", "findings": []}, sort_keys=True))
        return 0
    return 2
