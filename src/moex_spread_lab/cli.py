"""Command-line entry point for offline research and installed command providers."""

from __future__ import annotations

import argparse
from importlib.metadata import entry_points

from moex_spread_lab.core.w0 import W0Error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moex-spread-lab")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for entry in sorted(entry_points(group="moex_spread_lab.cli"), key=lambda ep: ep.name):
        entry.load()(subparsers)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args._msl_cli_handler(args)
    except W0Error as exc:
        print(f"W0 validation failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
