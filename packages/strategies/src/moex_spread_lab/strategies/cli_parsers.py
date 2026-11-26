"""Argparse subcommand registration for the strategies package CLI."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

Handler = Callable[[Any], int]


def _register_baseline_and_carry(subparsers: Any, handler: Handler) -> None:
    baseline_parser = subparsers.add_parser(
        "analytical-baseline", help="run a delayed candle-close spread baseline"
    )
    baseline_parser.add_argument("--near-candles", type=Path, required=True)
    baseline_parser.add_argument("--far-candles", type=Path, required=True)
    baseline_parser.add_argument("--benchmark", type=Path, required=True)
    baseline_parser.add_argument("--contracts", type=Path, required=True)
    baseline_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="path for the analytical-baseline.v1 JSON report",
    )
    baseline_parser.add_argument("--near", default="CNYRUBF")
    baseline_parser.add_argument("--far", default="CRZ6")
    baseline_parser.add_argument("--initial-capital", type=float, default=30000.0)
    baseline_parser.add_argument("--lookback", type=int, default=20)
    baseline_parser.add_argument("--entry-z", type=float, default=2.0)
    baseline_parser.add_argument("--exit-z", type=float, default=0.5)
    baseline_parser.add_argument("--quantity", type=int, default=1)
    baseline_parser.add_argument("--interval", type=int, default=24, choices=(1, 10, 60, 24))
    baseline_parser.add_argument("--decision-delay-bars", type=int, default=1)
    baseline_parser.add_argument(
        "--analysis-role",
        choices=("primary_baseline", "diagnostic_only"),
        default="primary_baseline",
    )
    cost_group = baseline_parser.add_mutually_exclusive_group()
    cost_group.add_argument("--cost-scenario", choices=("low", "base", "stress"))
    cost_group.add_argument("--cost-profile", type=Path)
    baseline_parser.set_defaults(_msl_cli_handler=handler)

    carry_parser = subparsers.add_parser(
        "carry-study", help="build a funding-aware EOD carry decomposition"
    )
    carry_parser.add_argument("--near", default="CNYRUBF")
    carry_parser.add_argument("--far", default="CRZ6")
    carry_parser.add_argument("--from-date", required=True)
    carry_parser.add_argument("--till-date", required=True)
    carry_parser.add_argument("--contracts", type=Path, required=True)
    carry_parser.add_argument("--output", type=Path, required=True)
    carry_parser.add_argument("--cache-dir", type=Path, default=Path("data/carry-history-cache"))
    carry_parser.add_argument("--delay-seconds", type=float, default=0.1)
    carry_parser.add_argument("--timeout", type=float, default=30.0)
    carry_parser.set_defaults(_msl_cli_handler=handler)

    carry_strategy_parser = subparsers.add_parser(
        "carry-strategy", help="run the frozen funding-aware carry proxy strategy"
    )
    carry_strategy_parser.add_argument("--carry-study", type=Path, required=True)
    carry_strategy_parser.add_argument("--benchmark", type=Path, required=True)
    carry_strategy_parser.add_argument("--contracts", type=Path, required=True)
    carry_strategy_parser.add_argument("--output", type=Path, required=True)
    carry_strategy_parser.add_argument(
        "--cost-scenario",
        choices=(
            "low",
            "base",
            "stress",
            "moex_current_taker",
            "vtb_online_prime_current",
            "tariff",
        ),
        default="base",
    )
    carry_strategy_parser.add_argument("--initial-capital", type=float, default=30000.0)
    carry_strategy_parser.add_argument(
        "--hypothesis", choices=("h2-s1", "h3-funding-only"), default="h2-s1"
    )
    carry_strategy_parser.set_defaults(_msl_cli_handler=handler)


def _register_screens_and_studies(subparsers: Any, handler: Handler) -> None:
    screen_parser = subparsers.add_parser(
        "pair-screen",
        help="screen documented futures pairs with the fixed H1 control and account stress gate",
    )
    screen_parser.add_argument("--candles-dir", type=Path, required=True)
    screen_parser.add_argument("--benchmark", type=Path, required=True)
    screen_parser.add_argument("--contracts", type=Path, required=True)
    screen_parser.add_argument("--output", type=Path, required=True)
    screen_parser.add_argument("--initial-capital", type=float, default=30000.0)
    screen_parser.set_defaults(_msl_cli_handler=handler)

    funding_screen_parser = subparsers.add_parser(
        "funding-screen",
        help="screen flagship perpetual-to-quarterly pairs with the frozen funding-only hypothesis",
    )
    funding_screen_parser.add_argument("--from-date", required=True)
    funding_screen_parser.add_argument("--till-date", required=True)
    funding_screen_parser.add_argument("--contracts", type=Path, required=True)
    funding_screen_parser.add_argument("--benchmark", type=Path, required=True)
    funding_screen_parser.add_argument(
        "--cache-dir", type=Path, default=Path("data/carry-screen-history-cache")
    )
    funding_screen_parser.add_argument("--output", type=Path, required=True)
    funding_screen_parser.add_argument("--initial-capital", type=float, default=30000.0)
    funding_screen_parser.add_argument(
        "--cost-scenario", choices=("low", "base", "stress", "tariff"), default="stress"
    )
    funding_screen_parser.add_argument("--delay-seconds", type=float, default=0.2)
    funding_screen_parser.add_argument("--timeout", type=float, default=60.0)
    funding_screen_parser.set_defaults(_msl_cli_handler=handler)

    s3_parser = subparsers.add_parser(
        "s3-reference",
        help="reproduce the reference SBRF calendar backtest and stress its fills and liquidity",
    )
    s3_parser.add_argument(
        "--reference-dir",
        type=Path,
        required=True,
        help="checkout of the reference calendar-spread project",
    )
    s3_parser.add_argument(
        "--benchmark", type=Path, default=Path("data/benchmark/lqdt-lineage.json")
    )
    s3_parser.add_argument("--output", type=Path, required=True)
    s3_parser.set_defaults(_msl_cli_handler=handler)

    calendar_parser = subparsers.add_parser(
        "calendar-study",
        help="study native MOEX calendar spreads over front windows (fetches and caches ISS history)",
    )
    calendar_parser.add_argument("--root", type=Path, default=Path.cwd())
    calendar_parser.add_argument("--till-date", required=True)
    calendar_parser.add_argument("--slippage-ticks", type=float, default=1.0)
    calendar_parser.add_argument(
        "--assets",
        nargs="+",
        default=None,
        help="asset prefixes; default is every registered asset",
    )
    calendar_parser.add_argument("--output", type=Path, required=True)
    calendar_parser.set_defaults(_msl_cli_handler=handler)

    screener_run_parser = subparsers.add_parser(
        "screener-run", help="replay one local screener configuration as JSON"
    )
    screener_run_parser.add_argument("--root", type=Path, default=Path.cwd())
    screener_run_parser.add_argument("--pair", required=True)
    screener_run_parser.add_argument(
        "--strategy", choices=("mean_reversion", "funding_only"), required=True
    )
    screener_run_parser.add_argument("--slippage-ticks", type=float, default=1.0)
    screener_run_parser.add_argument("--quantity", type=int, default=1)
    screener_run_parser.add_argument("--lookback", type=int, default=20)
    screener_run_parser.add_argument("--entry-z", type=float, default=2.0)
    screener_run_parser.add_argument("--exit-z", type=float, default=0.5)
    screener_run_parser.add_argument("--output", type=Path, required=True)
    screener_run_parser.set_defaults(_msl_cli_handler=handler)


def register_strategy_parsers(subparsers: Any, handler: Handler) -> None:
    _register_baseline_and_carry(subparsers, handler)
    _register_screens_and_studies(subparsers, handler)
