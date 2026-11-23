#!/usr/bin/env python3
"""List repository files touched by the current change.

Used by ``check_health.sh --changed`` and ``code_health_gate.py --changed`` to scope
checks to touched files. The change set is the union of:

* ``git diff --name-only <base>`` — tracked files differing from ``<base>``
  (default ``HEAD``) in the working tree, which covers both committed-on-branch
  and unstaged/staged edits when ``<base>`` is a branch point;
* ``git ls-files --others --exclude-standard`` — untracked, non-ignored files.

``--imports-changed`` exits 0 (true) when the change adds, removes or moves any
``import``/``from ... import`` statement, adds/renames/deletes a Python module,
or adds an untracked Python file — i.e. when the architecture import graph can
differ from HEAD. Exits 1 (false) otherwise, 2 on git errors.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_IMPORT_RE = re.compile(r"^[+-]\s*(?:import\s|from\s+\S+\s+import\b)", re.MULTILINE)


def _git(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"git {' '.join(args)} failed: {exc}") from exc
    return result.stdout


def _resolve_base(base: str) -> str:
    try:
        return _git(["rev-parse", "--verify", f"{base}^{{commit}}"]).strip()
    except RuntimeError as exc:
        raise RuntimeError(f"cannot resolve base revision {base!r}: {exc}") from exc


def changed_paths(
    base: str = "HEAD", existing_only: bool = True, suffixes: tuple[str, ...] | None = None
) -> list[str]:
    """Repo-relative touched paths, sorted, deduplicated."""
    base = _resolve_base(base)
    tracked = _git(["diff", "--name-only", "-z", base, "--"]).split("\0")
    untracked = _git(["ls-files", "--others", "--exclude-standard", "-z"]).split("\0")
    paths = sorted({path for path in (*tracked, *untracked) if path})
    if suffixes:
        lowered = tuple(suffix.lower() for suffix in suffixes)
        paths = [path for path in paths if path.lower().endswith(lowered)]
    if existing_only:
        paths = [path for path in paths if (ROOT / path).is_file()]
    return paths


def imports_changed(base: str = "HEAD") -> bool:
    base = _resolve_base(base)
    status = _git(["diff", "--name-status", "-M", base, "--"])
    for line in status.splitlines():
        parts = line.split("\t")
        code, paths = parts[0], parts[1:]
        if code[0] in "ADR" and any(path.endswith(".py") for path in paths):
            return True
    untracked_py = _git(["ls-files", "--others", "--exclude-standard", "--", "*.py"]).strip()
    if untracked_py:
        return True
    return bool(_IMPORT_RE.search(_git(["diff", "-M", base, "--", "*.py"])))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="HEAD", help="base revision (default: HEAD)")
    parser.add_argument(
        "--ext",
        action="append",
        default=[],
        help="only keep paths with this suffix (repeatable, e.g. --ext .py)",
    )
    parser.add_argument(
        "--all",
        dest="existing_only",
        action="store_false",
        help="include deleted paths (listed but missing on disk)",
    )
    parser.add_argument(
        "--imports-changed",
        action="store_true",
        help="exit 0 when the change can alter the import graph, else exit 1",
    )
    args = parser.parse_args(argv)
    try:
        if args.imports_changed:
            return 0 if imports_changed(args.base) else 1
        paths = changed_paths(
            args.base,
            existing_only=args.existing_only,
            suffixes=tuple(args.ext) if args.ext else None,
        )
    except RuntimeError as exc:
        print(f"changed-files: {exc}", file=sys.stderr)
        return 2
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
