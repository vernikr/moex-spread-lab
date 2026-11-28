"""CLI commands owned by the core package."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .w0 import W0Error, scan_secrets_detailed


def register(subparsers: Any) -> None:
    scan_parser = subparsers.add_parser(
        "secret-scan", help="scan repository files for credential patterns"
    )
    scan_parser.add_argument("--root", type=Path, default=Path.cwd())
    scan_parser.add_argument("--tracked-only", action="store_true")
    scan_parser.set_defaults(_msl_cli_handler=run)


def run(args: Any) -> int:
    if args.command == "secret-scan":
        try:
            report = scan_secrets_detailed(args.root.resolve(), tracked_only=args.tracked_only)
        except W0Error as exc:
            print(json.dumps({"status": "error", "detail": str(exc)}, sort_keys=True))
            return 2
        findings, skipped = report["findings"], report["skipped"]
        if findings:
            print(
                json.dumps(
                    {"status": "failed", "findings": findings, "skipped": skipped},
                    sort_keys=True,
                )
            )
            return 1
        print(json.dumps({"status": "passed", "findings": [], "skipped": skipped}, sort_keys=True))
        return 0
    return 2
