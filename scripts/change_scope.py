#!/usr/bin/env python3
"""CLI for the S2 change-scope router (docs/development/agent-performance-2/plan.md).

Shared by the local launcher (``scripts/verify.py``) and the ``scope`` job in
``.github/workflows/ci.yml``. Without ``--event`` it routes the working-tree change
set (tracked diff vs ``--base``, untracked files included); with ``--event`` it
routes the CI diff (``git diff base..head``). On any internal error it resolves to
``full`` (CI contract). Routing is advisory only.

Examples:

    python3 scripts/change_scope.py                         # local decision, human output
    python3 scripts/change_scope.py --format json            # machine-readable decision
    python3 scripts/change_scope.py --event push --base SHA --head SHA --github-output $GITHUB_OUTPUT
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # allow `from scripts...` when executed as a plain script

from scripts.change_scope_core import (  # noqa: E402  (path inserted above)
    NULL_SHA,
    PACKAGES,
    REVERSE_DEPENDENTS,
    TIER2_PATTERNS,
    ScopeDecision,
    conservative_ci_reason,
    decide_paths,
    decide_tier2,
    git_diff_names,
    git_diff_new_or_deleted,
    is_docs_path,
    matches_c_tier2,
    norm_path,
    package_of,
    touched_packages,
)

__all__ = [
    "NULL_SHA",
    "PACKAGES",
    "REVERSE_DEPENDENTS",
    "ROOT",
    "TIER2_PATTERNS",
    "ScopeDecision",
    "conservative_ci_reason",
    "decide_ci",
    "decide_local",
    "decide_paths",
    "decide_tier2",
    "git_diff_names",
    "git_diff_new_or_deleted",
    "is_docs_path",
    "local_change_set",
    "main",
    "matches_c_tier2",
    "norm_path",
    "package_of",
    "render_report",
    "touched_packages",
    "write_github_output",
]

# Backwards-compatible alias (was `_git_diff_names` in an earlier version).
_git_diff_names = git_diff_names
_conservative_ci_reason = conservative_ci_reason  # was module-private before the split


def local_change_set(base: str = "HEAD", root: Path = ROOT) -> tuple[list[str], set[str]]:
    """Return ``(paths, new_or_deleted)`` for the working-tree change set vs ``base``."""
    try:
        tracked = subprocess.run(
            ["git", "diff", "--name-only", "-z", base, "--"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        ).stdout.split("\0")
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        ).stdout.split("\0")
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"cannot compute local change set (base {base}): {exc}") from exc
    paths = sorted({path for path in (*tracked, *untracked) if path})
    nod = {path for path in tracked if path and not (root / path).is_file()}
    nod |= {path for path in untracked if path}
    return paths, nod


def _fallback_full(reason: str) -> ScopeDecision:
    return ScopeDecision(
        profile="full",
        packages=PACKAGES,
        docs_only=False,
        testmon=False,
        check_js_modules=False,
        run_tier2=True,
        perf_equivalence=True,
        reasons=(reason,),
        skipped=(),
    )


def decide_local(base: str = "HEAD", root: Path = ROOT) -> ScopeDecision:
    """Route the working-tree change set (tracked diff vs ``base`` + untracked files)."""
    try:
        paths, nod = local_change_set(base, root)
    except RuntimeError as exc:
        return _fallback_full(f"{exc}; resolving to full")
    return decide_paths(paths, new_or_deleted=nod)


def decide_ci(
    event_name: str,
    base_sha: str = "",
    head_sha: str = "HEAD",
    root: Path = ROOT,
) -> ScopeDecision:
    """Route a CI change set. Any error resolves to full."""
    reason = conservative_ci_reason(event_name, base_sha)
    if reason:
        return _fallback_full(reason)
    changed = git_diff_names(base_sha, head_sha, root)
    if changed is None:
        return _fallback_full(f"git diff {base_sha}..{head_sha} unavailable; resolving to full")
    nod = git_diff_new_or_deleted(base_sha, head_sha, root)
    return decide_paths(changed, new_or_deleted=nod)


def render_report(decision: ScopeDecision) -> str:
    lines = [
        f"scope: {decision.profile}",
        f"packages: {decision.legs_label()}",
        f"docs_only: {'true' if decision.docs_only else 'false'}",
        f"testmon: {'admissible (advisory)' if decision.testmon else 'disabled'}",
        f"run_tier2: {'true' if decision.run_tier2 else 'false'}",
        f"perf_equivalence: {'true' if decision.perf_equivalence else 'false (skipped)'}",
        f"check_js_modules: {'true' if decision.check_js_modules else 'false'}",
    ]
    lines.extend(f"reason: {reason}" for reason in decision.reasons)
    if decision.skipped:
        lines.append(f"skipped: {'; '.join(decision.skipped)}")
    return "\n".join(lines)


def write_github_output(path: Path, decision: ScopeDecision) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for key, value in decision.to_outputs().items():
            handle.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="HEAD", help="local base revision (default: HEAD)")
    parser.add_argument("--event", default=None, help="CI event name (enables CI mode)")
    parser.add_argument("--head", default="HEAD", help="CI head revision (default: HEAD)")
    parser.add_argument("--github-output", type=Path, default=None, help="$GITHUB_OUTPUT file")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    try:
        decision = (
            decide_ci(args.event, args.base, args.head)
            if args.event is not None
            else decide_local(args.base)
        )
    except Exception as exc:  # noqa: BLE001 — CI contract: any router error resolves to full
        decision = _fallback_full(f"router error: {exc}; resolving to full")

    if args.format == "json":
        print(json.dumps(decision.to_dict(), sort_keys=True))
    else:
        print(render_report(decision))
    if args.github_output is not None:
        try:
            write_github_output(args.github_output, decision)
        except OSError as exc:
            print(f"change_scope: cannot write --github-output: {exc}", file=sys.stderr)
            return 64
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
