"""CLI commands owned by the strategies package."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

from moex_spread_lab.core.w0 import W0Error, write_json

from .cli_parsers import register_strategy_parsers


def register(subparsers: Any) -> None:
    register_strategy_parsers(subparsers, run)


def _load_cost_profile_arg(args: Any, get_scenario: Callable[[str], dict[str, Any]]) -> Any:
    if args.cost_scenario:
        return get_scenario(args.cost_scenario)
    if args.cost_profile:
        try:
            profile = json.loads(args.cost_profile.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise W0Error(f"cannot read cost profile {args.cost_profile}: {exc}") from exc
        if not isinstance(profile, dict):
            raise W0Error("cost profile must be a JSON object")
        return cast(dict[str, Any], profile)
    return None


def _run_analytical_baseline_cmd(args: Any) -> int:
    from moex_spread_lab.accounting import publish_baseline_artifact, write_legacy_json
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
    cost_profile = _load_cost_profile_arg(args, get_cost_scenario)
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


def _run_carry_study_cmd(args: Any) -> int:
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


def _run_carry_strategy_cmd(args: Any) -> int:
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
        strategy_variant="funding_only" if args.hypothesis == "h3-funding-only" else "basis_median",
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


def _run_pair_screen_cmd(args: Any) -> int:
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


def _run_screener_run_cmd(args: Any) -> int:
    from moex_spread_lab.strategies.pair_screener.pair_screener import (
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
            {
                "run_id": result["run_id"],
                "status": result["status"],
                "output": str(args.output),
            },
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
                    {"asset": asset, **{k: variant[k] for k in summary_keys}},
                    sort_keys=True,
                )
            )
    return 0


def _run_s3_reference_cmd(args: Any) -> int:
    from moex_spread_lab.strategies.s3_reference.engine import run_s3

    result = run_s3(args.reference_dir, args.benchmark)
    write_json(args.output, result)
    keys = (
        "strategy",
        "variant",
        "cost_model",
        "trades",
        "gross_rub",
        "net_rub",
        "excess_over_lqdt_on_margin_rub",
    )
    for variant in result["variants"]:
        print(json.dumps({key: variant[key] for key in keys}, sort_keys=True))
    return 0


def _run_funding_screen_cmd(args: Any) -> int:
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


_DISPATCH: dict[str, Callable[[Any], int]] = {
    "analytical-baseline": _run_analytical_baseline_cmd,
    "carry-study": _run_carry_study_cmd,
    "carry-strategy": _run_carry_strategy_cmd,
    "pair-screen": _run_pair_screen_cmd,
    "screener-run": _run_screener_run_cmd,
    "calendar-study": _run_calendar_study_cmd,
    "s3-reference": _run_s3_reference_cmd,
    "funding-screen": _run_funding_screen_cmd,
}


def run(args: Any) -> int:
    handler = _DISPATCH.get(args.command)
    if handler is None:
        return 2
    return handler(args)
