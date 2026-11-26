"""Argparse subcommand registration for the data package CLI."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from moex_spread_lab.data.iss.catalog import DEFAULT_FAR, DEFAULT_ISS_BASE_URL, DEFAULT_NEAR

Handler = Callable[[Any], int]


def _add_sub(subparsers: Any, name: str, help_text: str, handler: Handler) -> Any:
    parser = subparsers.add_parser(name, help=help_text)
    parser.set_defaults(_msl_cli_handler=handler)
    return parser


def _add_root(parser: Any, default: Path | None = None) -> None:
    parser.add_argument("--root", type=Path, default=Path.cwd() if default is None else default)


def _add_lake_root(parser: Any) -> None:
    parser.add_argument("--lake-root", type=Path, default=Path("data-lake"))


def _add_format(parser: Any) -> None:
    parser.add_argument("--format", choices=("summary", "json"), default="summary")


def register_retention_parsers(subparsers: Any, handler: Handler) -> None:
    inv = _add_sub(
        subparsers,
        "data-inventory",
        "build a read-only local-data inventory and retention manifest",
        handler,
    )
    _add_root(inv)
    inv.add_argument("--output", type=Path)
    inv.add_argument("--hashes", action="store_true", help="include SHA-256 for every local file")
    inv.add_argument("--fast", action="store_true", help="skip JSON payload inspection")
    _add_format(inv)

    budget = _add_sub(
        subparsers,
        "storage-budget",
        "read-only allocated storage classes and TTL dry-runs",
        handler,
    )
    _add_root(budget)
    budget.add_argument("--archives-root", type=Path)
    budget.add_argument("--max-allocated-bytes", type=int)
    budget.add_argument("--output", type=Path)

    arc = _add_sub(
        subparsers,
        "data-archive",
        "archive verified rebuildable data caches outside the data directory",
        handler,
    )
    _add_root(arc)
    arc.add_argument("--manifest", type=Path, required=True)
    arc.add_argument("--archive", type=Path, required=True)
    arc.add_argument(
        "--apply",
        action="store_true",
        help="create a verified archive and remove its source caches",
    )
    _add_format(arc)


def register_h1_parsers(subparsers: Any, handler: Handler) -> None:
    seal = _add_sub(
        subparsers,
        "data-h1-seal",
        "activate/verify sealed H1 reads or restore JSON routing; never deletes source data",
        handler,
    )
    _add_root(seal)
    seal.add_argument("--plan", type=Path)
    seal.add_argument("--candidate", type=Path)
    seal_mode = seal.add_mutually_exclusive_group(required=True)
    for flag in ("--activate", "--verify", "--deactivate"):
        seal_mode.add_argument(flag, action="store_true")


def register_lake_parsers(subparsers: Any, handler: Handler) -> None:
    imp = _add_sub(
        subparsers,
        "data-import-candles",
        "import legacy iss-candles JSON into the local Parquet data lake",
        handler,
    )
    _add_root(imp)
    imp.add_argument("--legacy", type=Path, action="append", required=True)
    _add_lake_root(imp)
    imp.add_argument("--replace", action="store_true")
    imp.add_argument(
        "--verify", action="store_true", help="verify each Parquet import against its legacy JSON"
    )
    _add_format(imp)

    ref = _add_sub(
        subparsers,
        "data-refresh",
        "incrementally refresh candle series into legacy JSON and the verified Parquet lake",
        handler,
    )
    _add_root(ref)
    ref.add_argument("--secid", action="append", required=True)
    ref.add_argument("--interval", action="append", type=int, required=True)
    ref.add_argument("--from-date", help="required only for a first dataset or --full-refresh")
    ref.add_argument("--till-date", required=True)
    ref.add_argument("--output-dir", type=Path, default=Path("data/w1/candles"))
    _add_lake_root(ref)
    ref.add_argument("--base-url", default=DEFAULT_ISS_BASE_URL)
    ref.add_argument("--board", default="RFUD")
    ref.add_argument("--overlap-sessions", type=int, default=5)
    ref.add_argument("--chunk-days", type=int, default=90)
    ref.add_argument("--workers", type=int, default=2)
    ref.add_argument("--full-refresh", action="store_true")
    ref.add_argument("--raw-ttl-days", type=int, default=30)
    ref.add_argument("--pin-raw", action="store_true")
    ref.add_argument("--cache-dir", type=Path)
    ref.add_argument("--timeout", type=float, default=20.0)
    ref.add_argument("--delay-seconds", type=float, default=0.1)
    ref.add_argument("--retries", type=int, default=3)
    _add_format(ref)

    ver = _add_sub(
        subparsers,
        "data-verify",
        "verify every registered Parquet dataset and retained raw response",
        handler,
    )
    _add_root(ver)
    _add_lake_root(ver)
    _add_format(ver)

    comp = _add_sub(
        subparsers,
        "data-compact",
        "compact registered Parquet datasets into canonical Zstd files",
        handler,
    )
    _add_root(comp)
    _add_lake_root(comp)
    comp.add_argument(
        "--force", action="store_true", help="rewrite current single-file datasets too"
    )
    _add_format(comp)


def register_iss_and_benchmark_parsers(subparsers: Any, handler: Handler) -> None:
    iss = _add_sub(subparsers, "iss-intake", "read a current MOEX ISS futures catalog", handler)
    iss.add_argument("--output", type=Path, required=True)
    iss.add_argument("--base-url", default=DEFAULT_ISS_BASE_URL)
    iss.add_argument("--board", default="RFUD")
    iss.add_argument("--near", default=DEFAULT_NEAR)
    iss.add_argument("--far", default=DEFAULT_FAR)

    cnd = _add_sub(subparsers, "iss-candles", "download paginated ISS candle series", handler)
    cnd.add_argument("--secid", action="append", required=True)
    cnd.add_argument(
        "--interval", action="append", type=int, required=True, help="ISS interval: 1, 10, 60 or 24"
    )
    cnd.add_argument("--from-date", required=True)
    cnd.add_argument("--till-date", required=True)
    cnd.add_argument("--output-dir", type=Path, required=True)
    cnd.add_argument("--cache-dir", type=Path, default=Path("data/w1/candles-cache"))
    cnd.add_argument("--base-url", default=DEFAULT_ISS_BASE_URL)
    cnd.add_argument("--board", default="RFUD")
    cnd.add_argument("--timeout", type=float, default=20.0)
    cnd.add_argument("--delay-seconds", type=float, default=0.1)
    cnd.add_argument("--max-pages", type=int)
    cnd.add_argument("--retries", type=int, default=3)

    bench = _add_sub(
        subparsers,
        "benchmark-lineage",
        "build observed and proxy LQDT benchmark lineage",
        handler,
    )
    bench.add_argument("--from-date", default="2010-01-01")
    bench.add_argument("--till-date", required=True)
    bench.add_argument("--output", type=Path, required=True)
    bench.add_argument("--cache-dir", type=Path, default=Path("data/benchmark-cache"))
    bench.add_argument("--delay-seconds", type=float, default=0.1)
