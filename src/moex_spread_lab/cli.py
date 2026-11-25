"""Command-line entry point for offline research and installed command providers."""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import entry_points

from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.remote.cli import register as register_remote


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moex-spread-lab")
    subparsers = parser.add_subparsers(dest="command", required=True)
    register_remote(subparsers)
    for entry in sorted(entry_points(group="moex_spread_lab.cli"), key=lambda ep: ep.name):
        entry.load()(subparsers)
    return parser


def main() -> int:
    # Remote commands must not import every strategy/web plugin just to contact
    # the bridge. The normal parser still discovers all installed providers.
    if len(sys.argv) > 1 and sys.argv[1] == "remote":
        parser = argparse.ArgumentParser(prog="moex-spread-lab")
        register_remote(parser.add_subparsers(dest="command", required=True))
    else:
        parser = build_parser()
    args = parser.parse_args()
    try:
        return args._msl_cli_handler(args)
    except W0Error as exc:
        print(f"W0 validation failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
