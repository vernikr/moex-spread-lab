"""CLI commands owned by the accounting package."""

from __future__ import annotations

import json
from pathlib import Path

from moex_spread_lab.core.w0 import W0Error, write_json
from moex_spread_lab.accounting.research import run_research_pipeline


def register(subparsers):
    artifact_import = subparsers.add_parser("run-artifact-import", help="verify and import legacy analytical-baseline.v1 JSON without deleting it")
    artifact_import.add_argument("--legacy", type=Path, required=True)
    artifact_import.add_argument("--output-dir", type=Path, required=True)
    artifact_import.add_argument("--expected-sha256", help="required source hash when restoring from an archive manifest")
    artifact_import.add_argument("--retention-days", type=int, default=30)
    artifact_import.add_argument("--pin", action="store_true", help="explicitly retain important evidence details")
    artifact_import.set_defaults(_msl_cli_handler=run)

    artifact_pin_parser = subparsers.add_parser("run-artifact-pin", help="pin or unpin a compact evidence run's Parquet details")
    artifact_pin_parser.add_argument("--artifact-dir", type=Path, required=True)
    artifact_pin_group = artifact_pin_parser.add_mutually_exclusive_group()
    artifact_pin_group.add_argument("--pin", action="store_true", help="keep details without TTL; this is the default")
    artifact_pin_group.add_argument("--unpin", action="store_true", help="restart the artifact's configured detail TTL")
    artifact_pin_parser.add_argument("--format", choices=("summary", "json"), default="summary")
    artifact_pin_parser.set_defaults(_msl_cli_handler=run)

    artifact_gc_parser = subparsers.add_parser("run-artifact-gc", help="plan or collect expired unpinned Parquet run details")
    artifact_gc_parser.add_argument("--root", type=Path, default=Path.cwd())
    artifact_gc_parser.add_argument("--runs-root", type=Path, default=Path("data/analytics/runs"))
    artifact_gc_mode = artifact_gc_parser.add_mutually_exclusive_group()
    artifact_gc_mode.add_argument("--dry-run", action="store_true", help="report detail files only; this is the default")
    artifact_gc_mode.add_argument("--apply", action="store_true", help="remove expired detail files while preserving summaries and manifests")
    artifact_gc_parser.add_argument("--format", choices=("summary", "json"), default="summary")
    artifact_gc_parser.set_defaults(_msl_cli_handler=run)

    pipeline_parser = subparsers.add_parser("research-pipeline", help="run offline W1-W7 research waves")
    pipeline_parser.add_argument("--manifest", type=Path, required=True)
    pipeline_parser.add_argument("--fixture", type=Path, required=True)
    pipeline_parser.add_argument("--output", type=Path, required=True)
    pipeline_parser.set_defaults(_msl_cli_handler=run)

    ledger_parser = subparsers.add_parser("execution-ledger", help="build a rebuildable analytical execution and portfolio ledger")
    ledger_parser.add_argument("--baseline", type=Path, required=True)
    ledger_parser.add_argument("--output", type=Path, required=True)
    ledger_parser.set_defaults(_msl_cli_handler=run)

    validation_parser = subparsers.add_parser("validate-analytics", help="reproduce the historical W7 protocol (not the interactive screener split)")
    validation_parser.add_argument("--baseline", type=Path, required=True)
    validation_parser.add_argument("--ledger", type=Path, required=True)
    validation_parser.add_argument("--sensitivity", type=Path, action="append", default=[])
    validation_parser.add_argument("--output", type=Path, required=True)
    validation_parser.add_argument("--train-fraction", type=float, default=0.6)
    validation_parser.add_argument("--confirmation-fraction", type=float, default=0.2)
    validation_parser.set_defaults(_msl_cli_handler=run)

    account_risk_parser = subparsers.add_parser("account-risk-check", help="run the working VTB EBS/KPUR margin stress screen")
    account_risk_parser.add_argument("--near", default="CNYRUBF")
    account_risk_parser.add_argument("--far", default="CRZ6")
    account_risk_parser.add_argument("--contracts", type=Path, required=True)
    account_risk_parser.add_argument("--carry-study", type=Path)
    account_risk_parser.add_argument("--output", type=Path, required=True)
    account_risk_parser.add_argument("--initial-capital", type=float, default=30000.0)
    account_risk_parser.add_argument("--stress-vm-buffer", type=float, default=5000.0)
    account_risk_parser.add_argument("--uncertainty-buffer", type=float, default=5000.0)
    account_risk_parser.add_argument("--key-rate", type=float, default=0.14)
    account_risk_parser.set_defaults(_msl_cli_handler=run)



