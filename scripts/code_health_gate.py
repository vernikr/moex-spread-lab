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
    from .changed_files import changed_paths
    from .code_health_gate_common import (
        BASELINE_PATH,
        POLICY,
        PROTECTED_EXCEPTION_ROOTS,
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
    from changed_files import changed_paths
    from code_health_gate_common import (
        BASELINE_PATH,
        POLICY,
        PROTECTED_EXCEPTION_ROOTS,
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

SOURCE_SUFFIXES = (".py", ".c", ".js", ".css", ".html")
EXCLUDED_PARTS = ("vendor", "__pycache__")
SOURCE_TOP_LEVELS = ("packages", "src", "scripts")


def _touched_files(base: str) -> list[Path]:
    """Repo-relative touched paths restricted to ratcheted source suffixes."""
    try:
        touched = changed_paths(base, existing_only=True)
    except RuntimeError as exc:
        raise GateError(str(exc)) from exc
    files = []
    for rel in touched:
        parts = Path(rel).parts
        if not parts or parts[0] not in SOURCE_TOP_LEVELS:
            continue
        if set(parts) & set(EXCLUDED_PARTS):
            continue
        path = ROOT / rel
        if path.suffix.lower() in SOURCE_SUFFIXES and path.is_file():
            files.append(path)
    return files


def _complexipy_run(executable: str, paths: list[Path], directory: str) -> list[dict]:
    report = Path(directory) / "complexity.json"
    result = subprocess.run(
        [
            executable,
            "--ignore-complexity",
            "--output-format",
            "json",
            "--output",
            str(report),
            *[str(path) for path in paths],
        ],
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
    if not all(isinstance(finding, dict) for finding in findings):
        raise GateError("complexipy emitted a malformed finding")
    return findings


def _run_complexipy(paths: list[Path] | None = None) -> dict[str, int]:
    executable = shutil.which("complexipy")
    if executable is None:
        raise GateError("complexipy is not installed; run `uv sync --frozen`")
    # complexipy reports only the file basename, so group inputs by basename and
    # run same-name files in separate invocations where attribution needs it.
    runs: list[tuple[list[Path], str | None]] = []
    if paths is None:
        runs.append(([], None))
    else:
        by_name: dict[str, list[Path]] = {}
        for path in paths:
            by_name.setdefault(path.name, []).append(path)
        unique = [group[0] for group in by_name.values() if len(group) == 1]
        if unique:
            runs.append((unique, None))
        runs.extend(
            ([path], _relative(path))
            for group in by_name.values()
            if len(group) > 1
            for path in group
        )
    by_basename = {path.name: _relative(path) for path in paths or []}
    findings: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="msl-complexipy-") as directory:
        for run_paths, forced_rel in runs:
            for finding in _complexipy_run(executable, run_paths, directory):
                raw = str(finding["path"])
                rel = forced_rel or by_basename.get(Path(raw).name) or _relative(ROOT / raw)
                findings.append({**finding, "_rel": rel})
    scores: dict[str, int] = {}
    for finding in findings:
        name = str(finding.get("function_name") or "<module>")
        key = f"{finding['_rel']}::{name}"
        scores[key] = max(scores.get(key, 0), int(finding["complexity"]))
    return scores


def _lizard_command(executable: str, report: Path, paths: list[Path] | None) -> list[str]:
    command = [
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
    ]
    if paths is not None:
        command += [str(path) for path in paths]
    else:
        command += ["packages", "src"]
    return command


def _run_lizard(paths: list[Path] | None = None) -> dict[str, dict[str, int]]:
    executable = shutil.which("lizard")
    if executable is None:
        raise GateError("lizard is not installed; run `uv sync --frozen`")
    with tempfile.TemporaryDirectory(prefix="msl-lizard-") as directory:
        report = Path(directory) / "c-functions.csv"
        result = subprocess.run(
            _lizard_command(executable, report, paths),
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
    parser.add_argument(
        "--changed",
        action="store_true",
        help="only check touched files (LOC/token, complexity, broad exceptions; "
        "lizard/fastcore parity only when their inputs changed)",
    )
    parser.add_argument("--base", default="HEAD", help="base revision for --changed")
    args = parser.parse_args(argv)
    if args.changed and args.update_baseline:
        parser.error("--update-baseline requires a full scan")
    complexity: dict[str, int] = {}
    c_functions: dict[str, dict[str, int]] = {}
    try:
        baseline = _load_baseline()
        if args.changed:
            failures, file_metrics, skipped = _changed_failures(args.base, baseline)
        else:
            skipped = []
            failures, file_metrics = _check_files(_source_files(), baseline)
            complexity = _run_complexipy()
            c_functions = _run_lizard()
            failures.extend(_complexity_failures(complexity, baseline))
            failures.extend(_c_function_failures(c_functions, baseline))
            failures.extend(_fastcore_parity_failures())
            failures.extend(_exception_failures())
    except (GateError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"code-health-gate: unavailable: {exc}", file=sys.stderr)
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
    elif args.changed:
        print(
            f"code-health-gate (changed): passed ({len(file_metrics)} touched files; base {args.base})"
        )
        print(f"skipped: {', '.join(skipped) if skipped else 'nothing'}")
    else:
        print("code-health-gate: passed")
    return 0


def _changed_failures(
    base: str, baseline: dict[str, Any]
) -> tuple[list[str], dict[str, dict[str, int]], list[str]]:
    files = _touched_files(base)
    py_files = [path for path in files if path.suffix.lower() == ".py"]
    c_files = [path for path in files if path.suffix.lower() == ".c"]
    failures, file_metrics = _check_files(files, baseline, check_guard=False)
    skipped = ["guard_files", "full-repo scan"]
    if py_files:
        failures.extend(_complexity_failures(_run_complexipy(paths=py_files), baseline))
    else:
        skipped.append("complexipy (no touched .py)")
    if c_files:
        failures.extend(_c_function_failures(_run_lizard(paths=c_files), baseline))
    else:
        skipped.append("lizard/C functions (no touched .c)")
    fastcore_inputs = {
        "packages/core/src/moex_spread_lab/core/_fastcore.c",
        "packages/proof/tests/test_fastcore_parity.py",
    }
    if fastcore_inputs & {path.relative_to(ROOT).as_posix() for path in files}:
        failures.extend(_fastcore_parity_failures())
    else:
        skipped.append("fastcore parity (inputs untouched)")
    protected = [path for path in py_files if _is_under(path, PROTECTED_EXCEPTION_ROOTS)]
    failures.extend(_exception_failures(protected))
    return failures, file_metrics, skipped


def _is_under(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


if __name__ == "__main__":
    raise SystemExit(main())
