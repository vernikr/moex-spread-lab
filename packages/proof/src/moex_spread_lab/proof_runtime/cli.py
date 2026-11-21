"""CLI commands owned by the proof package."""

from __future__ import annotations

import json
from pathlib import Path

from moex_spread_lab.core.w0 import write_json


def register(subparsers):
    audit_parser = subparsers.add_parser("audit-pairs", help="independently recompute and audit the screener's price-only control for every local pair")
    audit_parser.add_argument("--root", type=Path, default=Path.cwd())
    audit_parser.add_argument("--output", type=Path, required=True)
    audit_parser.set_defaults(_msl_cli_handler=run)



def run(args):
    if args.command == "audit-pairs":
        from moex_spread_lab.proof_runtime.audit import audit_pairs
        from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs

        result = audit_pairs(ScreenerInputs(args.root.resolve()))
        write_json(args.output, result)
        print(json.dumps({**result["summary"], "output": str(args.output)}, sort_keys=True))
        return 0
    return 2
