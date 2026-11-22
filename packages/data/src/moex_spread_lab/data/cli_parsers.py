"""Argparse subcommand registration for the data package CLI."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from moex_spread_lab.data.iss.catalog import (
    DEFAULT_FAR,
    DEFAULT_ISS_BASE_URL,
    DEFAULT_NEAR,
)

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
    cand = _add_sub(
        subparsers,
        "data-archive-candidates",
        "plan, verify or explicitly apply research candidate archival",
        handler,
    )
    _add_root(cand, Path("."))
    cand.add_argument(
        "--plan",
        type=Path,
        required=True,
        help="new dry-run plan outside data/; required input for other modes",
    )
    cand.add_argument("--archive", type=Path, help="new tar.zst destination, dry-run only")
    mode = cand.add_mutually_exclusive_group(required=True)
    for flag in ("--dry-run", "--archive-only", "--apply"):
        mode.add_argument(flag, action="store_true")

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

    cat = _add_sub(
        subparsers,
        "archive-catalog",
        "verify archives and publish a no-clobber recovery catalog",
        handler,
    )
    cat.add_argument("--archive", type=Path, action="append", required=True)
    cat.add_argument("--review-date", required=True)
    cat.add_argument("--output", type=Path, required=True)

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
    h1_arc = _add_sub(
        subparsers,
        "data-h1-archive",
        "archive/apply exact sealed H1 sources with restore, evidence and 80%% gain gates",
        handler,
    )
    _add_root(h1_arc)
    h1_arc.add_argument("--plan", type=Path, required=True)
    h1_arc.add_argument("--archive", type=Path)
    h1_arc.add_argument("--evidence", type=Path)
    h1_arc.add_argument(
        "--retry-restored",
        action="store_true",
        help="explicitly retry apply only after all originals are restored; preserve prior journal",
    )
    h1_arc_mode = h1_arc.add_mutually_exclusive_group(required=True)
    for flag in ("--dry-run", "--archive-only", "--apply"):
        h1_arc_mode.add_argument(flag, action="store_true")

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

    h1 = _add_sub(
        subparsers,
        "data-h1-stage",
        "plan and verify isolated H1 candidate lakes; no runtime activation or deletion",
        handler,
    )
    _add_root(h1)
    h1.add_argument("--plan", type=Path, required=True)
    h1.add_argument("--output-dir", type=Path)
    h1_mode = h1.add_mutually_exclusive_group(required=True)
    for flag in ("--dry-run", "--stage", "--verify"):
        h1_mode.add_argument(flag, action="store_true")


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

    pin = _add_sub(
        subparsers,
        "data-pin-raw",
        "pin a raw .json.zst response against TTL collection",
        handler,
    )
    _add_root(pin)
    _add_lake_root(pin)
    pin.add_argument("--entry-id", required=True)
    _add_format(pin)

    gc = _add_sub(
        subparsers,
        "data-gc",
        "plan or collect expired unpinned raw .json.zst responses",
        handler,
    )
    _add_root(gc)
    _add_lake_root(gc)
    gc_mode = gc.add_mutually_exclusive_group()
    gc_mode.add_argument(
        "--dry-run", action="store_true", help="report files only; this is the default"
    )
    gc_mode.add_argument(
        "--apply", action="store_true", help="delete only expired, unpinned responses and orphans"
    )
    _add_format(gc)


def register_iss_and_benchmark_parsers(subparsers: Any, handler: Handler) -> None:
    iss = _add_sub(subparsers, "iss-intake", "read a current MOEX ISS futures catalog", handler)
    iss.add_argument("--output", type=Path, required=True)
    iss.add_argument("--base-url", default=DEFAULT_ISS_BASE_URL)
    iss.add_argument("--board", default="RFUD")
    iss.add_argument("--near", default=DEFAULT_NEAR)
    iss.add_argument("--far", default=DEFAULT_FAR)

    cov = _add_sub(
        subparsers,
        "iss-timeframe-inventory",
        "build an instrument candle-timeframe inventory",
        handler,
    )
    cov.add_argument("--output", type=Path, required=True)
    cov.add_argument("--csv-output", type=Path)
    cov.add_argument("--base-url", default=DEFAULT_ISS_BASE_URL)
    cov.add_argument("--board", default="RFUD")
    cov.add_argument("--workers", type=int, default=2)
    cov.add_argument("--delay-seconds", type=float, default=0.1)
    cov.add_argument("--timeout", type=float, default=8.0)
    cov.add_argument("--cache-dir", type=Path, default=Path("data/w1/candleborders-cache"))

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

    pair = _add_sub(
        subparsers,
        "iss-candle-pair-check",
        "check synchronization and OHLC quality for two candle series",
        handler,
    )
    pair.add_argument("--near", type=Path, required=True)
    pair.add_argument("--far", type=Path, required=True)
    pair.add_argument("--output", type=Path, required=True)

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
