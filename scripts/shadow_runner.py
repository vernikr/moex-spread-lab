#!/usr/bin/env python3
"""S3 shadow verification runner (plan §4 S3).

Runs --profile affected alongside --profile full and records missed failures.

Modes: --self | --commit RANGE | --inject SLUG | --injections | --scenarios
       --list-injections | --list-scenarios
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import shadow_core, shadow_regressions  # noqa: E402
from scripts import shadow_scenarios as scen_lib  # noqa: E402

# Re-exports for backward compatibility with existing tests/imports.
ProfileResult = shadow_core.ProfileResult
ShadowResult = shadow_core.ShadowResult
SHADOW_LOG = shadow_core.SHADOW_LOG
_parse_verify_output = shadow_core._parse_verify_output
compare_results = shadow_core.compare_results
append_log = shadow_core.append_log
print_summary = shadow_core.print_summary


def _print(r: shadow_core.ShadowResult, idx: int | None = None, n: int | None = None):
    prefix = f"[{idx}/{n}] " if idx is not None else ""
    issues = len(r.problems)
    tag = "MISS!" if issues else "ok"
    extra = f" profile={r.decision.get('profile', '?')}"
    if issues:
        extra += f" issues={issues}"
    print(
        f"  {prefix}aff={r.affected.outcome} full={r.full.outcome} {tag} ({r.wall_s:.1f}s){extra}",
        flush=True,
    )
    for msg in r.problems[:3]:
        print(f"       !! {msg}", flush=True)


def shadow_self(timeout: int) -> shadow_core.ShadowResult:
    r = shadow_core.shadow_one(ROOT, subject="worktree", kind="worktree", timeout=timeout)
    _print(r)
    return r


def shadow_commits(rev_range: str, timeout: int) -> list[shadow_core.ShadowResult]:
    import subprocess

    proc = subprocess.run(
        ["git", "rev-list", "--reverse", rev_range],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    shas = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    results = []
    for i, sha in enumerate(shas, 1):
        short = sha[:12]
        print(f"shadow: [{i}/{len(shas)}] {short}", flush=True)
        wt = shadow_core.create_worktree(f"cmt-{short}", sha)
        try:
            r = shadow_core.shadow_one(
                wt,
                subject=sha,
                kind="commit",
                base=f"{sha}~1",
                timeout=timeout,
                notes=[f"from {rev_range}"],
            )
            results.append(r)
            _print(r, i, len(shas))
        finally:
            shadow_core.cleanup_worktree(wt)
    return results


def shadow_inject(slug: str, base: str, timeout: int) -> shadow_core.ShadowResult:
    wt = shadow_core.create_worktree(f"inj-{slug}", base)
    try:
        desc = shadow_regressions.apply_injection(wt, slug)
        print(f"shadow: inject {slug} -> {desc}", flush=True)
        r = shadow_core.shadow_one(
            wt,
            subject=f"injection:{slug}",
            kind="injection",
            base="HEAD",
            timeout=timeout,
            notes=[f"{slug}: {desc}"],
        )
        _print(r)
        return r
    finally:
        shadow_core.cleanup_worktree(wt)


def shadow_all_injections(base: str, timeout: int) -> list[shadow_core.ShadowResult]:
    return [shadow_inject(i.slug, base, timeout) for i in shadow_regressions.REGRESSIONS]


def run_scenarios(timeout: int) -> list[shadow_core.ShadowResult]:
    wt = shadow_core.create_worktree("scenarios-HEAD", "HEAD")
    results: list[shadow_core.ShadowResult] = []
    scens = scen_lib.SCENARIOS
    try:
        for i, scen in enumerate(scens, 1):
            print(f"shadow: [{i}/{len(scens)}] {scen.name} ({scen.expected_profile})", flush=True)
            scen_lib.reset_worktree(wt)
            scen.apply(wt)
            r = shadow_core.shadow_one(
                wt,
                subject=f"scenario:{scen.name}",
                kind="scenario",
                base="HEAD",
                timeout=timeout,
                notes=[scen.description],
                expected_profile=scen.expected_profile,
                expected_packages=scen.expected_packages,
                expected_testmon=scen.expected_testmon,
            )
            results.append(r)
            _print(r, i, len(scens))
    finally:
        scen_lib.reset_worktree(wt)
        shadow_core.cleanup_worktree(wt)
    return results


def _run_modes(args) -> list[shadow_core.ShadowResult]:
    results: list[shadow_core.ShadowResult] = []
    if args.self:
        results.append(shadow_self(args.timeout))
    if args.commit:
        results.extend(shadow_commits(args.commit, args.timeout))
    if args.inject:
        results.append(shadow_inject(args.inject, args.base, args.timeout))
    if args.injections:
        results.extend(shadow_all_injections(args.base, args.timeout))
    if args.scenarios:
        results.extend(run_scenarios(args.timeout))
    return results


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self", action="store_true")
    p.add_argument("--commit", metavar="RANGE")
    p.add_argument("--inject", metavar="SLUG")
    p.add_argument("--injections", action="store_true")
    p.add_argument("--scenarios", action="store_true")
    p.add_argument("--list-injections", action="store_true")
    p.add_argument("--list-scenarios", action="store_true")
    p.add_argument("--base", default="HEAD")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--no-log", action="store_true")
    args = p.parse_args(argv)

    if args.list_injections:
        for s, c, d in shadow_regressions.list_injections():
            print(f"  {s:20s} [{c:10s}] {d}")
        return 0
    if args.list_scenarios:
        for n, pr, d in scen_lib.list_scenarios():
            print(f"  {n:30s} [{pr:8s}] {d}")
        return 0
    has_run = any([args.self, args.commit, args.inject, args.injections, args.scenarios])
    if not has_run:
        p.error("at least one mode is required")

    results = _run_modes(args)
    if not args.no_log:
        for r in results:
            shadow_core.append_log(r)
    shadow_core.print_summary(results)
    return 1 if any(r.problems for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
