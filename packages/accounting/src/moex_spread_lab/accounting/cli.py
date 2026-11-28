"""CLI commands owned by the accounting package."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from moex_spread_lab.core.w0 import W0Error, write_json

from .account_risk import (
    DEFAULT_INITIAL_CAPITAL,
    DEFAULT_KEY_RATE,
    DEFAULT_STRESS_VM_BUFFER,
    DEFAULT_UNCERTAINTY_BUFFER,
)


def register(subparsers: Any) -> None:
    account_risk_parser = subparsers.add_parser(
        "account-risk-check", help="run the working VTB EBS/KPUR margin stress screen"
    )
    account_risk_parser.add_argument("--near", default="CNYRUBF")
    account_risk_parser.add_argument("--far", default="CRZ6")
    account_risk_parser.add_argument("--contracts", type=Path, required=True)
    account_risk_parser.add_argument("--carry-study", type=Path)
    account_risk_parser.add_argument("--output", type=Path, required=True)
    account_risk_parser.add_argument(
        "--initial-capital", type=float, default=DEFAULT_INITIAL_CAPITAL
    )
    account_risk_parser.add_argument(
        "--stress-vm-buffer", type=float, default=DEFAULT_STRESS_VM_BUFFER
    )
    account_risk_parser.add_argument(
        "--uncertainty-buffer", type=float, default=DEFAULT_UNCERTAINTY_BUFFER
    )
    account_risk_parser.add_argument("--key-rate", type=float, default=DEFAULT_KEY_RATE)
    account_risk_parser.set_defaults(_msl_cli_handler=_run_account_risk_cmd)


def _carry_study_pnl(path: Path | None) -> list[float] | None:
    """Observed daily P&L of a carry study; a study without observations fails closed."""
    if path is None:
        return None
    try:
        document = cast(object, json.loads(path.read_text(encoding="utf-8")))
        if not isinstance(document, dict):
            raise TypeError(f"carry study must be a JSON object, got {type(document).__name__}")
        rows = cast(dict[str, Any], document)["rows"]
        if not isinstance(rows, list):
            raise TypeError("carry study rows must be a list")
        observed = [
            float(cast(dict[str, Any], row)["daily_gross_carry_pnl"])
            for row in cast(list[Any], rows)[1:]
        ]
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise W0Error(f"cannot read carry study for risk screen: {exc}") from exc
    if not observed:
        raise W0Error(
            "carry study yields no observations after the first row; "
            "the observed-loss reserve cannot be applied"
        )
    return observed


def _run_account_risk_cmd(args: Any) -> int:
    from moex_spread_lab.accounting.account_risk import assess_vtb_pair

    observed_daily_pnl = _carry_study_pnl(args.carry_study)
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
