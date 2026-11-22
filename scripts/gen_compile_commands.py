#!/usr/bin/env python3
"""Generate git-ignored compile_commands.json for C99 `_fastcore.c` static analysis."""

from __future__ import annotations

import argparse
import json
import shutil
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
C_SOURCE = ROOT / "packages/core/src/moex_spread_lab/core/_fastcore.c"
OUTPUT_PATH = ROOT / "compile_commands.json"


def build_compile_commands(root: Path = ROOT) -> list[dict[str, object]]:
    c_path = root / "packages/core/src/moex_spread_lab/core/_fastcore.c"
    if not c_path.is_file():
        raise FileNotFoundError(f"missing C source: {c_path}")
    compiler = shutil.which("clang") or shutil.which("cc")
    if compiler is None:
        raise RuntimeError("neither clang nor cc is available on PATH")
    include_dir = sysconfig.get_path("include")
    if not include_dir or not Path(include_dir, "Python.h").is_file():
        raise RuntimeError(f"Python.h was not found in {include_dir!r}")
    obj_path = c_path.with_suffix(".o")
    return [
        {
            "directory": str(root),
            "file": str(c_path),
            "arguments": [
                compiler,
                "-std=c99",
                "-O3",
                "-ffp-contract=off",
                "-fPIC",
                "-Wall",
                "-Wextra",
                f"-I{include_dir}",
                "-c",
                str(c_path),
                "-o",
                str(obj_path),
            ],
        }
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help="output path (defaults to repo-root compile_commands.json)",
    )
    args = parser.parse_args(argv)
    entries = build_compile_commands(ROOT)
    args.output.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
