#!/usr/bin/env python3
"""Determine whether C99 Tier 2 static-analysis & sanitizer checks should run in CI.

Thin wrapper kept for backwards compatibility: the Tier 2 patterns and the
decision logic live in ``scripts/change_scope.py`` (the S2 path router), this
script keeps its CLI and delegates to it (plan S2: ``should_run_c_tier2.py``
keeps its CLI and delegates).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # allow `from scripts...` when executed as a plain script

from scripts.change_scope import (  # noqa: E402
    NULL_SHA,
    TIER2_PATTERNS,
    _conservative_ci_reason,  # re-exported for backwards compatibility
    decide_tier2,
    git_diff_names,
    matches_c_tier2,
)

__all__ = [
    "NULL_SHA",
    "ROOT",
    "TIER2_PATTERNS",
    "_conservative_ci_reason",
    "decide_tier2",
    "git_diff_names",
    "main",
    "matches_c_tier2",
]

# Backwards-compatible alias (was ``_git_diff_names`` in an earlier version).
_git_diff_names = git_diff_names


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
