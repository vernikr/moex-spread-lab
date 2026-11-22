#!/usr/bin/env python3
"""Determine whether C99 Tier 2 static-analysis & sanitizer checks should run in CI."""

from __future__ import annotations

import argparse
import fnmatch
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TIER2_PATTERNS = (
    ".clang-tidy",
    "packages/core/src/moex_spread_lab/core/_fastcore.c",
    "packages/core/src/moex_spread_lab/core/_fastcore_loader.py",
    "packages/core/tests/test_fastcore_sanitizers.py",
    "packages/proof/tests/test_fastcore_parity.py",
    "scripts/gen_compile_commands.py",
    "scripts/run_sanitized_pytest.py",
    "scripts/should_run_c_tier2.py",
    ".github/workflows/ci.yml",
)

NULL_SHA = "0" * 40


def matches_c_tier2(paths: list[str]) -> bool:
    """Return True if any changed repository-relative path requires C99 Tier 2 checks."""
    for raw in paths:
        norm = raw.strip().replace("\\", "/").removeprefix("./").lstrip("/")
        if not norm:
            continue
        if any(fnmatch.fnmatch(norm, pat) for pat in TIER2_PATTERNS):
            return True
    return False


def _git_diff_names(base: str, head: str, root: Path) -> list[str] | None:
    res = subprocess.run(
        ["git", "diff", "--name-only", f"{base}..{head}"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return None
    return [line for line in res.stdout.splitlines() if line.strip()]


def decide_tier2(
    *,
    event_name: str,
    base_sha: str = "",
    head_sha: str = "HEAD",
    root: Path = ROOT,
) -> tuple[bool, str]:
    """Return `(should_run, reason)` for GitHub Actions or local execution."""
    if event_name in {"schedule", "workflow_dispatch"}:
        return True, f"event={event_name} runs full Tier 2 unconditionally"
    if not base_sha or base_sha == NULL_SHA:
        return True, "missing base SHA; running Tier 2 conservatively"
    changed = _git_diff_names(base_sha, head_sha, root)
    if changed is None:
        return True, f"git diff {base_sha}..{head_sha} unavailable; running Tier 2 conservatively"
    if matches_c_tier2(changed):
        return True, "C99 / sanitizer / CI paths changed"
    return False, f"no C99 Tier 2 paths in {len(changed)} changed file(s)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", default="push", help="GitHub event name")
    parser.add_argument("--base", default="", help="Base commit SHA")
    parser.add_argument("--head", default="HEAD", help="Head commit SHA")
    parser.add_argument(
        "--github-output",
        type=Path,
        default=None,
        help="Optional $GITHUB_OUTPUT file path to append run_tier2=true/false",
    )
    args = parser.parse_args(argv)
    should_run, reason = decide_tier2(event_name=args.event, base_sha=args.base, head_sha=args.head)
    flag = "true" if should_run else "false"
    print(f"run_tier2={flag} ({reason})")
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"run_tier2={flag}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
