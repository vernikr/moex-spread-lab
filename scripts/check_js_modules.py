#!/usr/bin/env python3
"""Lightweight checks for the project's ES modules; Node adds a full syntax check.

The identifier guard is heuristic, not a JS scope analyser: it checks references to
sibling top-level declarations and ignores properties and object keys. Node is
optional; without it syntax checking is skipped explicitly.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

JS_DIR = Path(__file__).resolve().parents[1] / "packages/web/src/moex_spread_lab/web/static/js"
IDENT = r"[A-Za-z_$][\w$]*"
IMPORT = re.compile(r"\bimport\s*\{([^}]*)\}\s*from\s*['\"]([^'\"]+)['\"]\s*;?", re.S)
EXPORT = re.compile(r"\bexport\s*\{([^}]*)\}(?:\s*from\s*['\"]([^'\"]+)['\"])?\s*;?", re.S)
DECL = re.compile(
    rf"^(?:export\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+({IDENT})", re.M
)
EXPORTED = re.compile(rf"^export\s+(?:async\s+)?(?:function|class|const|let|var)\s+({IDENT})", re.M)
# Preserve template interpolations while masking strings and comments.
MASK = re.compile(r"//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")
TOKEN = re.compile(rf"{IDENT}|\?\.|[^\s]", re.M)


def bindings(spec: str) -> list[tuple[str, str]]:
    result = []
    for item in spec.split(","):
        parts = item.strip().split()
        if not parts:
            continue
        if len(parts) == 1:
            result.append((parts[0], parts[0]))
        elif len(parts) == 3 and parts[1] == "as":
            result.append((parts[0], parts[2]))
        else:
            raise ValueError(f"unsupported binding: {item.strip()}")
    return result


def _bindings_check(
    source: dict[Path, str],
) -> tuple[list[str], dict[Path, set[str]], dict[Path, set[str]]]:
    paths = sorted(source)
    errors: list[str] = []
    declared = {p: set(DECL.findall(text)) for p, text in source.items()}
    public = {p: set(EXPORTED.findall(text)) for p, text in source.items()}
    local_imports: dict[Path, set[str]] = {p: set() for p in paths}
    named_imports: dict[Path, list[tuple[Path, str]]] = {p: [] for p in paths}
    reexports: list[tuple[Path, Path | None, str, str]] = []

    for path in paths:
        _parse_file(path, source, errors, local_imports, named_imports, reexports)

    errors.extend(_resolve_exports(public, declared, local_imports, named_imports, reexports))
    return errors, declared, local_imports


def _target(path: Path, name: str, source: dict[Path, str], errors: list[str]) -> Path | None:
    dest = (path.parent / name).resolve()
    if not name.startswith(".") or dest not in source:
        errors.append(f"{path.name}: unresolved module {name}")
        return None
    return dest


def _parse_file(
    path: Path,
    source: dict[Path, str],
    errors: list[str],
    local_imports: dict[Path, set[str]],
    named_imports: dict[Path, list[tuple[Path, str]]],
    reexports: list[tuple[Path, Path | None, str, str]],
) -> None:
    seen: set[str] = set()
    for match in IMPORT.finditer(source[path]):
        dest_name = match.group(2)
        if dest_name in seen:
            errors.append(f"{path.name}: duplicate import from {dest_name}")
        seen.add(dest_name)
        dest = _target(path, dest_name, source, errors)
        for original, alias in bindings(match.group(1)):
            local_imports[path].add(alias)
            if dest:
                named_imports[path].append((dest, original))
    for match in EXPORT.finditer(source[path]):
        dest = _target(path, match.group(2), source, errors) if match.group(2) else None
        for original, alias in bindings(match.group(1)):
            reexports.append((path, dest, original, alias))


def _resolve_exports(
    public: dict[Path, set[str]],
    declared: dict[Path, set[str]],
    local_imports: dict[Path, set[str]],
    named_imports: dict[Path, list[tuple[Path, str]]],
    reexports: list[tuple[Path, Path | None, str, str]],
) -> list[str]:
    errors: list[str] = []
    # Fixed point supports chains and cycles without recursion.
    changed = True
    while changed:
        changed = False
        for path, dest, original, alias in reexports:
            available = public[dest] if dest else declared[path] | local_imports[path]
            if original in available and alias not in public[path]:
                public[path].add(alias)
                changed = True
    errors.extend(_export_errors(public, declared, local_imports, named_imports, reexports))

    return errors


def _export_errors(
    public: dict[Path, set[str]],
    declared: dict[Path, set[str]],
    local_imports: dict[Path, set[str]],
    named_imports: dict[Path, list[tuple[Path, str]]],
    reexports: list[tuple[Path, Path | None, str, str]],
) -> list[str]:
    errors: list[str] = []
    for path, dest, original, _alias in reexports:
        available = public[dest] if dest else declared[path] | local_imports[path]
        if original not in available:
            errors.append(
                f"{path.name}: cannot export {original} from {dest.name if dest else 'local bindings'}"
            )
    for path, entries in named_imports.items():
        for dest, original in entries:
            if original not in public[dest]:
                errors.append(f"{path.name}: {dest.name} does not export {original}")

    return errors


def _identifier_check(
    source: dict[Path, str], declared: dict[Path, set[str]], local_imports: dict[Path, set[str]]
) -> list[str]:
    errors: list[str] = []
    siblings = set().union(*declared.values())
    for path, text in source.items():
        # Ignore comments and quoted strings, but retain ${...} in templates.
        text = IMPORT.sub("", EXPORT.sub("", text))
        text = MASK.sub(" ", text)
        tokens = TOKEN.findall(text)
        local = declared[path] | local_imports[path]
        # Any explicit local declaration/parameter may shadow a sibling name.
        local.update(re.findall(rf"\b(?:const|let|var|function|class)\s+({IDENT})", text))
        errors.extend(_token_errors(path, tokens, siblings, local))

    return errors


def _token_errors(path: Path, tokens: list[str], siblings: set[str], local: set[str]) -> list[str]:
    errors: list[str] = []
    for i, token in enumerate(tokens):
        if token not in siblings or token in local:
            continue
        previous = tokens[i - 1] if i else ""
        following = tokens[i + 1] if i + 1 < len(tokens) else ""
        if previous in {".", "?."} or following == ":":
            continue
        if following in {"(", ".", "?.", "["}:
            errors.append(f"{path.name}: possible undefined sibling symbol {token}")
            local.add(token)

    return errors


def _syntax_check(paths: list[Path]) -> list[str]:
    errors: list[str] = []

    if node := shutil.which("node"):
        # node --check accepts only one file; vm.SourceTextModule parses all
        # modules in one process without executing application code.
        program = """const fs = require('fs'), vm = require('vm');
let failed = false;
for (const file of process.argv.slice(1)) {
  try { new vm.SourceTextModule(fs.readFileSync(file, 'utf8'), {identifier: file}); }
  catch (err) { console.error(file + ': ' + err.message); failed = true; }
}
if (failed) process.exitCode = 1;"""
        result = subprocess.run(
            [
                node,
                "--no-warnings",
                "--experimental-vm-modules",
                "-e",
                program,
                *map(str, paths),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            errors.append(f"JavaScript syntax check failed:\n{result.stderr.strip()}")
    else:
        print("js-modules: node not found; syntax check skipped", file=sys.stderr)
    return errors


def check_modules(directory: Path, *, syntax: bool = True) -> list[str]:
    paths = sorted(directory.glob("*.js"))
    if not paths:
        return [f"{directory}: no JavaScript modules found"]
    source = {p.resolve(): p.read_text(encoding="utf-8") for p in paths}
    errors, declared, local_imports = _bindings_check(source)
    errors.extend(_identifier_check(source, declared, local_imports))
    if syntax:
        errors.extend(_syntax_check(paths))
    return errors


def main() -> int:
    errors = check_modules(JS_DIR)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("js-modules: passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
