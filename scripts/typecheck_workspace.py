#!/usr/bin/env python3
"""Build the unified namespace typing view (git-ignored symlinks only), then run
basedpyright on the workspace: one deterministic namespace root, sources stay put."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ViewError(RuntimeError):
    pass


def _add_link(destination: Path, source: Path, view_root: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise ViewError(f"namespace collision at {destination.relative_to(view_root)}")
    destination.symlink_to(source.resolve(), target_is_directory=source.is_dir())


def build_view(root: Path = ROOT) -> Path:
    view_root = root / ".typecheck_typings"
    namespace_view = view_root / "moex_spread_lab"
    marker = view_root / ".generated-by-typecheck-workspace"
    package_roots = tuple(
        root / rel
        for rel in (
            "packages/core/src/moex_spread_lab",
            "packages/data/src/moex_spread_lab",
            "packages/accounting/src/moex_spread_lab",
            "packages/strategies/src/moex_spread_lab",
            "packages/proof/src/moex_spread_lab",
            "packages/web/src/moex_spread_lab",
            "src/moex_spread_lab",
        )
    )
    missing = [path for path in package_roots if not path.is_dir()]
    if missing:
        raise ViewError("missing namespace roots: " + ", ".join(str(path) for path in missing))
    if view_root.exists() and not marker.is_file():
        raise ViewError(f"refusing to replace unmarked generated path: {view_root}")
    if marker.is_file():
        shutil.rmtree(view_root)
    namespace_view.mkdir(parents=True)
    marker.write_text("generated; safe to recreate\n", encoding="utf-8")

    entries: dict[str, Path] = {}
    for source_root in package_roots:
        for child in sorted(source_root.iterdir()):
            if child.name in {"__pycache__", ".DS_Store"}:
                continue
            previous = entries.get(child.name)
            if previous is not None:
                raise ViewError(f"duplicate namespace entry {child.name!r}: {previous} and {child}")
            entries[child.name] = child
    for name, source in entries.items():
        _add_link(namespace_view / name, source, view_root)
    return view_root


def _basedpyright_command() -> list[str]:
    local = ROOT / ".venv/bin/basedpyright"
    if local.is_file():
        return [str(local)]
    executable = shutil.which("basedpyright")
    if executable:
        return [executable]
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "--no-sync", "basedpyright"]
    raise ViewError("basedpyright is unavailable; run `uv sync --frozen`")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build-only", action="store_true", help="generate the view without type-checking"
    )
    parser.add_argument(
        "paths", nargs="*", help="optional files/directories passed to basedpyright"
    )
    args = parser.parse_args(argv)
    try:
        build_view()
        if args.build_only:
            print("typecheck namespace view: .typecheck_typings")
            return 0
        result = subprocess.run(_basedpyright_command() + args.paths, cwd=ROOT, check=False)
    except (OSError, ViewError) as exc:
        print(f"typecheck workspace: {exc}", file=sys.stderr)
        return 2
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
