#!/usr/bin/env python3
"""Compact source inspection: outline and fast edit checks."""

from __future__ import annotations

import argparse
import ast
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.code_health_gate_common import POLICY, GateError, _budget_for, _file_metrics

ROOT = Path(__file__).resolve().parents[1]
# Size budgets, the token estimator and the complexity ceiling are the gate's own
# policy (TD-1 #152, TD-6 #158): the inner-loop helper must never disagree with it.
MAX_COMPLEXITY = int(POLICY["max_cognitive_complexity"])
SIZE_MEASURES = ("loc", "tokens")


def _path(value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else ROOT / path).resolve()


def _relative_key(path: Path) -> str:
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.as_posix()


def _band(relative: str, metrics: dict[str, int]) -> tuple[str, str, dict[str, int]]:
    archetype, budget = _budget_for(relative)
    band = "green"
    for measure in SIZE_MEASURES:
        hard = budget.get(f"hard_{measure}")
        if hard is None:
            continue
        if metrics[measure] > hard:
            return archetype, "over-hard", budget
        if metrics[measure] > budget[f"soft_{measure}"]:
            band = "advisory"
    return archetype, band, budget


def _measure_text(measure: str, value: int, budget: dict[str, int]) -> str:
    hard = budget.get(f"hard_{measure}")
    if hard is None:
        return f"{measure}={value} (not budgeted)"
    return f"{measure}={value} (soft {budget[f'soft_{measure}']}, hard {hard})"


def _budget_text(path: Path) -> tuple[str, str]:
    metrics = _file_metrics(path)
    archetype, band, budget = _band(_relative_key(path), metrics)
    sizes = " ".join(_measure_text(name, metrics[name], budget) for name in SIZE_MEASURES)
    return band, f"archetype={archetype} band={band} {sizes}"


def _complexity(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    score = 1

    def visit(current: ast.AST) -> None:
        nonlocal score
        for child in ast.iter_child_nodes(current):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(
                child, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp)
            ):
                score += 1
            elif isinstance(child, ast.BoolOp):
                score += max(0, len(child.values) - 1)
            elif isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                score += len(child.generators)
            elif isinstance(child, ast.match_case) and child.guard is not None:
                score += 1
            visit(child)

    visit(node)
    return score


def _function_nodes(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def outline(path: Path) -> int:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines()
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.append(f"{node.lineno}: import {', '.join(alias.name for alias in node.names)}")
        elif isinstance(node, ast.ImportFrom):
            imports.append(f"{node.lineno}: from {'.' * node.level}{node.module or ''} import ...")
    imports.sort(key=lambda item: int(item.split(":", 1)[0]))
    print(f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    print(f"{_budget_text(path)[1]} bytes={len(source.encode('utf-8'))} total_lines={len(lines)}")
    print("Imports:")
    for item in imports:
        print(f"  {item}")
    print("Definitions:")
    functions = _function_nodes(tree)
    for node in sorted(functions, key=lambda item: (item.lineno, item.name)):
        end = node.end_lineno or node.lineno
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        try:
            args = ast.unparse(node.args)
        except (AttributeError, ValueError):
            args = "..."
        print(f"  {node.lineno}-{end}: {prefix} {node.name}({args}) [CCN {_complexity(node)}]")
    classes = sorted(
        (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)),
        key=lambda item: item.lineno,
    )
    for node in classes:
        print(f"  {node.lineno}-{node.end_lineno or node.lineno}: class {node.name}")
    return 0


def _run(label: str, command: list[str]) -> bool:
    try:
        result = subprocess.run(command, cwd=ROOT, check=False, text=True)
    except OSError as exc:
        print(f"{label}: unavailable ({exc})")
        return False
    print(f"{label}: {'PASS' if result.returncode == 0 else 'FAIL'}")
    return result.returncode == 0


def _complexities(path: Path) -> dict[str, int]:
    if path.suffix != ".py":
        return {}
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {f"{node.name}@{node.lineno}": _complexity(node) for node in _function_nodes(tree)}


def _ruff_command() -> list[str]:
    local = ROOT / ".venv/bin/ruff"
    if local.is_file():
        return [str(local)]
    executable = shutil.which("ruff")
    if executable:
        return [executable]
    uv = shutil.which("uv")
    return [uv, "run", "--no-sync", "ruff"] if uv else [sys.executable, "-m", "ruff"]


def _check_file(path: Path, *, strict_limits: bool) -> tuple[bool, bool]:
    if not path.is_file():
        print(f"missing file: {path}")
        return False, False
    try:
        band, summary = _budget_text(path)
        complexities = _complexities(path)
    except (GateError, OSError, UnicodeError, SyntaxError) as exc:
        print(f"parse: {path}: {exc}")
        return False, False
    offenders = [
        f"{name}={value}" for name, value in complexities.items() if value > MAX_COMPLEXITY
    ]
    # Like the gate, the advisory band never fails; only hard ceilings do.
    over = band == "over-hard" or bool(offenders)
    status = "FAIL" if strict_limits and over else "WARN" if over else "PASS"
    print(f"{_relative_key(path)}: {status} {summary}")
    for item in offenders:
        print(f"  complexity ceiling {MAX_COMPLEXITY}: {item}")
    return not (strict_limits and over), True


def _run_file_checks(paths: list[Path]) -> bool:
    arguments = [
        str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path) for path in paths
    ]
    results = (
        _run("ruff check", [*_ruff_command(), "check", *arguments]),
        _run("ruff format", [*_ruff_command(), "format", "--check", *arguments]),
        _run("basedpyright", [sys.executable, "scripts/typecheck_workspace.py", *arguments]),
    )
    return all(results)


def check(paths: list[Path], *, strict_limits: bool, metrics_only: bool) -> int:
    if not paths:
        print("inspect check: pass at least one file")
        return 2
    success = True
    valid: list[Path] = []
    for path in paths:
        file_success, is_valid = _check_file(path, strict_limits=strict_limits)
        success &= file_success
        if is_valid:
            valid.append(path)
    if not metrics_only and valid:
        success &= _run_file_checks(valid)
    return 0 if success else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    outline_parser = sub.add_parser("outline", help="imports, definitions, ranges and complexity")
    outline_parser.add_argument("file")
    check_parser = sub.add_parser(
        "check", help="fast lint, format, token/LOC/complexity and type checks"
    )
    check_parser.add_argument("files", nargs="+")
    check_parser.add_argument(
        "--strict-limits", action="store_true", help="fail over the gate's hard ceilings"
    )
    check_parser.add_argument("--metrics-only", action="store_true", help="skip external tools")
    args = parser.parse_args(argv)
    try:
        if args.command == "outline":
            return outline(_path(args.file))
        return check(
            [_path(value) for value in args.files],
            strict_limits=args.strict_limits,
            metrics_only=args.metrics_only,
        )
    except (GateError, OSError, UnicodeError, SyntaxError) as exc:
        print(f"inspect: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
