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
    local_screener_parser.set_defaults(_msl_cli_handler=run)

    daemon = subparsers.add_parser(
        "install-workstation-daemon", help="install the macOS loopback screener launch agent"
    )
    daemon.add_argument("--root", type=Path, default=Path.cwd(), help="repository checkout")
    mode = daemon.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="preview without writing")
    mode.add_argument("--apply", action="store_true", help="install and start the launch agent")
    daemon.set_defaults(_msl_cli_handler=run)


def run(args):
    if args.command == "install-workstation-daemon":
        from .workstation_daemon import install

        return install(args.root, apply=args.apply)
    if args.command == "screener":
        import signal

        from .server import ScreenerInputs, serve

        def terminate(_signum, _frame):
            raise KeyboardInterrupt

        previous = signal.signal(signal.SIGTERM, terminate)
        try:
            serve(ScreenerInputs(args.root.resolve()), args.port)
        except KeyboardInterrupt:
            return 0
        finally:
            signal.signal(signal.SIGTERM, previous)
        return 0
    return 2
