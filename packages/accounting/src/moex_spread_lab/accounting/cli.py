"""CLI commands owned by the accounting package."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from moex_spread_lab.core.w0 import W0Error, write_json


def register(subparsers: Any) -> None:
    ledger_parser = subparsers.add_parser(
        "execution-ledger", help="build a rebuildable analytical execution and portfolio ledger"
    )
    ledger_parser.add_argument("--baseline", type=Path, required=True)
    ledger_parser.add_argument("--output", type=Path, required=True)
    ledger_parser.set_defaults(_msl_cli_handler=_run_execution_ledger_cmd)

    account_risk_parser = subparsers.add_parser(
        "account-risk-check", help="run the working VTB EBS/KPUR margin stress screen"
    )
    account_risk_parser.add_argument("--near", default="CNYRUBF")
    account_risk_parser.add_argument("--far", default="CRZ6")
    account_risk_parser.add_argument("--contracts", type=Path, required=True)
    account_risk_parser.add_argument("--carry-study", type=Path)
    account_risk_parser.add_argument("--output", type=Path, required=True)
    account_risk_parser.add_argument("--initial-capital", type=float, default=30000.0)
    account_risk_parser.add_argument("--stress-vm-buffer", type=float, default=5000.0)
    account_risk_parser.add_argument("--uncertainty-buffer", type=float, default=5000.0)
    account_risk_parser.add_argument("--key-rate", type=float, default=0.14)
    account_risk_parser.set_defaults(_msl_cli_handler=_run_account_risk_cmd)


def _run_execution_ledger_cmd(args: Any) -> int:
    from moex_spread_lab.accounting.ledger import build_ledger

    result = build_ledger(args.baseline)
    write_json(args.output, result)
    print(
        json.dumps(
            {
                "release_state": result["release_state"],
                "timeframe": result["timeframe"],
                "execution_attempt_count": result["summary"]["execution_attempt_count"],
                "final_X": result["summary"]["final_X"],
                "executable_value_available": result["summary"]["executable_value_available"],
                "output": str(args.output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _run_account_risk_cmd(args: Any) -> int:
    from moex_spread_lab.accounting.account_risk import assess_vtb_pair

    observed_daily_pnl: list[float] | None = None
    if args.carry_study:
        try:
            carry_study = json.loads(args.carry_study.read_text(encoding="utf-8"))
            observed_daily_pnl = [
                float(row["daily_gross_carry_pnl"]) for row in carry_study.get("rows", [])[1:]
            ]
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise W0Error(f"cannot read carry study for risk screen: {exc}") from exc
    result = assess_vtb_pair(
        args.contracts,
        args.near,
        args.far,
        initial_capital=args.initial_capital,
        stress_vm_buffer=args.stress_vm_buffer,
        uncertainty_buffer=args.uncertainty_buffer,
        key_rate=args.key_rate,
        observed_daily_pnl=observed_daily_pnl,
    )
    write_json(args.output, result)
    print(
        json.dumps(
            {
                "schema_version": result["schema_version"],
                "current_snapshot_passed": result["summary"]["current_snapshot_passed"],
                "all_working_scenarios_passed": result["summary"]["all_scenarios_passed"],
                "pair_margin_no_offset_rub": result["pair"]["pair_margin_no_offset_rub"],
                "most_severe_remaining_capital_rub": result["summary"][
                    "most_severe_remaining_capital_rub"
                ],
                "output": str(args.output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0
