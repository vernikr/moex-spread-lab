"""CLI commands owned by the strategies package."""

from __future__ import annotations

import json
from pathlib import Path

from moex_spread_lab.core.w0 import W0Error, write_json


def register(subparsers):
    baseline_parser = subparsers.add_parser(
        "analytical-baseline", help="run a delayed candle-close spread baseline"
    )
    baseline_parser.add_argument("--near-candles", type=Path, required=True)
    baseline_parser.add_argument("--far-candles", type=Path, required=True)
    baseline_parser.add_argument("--benchmark", type=Path, required=True)
    baseline_parser.add_argument("--contracts", type=Path, required=True)
    baseline_parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="new directory for summary.json and Parquet details",
    )
    baseline_parser.add_argument(
        "--legacy-json",
        type=Path,
        help="explicit compatibility export of the full pretty JSON report",
    )
    baseline_parser.add_argument(
        "--retention-days",
        type=int,
        default=30,
        help="retain unpinned Parquet details for this many days",
    )
    baseline_parser.add_argument(
        "--pin", action="store_true", help="retain this evidence run's Parquet details without TTL"
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
    baseline_parser.set_defaults(_msl_cli_handler=run)

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
    carry_parser.set_defaults(_msl_cli_handler=run)

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
    carry_strategy_parser.set_defaults(_msl_cli_handler=run)

    screen_parser = subparsers.add_parser(
        "pair-screen",
        help="screen documented futures pairs with the fixed H1 control and account stress gate",
    )
    screen_parser.add_argument("--candles-dir", type=Path, required=True)
    screen_parser.add_argument("--benchmark", type=Path, required=True)
    screen_parser.add_argument("--contracts", type=Path, required=True)
    screen_parser.add_argument("--output", type=Path, required=True)
    screen_parser.add_argument("--initial-capital", type=float, default=30000.0)
    screen_parser.set_defaults(_msl_cli_handler=run)

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
    funding_screen_parser.set_defaults(_msl_cli_handler=run)

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
    s3_parser.set_defaults(_msl_cli_handler=run)

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
    calendar_parser.set_defaults(_msl_cli_handler=run)

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
    # Third-party command providers add subcommands without modifying this file.
    # Existing commands remain here until their package-local migration is complete.
    screener_run_parser.set_defaults(_msl_cli_handler=run)


def run(args):
    if args.command == "analytical-baseline":
        from moex_spread_lab.strategies.pair_mean_reversion.analytics import (
            build_series,
            get_cost_scenario,
            run_mean_reversion_baseline,
        )

        series = build_series(
            args.near_candles,
            args.far_candles,
            args.benchmark,
            args.contracts,
            near=args.near,
            far=args.far,
            interval=args.interval,
        )
        cost_profile = None
        if args.cost_scenario:
            cost_profile = get_cost_scenario(args.cost_scenario)
        elif args.cost_profile:
            try:
                cost_profile = json.loads(args.cost_profile.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise W0Error(f"cannot read cost profile {args.cost_profile}: {exc}") from exc
            if not isinstance(cost_profile, dict):
                raise W0Error("cost profile must be a JSON object")
        result = run_mean_reversion_baseline(
            series,
            initial_capital=args.initial_capital,
            lookback=args.lookback,
            entry_z=args.entry_z,
            exit_z=args.exit_z,
            quantity=args.quantity,
            decision_delay_bars=args.decision_delay_bars,
            analysis_role=args.analysis_role,
            cost_profile=cost_profile,
        )
        from moex_spread_lab.accounting.run_artifacts import (
            publish_baseline_artifact,
            write_legacy_json,
        )

        artifact = publish_baseline_artifact(
            args.output_dir,
            result,
            retention_days=args.retention_days,
            pin=args.pin,
        )
        if args.legacy_json is not None:
            write_legacy_json(args.legacy_json, result)
        print(
            json.dumps(
                {
                    "release_state": result["release_state"],
                    "timeframe": result["timeframe"],
                    "analysis_role": result["analysis_role"],
                    "first_date": result["summary"]["first_date"],
                    "last_date": result["summary"]["last_date"],
                    "common_row_count": result["summary"]["common_row_count"],
                    "trade_count": result["summary"]["trade_count"],
                    "gross_pnl": result["summary"]["gross_pnl"],
                    "final_X": result["summary"]["final_X"],
                    "max_drawdown_D": result["summary"]["max_drawdown_D"],
                    "cost_status": result["costs"]["status"],
                    "artifact_dir": artifact["artifact_dir"],
                    "artifact_id": artifact["artifact_id"],
                    "legacy_json": str(args.legacy_json) if args.legacy_json else None,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "carry-study":
        from moex_spread_lab.strategies.funding_carry.carry import build_carry_study, fetch_history

        near_source, far_source = (
            fetch_history(
                secid,
                args.from_date,
                args.till_date,
                cache_dir=args.cache_dir,
                delay_seconds=args.delay_seconds,
                timeout=args.timeout,
            )
            for secid in (args.near, args.far)
        )
        result = build_carry_study(
            near_source,
            far_source,
            args.contracts,
            near=args.near,
            far=args.far,
            funding_regimes=None if args.near == "CNYRUBF" else (),
        )
        write_json(args.output, result)
        print(
            json.dumps(
                {
                    "release_state": result["release_state"],
                    "study_role": result["study_role"],
                    "near": result["near"],
                    "far": result["far"],
                    "common_row_count": result["summary"]["common_row_count"],
                    "total_basis_pnl": result["summary"]["total_basis_pnl"],
                    "total_funding_received": result["summary"]["total_funding_received"],
                    "total_gross_carry_pnl": result["summary"]["total_gross_carry_pnl"],
                    "output": str(args.output),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "carry-strategy":
        from moex_spread_lab.strategies.funding_carry.carry import build_carry_strategy

        try:
            carry_study = json.loads(args.carry_study.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise W0Error(f"cannot read carry study {args.carry_study}: {exc}") from exc
        result = build_carry_strategy(
            carry_study,
            args.benchmark,
            args.contracts,
            transaction_cost_scenario=args.cost_scenario,
            initial_capital=args.initial_capital,
            strategy_variant="funding_only"
            if args.hypothesis == "h3-funding-only"
            else "basis_median",
        )
        write_json(args.output, result)
        print(
            json.dumps(
                {
                    "rule_id": result["rule_id"],
                    "release_state": result["release_state"],
                    "closed_trade_count": result["summary"]["closed_trade_count"],
                    "open_trade_count": result["summary"]["open_trade_count"],
                    "final_X": result["summary"]["final_X"],
                    "gross_pnl": result["summary"]["gross_pnl"],
                    "transaction_costs": result["summary"]["transaction_costs"],
                    "output": str(args.output),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "pair-screen":
        from moex_spread_lab.strategies.pair_screener.screener import screen_pairs

        result = screen_pairs(
            args.candles_dir,
            args.benchmark,
            args.contracts,
            initial_capital=args.initial_capital,
        )
        write_json(args.output, result)
        print(
            json.dumps(
                {
                    "schema_version": result["schema_version"],
                    "candidate_count": result["summary"]["candidate_count"],
                    "stress_pass_count": result["summary"]["stress_pass_count"],
                    "positive_h1_control_count": result["summary"]["positive_h1_control_count"],
                    "output": str(args.output),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "screener-run":
        from moex_spread_lab.strategies.pair_screener.pair_screener import (
            ScreenerInputs,
            build_result,
        )

        parameters = {"lookback": args.lookback}
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
                {
                    "run_id": result["run_id"],
                    "status": result["status"],
                    "output": str(args.output),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "calendar-study":
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
        for asset, item in result["assets"].items():
            for variant in item.get("variants", []):
                print(
                    json.dumps(
                        {
                            "asset": asset,
                            **{
                                k: variant[k]
                                for k in (
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
                            },
                        },
                        sort_keys=True,
                    )
                )
        return 0
    if args.command == "s3-reference":
        from moex_spread_lab.strategies.s3_reference.engine import run_s3

        result = run_s3(args.reference_dir, args.benchmark)
        write_json(args.output, result)
        for variant in result["variants"]:
            print(
                json.dumps(
                    {
                        key: variant[key]
                        for key in (
                            "strategy",
                            "variant",
                            "cost_model",
                            "trades",
                            "gross_rub",
                            "net_rub",
                            "excess_over_lqdt_on_margin_rub",
                        )
                    },
                    sort_keys=True,
                )
            )
        return 0
    if args.command == "funding-screen":
        from moex_spread_lab.strategies.funding_carry.funding_screen import screen_funding_pairs

        result = screen_funding_pairs(
            contracts_path=args.contracts,
            benchmark_path=args.benchmark,
            cache_dir=args.cache_dir,
            from_date=args.from_date,
            till_date=args.till_date,
            initial_capital=args.initial_capital,
            cost_scenario=args.cost_scenario,
            delay_seconds=args.delay_seconds,
            timeout=args.timeout,
        )
        write_json(args.output, result)
        print(
            json.dumps(
                {
                    "schema_version": result["schema_version"],
                    "candidate_count": result["summary"]["candidate_count"],
                    "stress_pass_count": result["summary"]["stress_pass_count"],
                    "positive_final_X_count": result["summary"]["positive_final_X_count"],
                    "output": str(args.output),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    return 2
