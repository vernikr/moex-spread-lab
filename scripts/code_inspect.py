#!/usr/bin/env python3
"""Compact source inspection: outline, symbol callers, and fast edit checks."""

from __future__ import annotations

import argparse
import ast
import io
import shutil
import subprocess
import sys
import tokenize
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAX_LOC = 400
DEFAULT_MAX_TOKENS = 3500
DEFAULT_MAX_COMPLEXITY = 15


def _path(value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else ROOT / path).resolve()


def _tokens(source: str) -> int:
    ignored = {
        tokenize.ENCODING,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.COMMENT,
        tokenize.ENDMARKER,
    }
    return sum(
        1
        for item in tokenize.generate_tokens(io.StringIO(source).readline)
        if item.type not in ignored
    )


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
    print(f"LOC {len(lines)} | tokens {_tokens(source)} | bytes {len(source.encode('utf-8'))}")
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


def _category(path: Path) -> str:
    parts = path.parts
    if "tests" in parts or path.name.startswith("test_"):
        return "tests"
    if "src" in parts and "moex_spread_lab" in parts:
        # The top-level src tree is the compatibility facade; package/src trees are production.
        try:
            relative = path.relative_to(ROOT)
        except ValueError:
            relative = path
        if relative.parts[:2] == ("src", "moex_spread_lab"):
            return "compatibility-facade"
    return "production"


def _python_files() -> list[Path]:
    roots = (ROOT / "packages", ROOT / "src", ROOT / "scripts", ROOT / "tests")
    excluded = {".git", ".venv", "__pycache__", "node_modules", ".typecheck_typings"}
    return sorted(
        path
        for root in roots
        if root.is_dir()
        for path in root.rglob("*.py")
        if not excluded.intersection(path.parts)
    )


def _reference_line(node: ast.AST, symbol: str) -> int | None:
    if isinstance(node, ast.Name) and node.id == symbol:
        return node.lineno
    if isinstance(node, ast.Attribute) and node.attr == symbol:
        return node.lineno
    if isinstance(node, ast.alias) and node.name == symbol:
        return node.lineno
    return None


def _is_definition(node: ast.AST, symbol: str) -> bool:
    return (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name == symbol
    )


def _symbol_lines(tree: ast.AST, symbol: str) -> list[int]:
    lines = {
        line
        for node in ast.walk(tree)
        if not _is_definition(node, symbol)
        if (line := _reference_line(node, symbol)) is not None
    }
    return sorted(lines)


def _file_callers(path: Path, symbol: str) -> tuple[str, list[tuple[str, int, str]]] | None:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        return None
    lines = _symbol_lines(tree, symbol)
    if not lines:
        return None
    relative = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    source_lines = source.splitlines()
    return _category(path), [
        (relative, line, source_lines[line - 1].strip()[:180]) for line in lines
    ]


def _print_callers(hits: dict[str, list[tuple[str, int, str]]]) -> None:
    total = 0
    for category in ("production", "tests", "compatibility-facade"):
        rows = hits.get(category, [])
        print(f"{category}: {len(rows)}")
        for path, line, snippet in rows:
            print(f"  {path}:{line}: {snippet}")
        total += len(rows)
    print(f"total references: {total}")


def callers(symbol: str) -> int:
    hits: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
    for path in _python_files():
        result = _file_callers(path, symbol)
        if result is not None:
            category, rows = result
            hits[category].extend(rows)
    _print_callers(hits)
    return 0


def _run(label: str, command: list[str]) -> bool:
    try:
        result = subprocess.run(command, cwd=ROOT, check=False, text=True)
    except OSError as exc:
        print(f"{label}: unavailable ({exc})")
        return False
    print(f"{label}: {'PASS' if result.returncode == 0 else 'FAIL'}")
    return result.returncode == 0


def _size_metrics(path: Path) -> tuple[int, int, dict[str, int]]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    complexities = {
        f"{node.name}@{node.lineno}": _complexity(node) for node in _function_nodes(tree)
    }
    return len(source.splitlines()), int(len(source.encode("utf-8")) / 3.6), complexities


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
        loc, tokens, complexities = _size_metrics(path)
    except (OSError, UnicodeError, SyntaxError) as exc:
        print(f"parse: {path}: {exc}")
        return False, False
    offenders = [
        f"{name}={value}" for name, value in complexities.items() if value > DEFAULT_MAX_COMPLEXITY
    ]
    over = loc > DEFAULT_MAX_LOC or tokens > DEFAULT_MAX_TOKENS or bool(offenders)
    status = "FAIL" if strict_limits and over else "WARN" if over else "PASS"
    display = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    print(f"{display}: {status} LOC={loc}/{DEFAULT_MAX_LOC} tokens={tokens}/{DEFAULT_MAX_TOKENS}")
    for item in offenders:
        print(f"  complexity ceiling {DEFAULT_MAX_COMPLEXITY}: {item}")
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
    caller_parser = sub.add_parser("callers", help="production/test/facade references to a symbol")
    caller_parser.add_argument("symbol")
    check_parser = sub.add_parser(
        "check", help="fast lint, format, token/LOC/complexity and type checks"
    )
    check_parser.add_argument("files", nargs="+")
    check_parser.add_argument(
        "--strict-limits", action="store_true", help="fail over repository ceilings"
    )
    check_parser.add_argument("--metrics-only", action="store_true", help="skip external tools")
    args = parser.parse_args(argv)
    try:
        if args.command == "outline":
            return outline(_path(args.file))
        if args.command == "callers":
            return callers(args.symbol)
        return check(
            [_path(value) for value in args.files],
            strict_limits=args.strict_limits,
            metrics_only=args.metrics_only,
        )
    except (OSError, UnicodeError, SyntaxError) as exc:
        print(f"inspect: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