def run(args):
    if args.command == "run-artifact-import":
        from moex_spread_lab.accounting.run_artifact_import import import_baseline_artifact

        try:
            result = import_baseline_artifact(
                args.legacy, args.output_dir, expected_sha256=args.expected_sha256,
                retention_days=args.retention_days, pin=args.pin,
            )
        except OSError as exc:
            raise W0Error(f"legacy import failed: {exc}") from exc
        print(json.dumps(result, sort_keys=True))
        return 0
    if args.command == "run-artifact-pin":
        from moex_spread_lab.accounting.run_artifacts import pin_run_artifact

        manifest = pin_run_artifact(args.artifact_dir, pinned=not args.unpin)
        output = {"status": "pinned" if not args.unpin else "unpinned", "manifest": manifest}
        if args.format == "json":
            print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps({
                "status": output["status"],
                "artifact_id": manifest["artifact_id"],
                "pinned": manifest["retention"]["pinned"],
                "expires_at": manifest["retention"]["expires_at"],
            }, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "run-artifact-gc":
        from moex_spread_lab.accounting.run_artifacts import gc_run_artifacts

        root = args.root.resolve()
        runs_root = args.runs_root if args.runs_root.is_absolute() else root / args.runs_root
        output = gc_run_artifacts(runs_root, dry_run=not args.apply)
        if args.format == "json":
            print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps({
                key: output[key]
                for key in ("status", "expired_artifact_count", "retained_artifact_count")
            }, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "research-pipeline":
        result = run_research_pipeline(args.manifest, args.fixture)
        write_json(args.output, result)
        print(json.dumps({
            "all_fixture_gates_passed": result["all_fixture_gates_passed"],
            "release_state": result["release_state"],
            "report_hash": result["report_hash"],
        }, sort_keys=True))
        return 0
    if args.command == "execution-ledger":
        from moex_spread_lab.accounting.ledger import build_ledger

        result = build_ledger(args.baseline)
        write_json(args.output, result)
        print(json.dumps({
            "release_state": result["release_state"],
            "timeframe": result["timeframe"],
            "execution_attempt_count": result["summary"]["execution_attempt_count"],
            "final_X": result["summary"]["final_X"],
            "executable_value_available": result["summary"]["executable_value_available"],
            "output": str(args.output),
        }, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "validate-analytics":
        from moex_spread_lab.accounting.validation import build_validation

        result = build_validation(
            args.baseline,
            args.ledger,
            args.sensitivity,
            train_fraction=args.train_fraction,
            confirmation_fraction=args.confirmation_fraction,
        )
        write_json(args.output, result)
        print(json.dumps({
            "release_state": result["release_state"],
            "verdict": result["verdict"],
            "timeframe": result["timeframe"],
            "sealed_confirmation_rows": result["splits"]["sealed_confirmation"]["row_count"],
            "final_X": result["splits"]["sealed_confirmation"]["final_X"],
            "blocker_count": len(result["conclusion_blockers"]),
            "output": str(args.output),
        }, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "account-risk-check":
        from moex_spread_lab.accounting.account_risk import assess_vtb_pair

        observed_daily_pnl = None
        if args.carry_study:
            try:
                carry_study = json.loads(args.carry_study.read_text(encoding="utf-8"))
                observed_daily_pnl = [
                    float(row["daily_gross_carry_pnl"])
                    for row in carry_study.get("rows", [])[1:]
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
        print(json.dumps({
            "schema_version": result["schema_version"],
            "current_snapshot_passed": result["summary"]["current_snapshot_passed"],
            "all_working_scenarios_passed": result["summary"]["all_scenarios_passed"],
            "pair_margin_no_offset_rub": result["pair"]["pair_margin_no_offset_rub"],
            "most_severe_remaining_capital_rub": result["summary"]["most_severe_remaining_capital_rub"],
            "output": str(args.output),
        }, ensure_ascii=False, sort_keys=True))
        return 0
    return 2
