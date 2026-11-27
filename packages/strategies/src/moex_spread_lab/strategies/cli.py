"""CLI commands owned by the strategies package."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from moex_spread_lab.core.w0 import write_json


def _register_calendar_study(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "calendar-study",
        help="study native MOEX calendar spreads over front windows (fetches and caches ISS history)",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--till-date", required=True)
    parser.add_argument("--slippage-ticks", type=float, default=1.0)
    parser.add_argument(
        "--assets",
        nargs="+",
        default=None,
        help="asset prefixes; default is every registered asset",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.set_defaults(_msl_cli_handler=run)


def _register_screener_run(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "screener-run", help="replay one local screener configuration as JSON"
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--pair", required=True)
    parser.add_argument("--strategy", choices=("mean_reversion", "funding_only"), required=True)
    parser.add_argument("--slippage-ticks", type=float, default=1.0)
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument("--lookback", type=int, default=20)
    parser.add_argument("--entry-z", type=float, default=2.0)
    parser.add_argument("--exit-z", type=float, default=0.5)
    parser.add_argument("--output", type=Path, required=True)
    parser.set_defaults(_msl_cli_handler=run)


def register(subparsers: Any) -> None:
    _register_calendar_study(subparsers)
    _register_screener_run(subparsers)


def _run_screener_run_cmd(args: Any) -> int:
    from moex_spread_lab.strategies.pair_screener import (
        ScreenerInputs,
        build_result,
    )

    parameters: dict[str, Any] = {"lookback": args.lookback}
    if args.strategy == "mean_reversion":
        parameters.update({"entry_z": args.entry_z, "exit_z": args.exit_z})
    result = build_result(
        ScreenerInputs(args.root.resolve()),
        {
            "pair": args.pair,
            "strategy": args.strategy,
            "slippage_ticks": args.slippage_ticks,
            "quantity": args.quantity,
            "parameters": parameters,
        },
    )
    write_json(args.output, result)
    print(
        json.dumps(
            {"run_id": result["run_id"], "status": result["status"], "output": str(args.output)},
            sort_keys=True,
        )
    )
    return 0


def _run_calendar_study_cmd(args: Any) -> int:
    from moex_spread_lab.strategies.calendar.calendar_spreads import ASSETS, run_calendar_study

    root = args.root.resolve()
    result = run_calendar_study(
        root / "data/spread-history-cache",
        root / "data/benchmark/lqdt-lineage.json",
        root / "data/benchmark-cache",
        root / "data/w1/current-intake.json",
        assets=tuple(args.assets or ASSETS),
        slippage_ticks=args.slippage_ticks,
        till_date=args.till_date,
    )
    write_json(args.output, result)
    summary_keys = (
        "strategy",
        "variant",
        "trades",
        "windows_with_trades",
        "windows_net_positive",
        "gross_rub",
        "costs_rub",
        "net_rub",
        "excess_over_lqdt_on_margin_rub",
        "excess_pp_per_year_on_allocation",
    )
    for asset, item in result["assets"].items():
        for variant in item.get("variants", []):
            print(
                json.dumps(
                    {"asset": asset, **{k: variant[k] for k in summary_keys}}, sort_keys=True
                )
            )
    return 0


_DISPATCH: dict[str, Callable[[Any], int]] = {
    "calendar-study": _run_calendar_study_cmd,
    "screener-run": _run_screener_run_cmd,
}


def run(args: Any) -> int:
    handler = _DISPATCH.get(args.command)
    if handler is None:
        return 2
    return handler(args)
