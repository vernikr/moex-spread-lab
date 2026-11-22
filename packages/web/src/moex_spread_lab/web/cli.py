"""CLI commands owned by the web package."""

from __future__ import annotations

from pathlib import Path


def register(subparsers):
    local_screener_parser = subparsers.add_parser(
        "screener", help="serve the private offline research screener on loopback"
    )
    local_screener_parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository with local ignored research inputs",
    )
    local_screener_parser.add_argument("--port", type=int, default=8765)
    local_screener_parser.add_argument(
        "--host",
        choices=("127.0.0.1", "0.0.0.0"),
        default="127.0.0.1",
        help="0.0.0.0 only for a sandbox preview; the API keeps its loopback-only route guard",
    )
    local_screener_parser.set_defaults(_msl_cli_handler=run)


def run(args):
    if args.command == "screener":
        from .server import ScreenerInputs, serve

        serve(ScreenerInputs(args.root.resolve()), args.port, args.host)
        return 0
    return 2
