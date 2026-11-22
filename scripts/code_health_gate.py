#!/usr/bin/env python3
"""Enforce repository-local LOC, cognitive-complexity, C, parity, and exception ratchets."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

if __package__:
    from .code_health_gate_common import (
        BASELINE_PATH,
        POLICY,
        ROOT,
        GateError,
        _load_baseline,
        _relative,
        _source_files,
    )
    from .code_health_gate_rules import (
        _c_function_failures,
        _check_files,
        _complexity_failures,
        _exception_failures,
        _fastcore_parity_failures,
        _updated_baseline,
    )
else:
    from code_health_gate_common import (
        BASELINE_PATH,
        POLICY,
        ROOT,
        GateError,
        _load_baseline,
        _relative,
        _source_files,
    )
    from code_health_gate_rules import (
        _c_function_failures,
        _check_files,
        _complexity_failures,
        _exception_failures,
        _fastcore_parity_failures,
        _updated_baseline,
    )


def _run_complexipy() -> dict[str, int]:
    executable = shutil.which("complexipy")
    if executable is None:
        raise GateError("complexipy is not installed; run `uv sync --frozen`")
    with tempfile.TemporaryDirectory(prefix="msl-complexipy-") as directory:
        report = Path(directory) / "complexity.json"
        result = subprocess.run(
            [executable, "--ignore-complexity", "--output-format", "json", "--output", str(report)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            details = (result.stderr or result.stdout).strip()[-1500:]
            raise GateError(f"complexipy failed ({result.returncode}): {details}")
        try:
            findings = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GateError(f"cannot read complexipy JSON output: {exc}") from exc
    if not isinstance(findings, list):
        raise GateError("complexipy JSON output must be a list")
    scores = {}
    for finding in findings:
        if not isinstance(finding, dict):
            raise GateError("complexipy emitted a malformed finding")
        path = _relative(ROOT / str(finding["path"]))
        name = str(finding.get("function_name") or "<module>")
        key = f"{path}::{name}"
        scores[key] = max(scores.get(key, 0), int(finding["complexity"]))
    return scores


def _run_lizard() -> dict[str, dict[str, int]]:
    executable = shutil.which("lizard")
    if executable is None:
        raise GateError("lizard is not installed; run `uv sync --frozen`")
    with tempfile.TemporaryDirectory(prefix="msl-lizard-") as directory:
        report = Path(directory) / "c-functions.csv"
        result = subprocess.run(
            [
                executable,
                "-l",
                "c",
                "-C",
                str(POLICY["max_c_function_ccn"]),
                "-L",
                str(POLICY["max_c_function_nloc"]),
                "-a",
                str(POLICY["max_c_function_parameters"]),
                "-i",
                "-1",
                "--csv",
                "-o",
                str(report),
                "packages",
                "src",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            details = (result.stderr or result.stdout).strip()[-1500:]
            raise GateError(f"lizard failed ({result.returncode}): {details}")
        try:
            with report.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            raise GateError(f"cannot read lizard CSV output: {exc}") from exc
    metrics = {}
    for row in rows:
        if len(row) < 11:
            continue
        key = f"{_relative(ROOT / row[6])}::{row[7]}"
        values = {"nloc": int(row[0]), "ccn": int(row[1]), "parameters": int(row[3])}
        if key not in metrics:
            metrics[key] = values
        else:
            for name, value in values.items():
                metrics[key][name] = max(metrics[key][name], value)
    return metrics


def _write_baseline(value: dict[str, Any]) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="lock in reductions only; never accept new growth or new offenders",
    )
    args = parser.parse_args(argv)
    try:
        baseline = _load_baseline()
        failures, file_metrics = _check_files(_source_files(), baseline)
        complexity = _run_complexipy()
        c_functions = _run_lizard()
        failures.extend(_complexity_failures(complexity, baseline))
        failures.extend(_c_function_failures(c_functions, baseline))
        failures.extend(_fastcore_parity_failures())
        failures.extend(_exception_failures())
    except (GateError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"code-health-gate: failed: {exc}", file=sys.stderr)
        return 2
    if failures:
        print("code-health-gate: failed")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    if args.update_baseline:
        _write_baseline(_updated_baseline(baseline, file_metrics, complexity, c_functions))
        print(
            f"code-health-gate: baseline reductions recorded in {BASELINE_PATH.relative_to(ROOT)}"
        )
    else:
        print("code-health-gate: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
