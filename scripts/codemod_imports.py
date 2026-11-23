#!/usr/bin/env python3
"""Idempotent import-path codemods with a diff-first, explicit-apply workflow."""

from __future__ import annotations

import argparse
import ast
import difflib
import keyword
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _mapping(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        old, separator, new = value.partition("=")
        if not separator or not old or not new:
            raise ValueError(f"expected OLD=NEW, got {value!r}")
        result[old] = new
    return result


def rewrite_imports(source: str, mapping: dict[str, str], filename: str = "<source>") -> str:
    """Rewrite only AST-confirmed absolute ``from old import`` statements."""
    tree = ast.parse(source, filename=filename)
    lines = source.splitlines(keepends=True)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level or node.module not in mapping:
            continue
        module = node.module
        target = mapping[module]
        index = node.lineno - 1
        line = lines[index]
        pattern = re.compile(r"^(\s*from\s+)" + re.escape(module) + r"(?=\s+import\b)")
        rewritten, count = pattern.subn(
            lambda match, replacement=target: match.group(1) + replacement,
            line,
            count=1,
        )
        if count != 1:
            raise ValueError(f"could not locate import at {filename}:{node.lineno}")
        lines[index] = rewritten
    result = "".join(lines)
    ast.parse(result, filename=filename)
    return result


def _show_diff(path: Path, before: str, after: str) -> None:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=str(path),
        tofile=str(path) + " (codemod)",
    )
    sys.stdout.writelines(diff)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def rewrite_files(paths: list[Path], mapping: dict[str, str], *, apply: bool) -> int:
    changed = 0
    for path in paths:
        before = path.read_text(encoding="utf-8")
        after = rewrite_imports(before, mapping, str(path))
        if after == before:
            continue
        _show_diff(path, before, after)
        changed += 1
        if apply:
            _atomic_write(path, after)
    print(f"{'applied' if apply else 'preview'}: {changed} file(s) would change")
    return 0


def render_shim(module: str, symbols: list[str]) -> str:
    if not module or not symbols:
        raise ValueError("module and at least one symbol are required")
    module_parts = module.split(".")
    if any(not part.isidentifier() or keyword.iskeyword(part) for part in module_parts):
        raise ValueError("module must be a dotted Python identifier")
    if any(not symbol.isidentifier() or keyword.iskeyword(symbol) for symbol in symbols):
        raise ValueError("re-export names must be Python identifiers")
    unique = list(dict.fromkeys(symbols))
    lines = [
        '"""Compatibility re-exports; implementation lives in the package module."""',
        "",
        f"from {module} import (",
        *(f"    {symbol}," for symbol in unique),
        ")",
        "",
        "__all__ = (",
        *(f'    "{symbol}",' for symbol in unique),
        ")",
        "",
    ]
    return "\n".join(lines)


def create_shim(path: Path, module: str, symbols: list[str], *, apply: bool) -> int:
    content = render_shim(module, symbols)
    if path.exists():
        before = path.read_text(encoding="utf-8")
        if before == content:
            print(f"unchanged: {path}")
            return 0
        _show_diff(path, before, content)
        print("refusing to replace an existing file with different content", file=sys.stderr)
        return 1
    _show_diff(path, "", content)
    if apply:
        _atomic_write(path, content)
        print(f"created: {path}")
    else:
        print("preview only; pass --apply to write")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    rewrite = sub.add_parser("rewrite", help="rewrite absolute from-import module paths")
    rewrite.add_argument("--map", action="append", default=[], metavar="OLD=NEW", required=True)
    rewrite.add_argument(
        "--apply", action="store_true", help="write changes; default only prints a diff"
    )
    rewrite.add_argument("files", nargs="+")
    shim = sub.add_parser("shim", help="generate a deterministic compatibility re-export module")
    shim.add_argument("path")
    shim.add_argument("--from", dest="module", required=True)
    shim.add_argument("--symbols", nargs="+", required=True)
    shim.add_argument(
        "--apply", action="store_true", help="write changes; default only prints a diff"
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "rewrite":
            paths = [
                Path(value) if Path(value).is_absolute() else ROOT / value for value in args.files
            ]
            return rewrite_files(paths, _mapping(args.map), apply=args.apply)
        target = Path(args.path) if Path(args.path).is_absolute() else ROOT / args.path
        return create_shim(target, args.module, args.symbols, apply=args.apply)
    except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
        print(f"codemod: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
