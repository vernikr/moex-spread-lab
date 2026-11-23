#!/usr/bin/env python3
"""S3 shadow verification runner (plan §4 S3).

Runs --profile affected alongside --profile full on the same tree and records
"missed failures" (full reports failed/timeout/unavailable where affected passed).

Modes: --self | --commit RANGE | --inject SLUG | --injections | --list-injections
Log: reports/perf/shadow.jsonl (gitignored). Acceptance: >=30 candidates + 4
injections, zero missed failures.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHADOW_LOG = ROOT / "reports/perf/shadow.jsonl"
WORKTREES_DIR = ROOT / ".shadow-worktrees"
sys.path.insert(0, str(ROOT))
from scripts import shadow_regressions  # noqa: E402


@dataclass
class ProfileResult:
    outcome: str
    wall_s: float
    label: str
    per_step: dict[str, str] = field(default_factory=dict)
    returncode: int = 0

    @property
    def ok(self) -> bool:
        return self.outcome == "PASSED"


@dataclass
class ShadowResult:
    subject: str
    subject_kind: str
    decision: dict
    affected: ProfileResult
    full: ProfileResult
    missed_failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    started: str = ""
    wall_s: float = 0.0


def _venv_python(worktree: Path) -> list[str]:
    vp = worktree / ".venv/bin/python"
    return [str(vp)] if vp.is_file() else [sys.executable]


def _parse_verify_output(output: str, rc: int) -> tuple[str, str, dict[str, str]]:
    per_step: dict[str, str] = {}
    label, outcome = "", "PASSED" if rc == 0 else "FAILED"
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("verify ("):
            try:
                rest = s[len("verify (") :]
                label, _, rest2 = rest.partition(")")
                outcome = rest2.split(":")[1].strip().split()[0]
            except (IndexError, ValueError):
                pass
        else:
            parts = s.split()
            if len(parts) >= 2 and parts[0] in (
                "passed",
                "failed",
                "timeout",
                "unavailable",
                "not run",
            ):
                per_step[parts[1]] = parts[0]
    if rc in (124, 137, 143):
        outcome = "TIMEOUT"
    elif rc == 127:
        outcome = "UNAVAILABLE"
    return outcome, label, per_step


def run_profile(
    worktree: Path, profile: str, base: str = "HEAD", timeout: int = 600
) -> ProfileResult:
    cmd = [*_venv_python(worktree), "scripts/verify.py", "--profile", profile, "--base", base]
    env = {
        **os.environ,
        "MOEX_OFFLINE_ONLY": "1",
        "UV_NO_SYNC": "1",
        "TESTMON_DATAFILE": str(worktree / ".testmondata"),
    }
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=worktree, env=env, check=False, capture_output=True, text=True, timeout=timeout
        )
        output, rc = proc.stdout + "\n" + proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        output = (
            exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""
        ) + "\n[timeout]"
        rc = 124
    outcome, label, per_step = _parse_verify_output(output, rc)
    return ProfileResult(outcome, time.monotonic() - t0, label, per_step, rc)


def compare_results(aff: ProfileResult, full: ProfileResult) -> list[str]:
    missed = []
    if aff.ok and not full.ok:
        missed.append(f"<overall>: aff={aff.outcome} full={full.outcome}")
    for k, fstatus in full.per_step.items():
        if fstatus in ("passed", "not run"):
            continue
        if aff.per_step.get(k, "not run") == "passed":
            missed.append(f"{k}: aff=passed full={fstatus}")
    return missed


def get_router_decision(worktree: Path, base: str = "HEAD") -> dict:
    try:
        p = subprocess.run(
            [sys.executable, "scripts/change_scope.py", "--base", base, "--format", "json"],
            cwd=worktree,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if p.returncode == 0:
            return json.loads(p.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return {"profile": "unknown", "error": "router decision unavailable"}


def shadow_one(
    worktree: Path,
    *,
    subject: str,
    kind: str,
    base: str = "HEAD",
    timeout: int = 600,
    notes: list[str] | None = None,
) -> ShadowResult:
    started = datetime.now(UTC).isoformat(timespec="seconds")
    t0 = time.monotonic()
    dec = get_router_decision(worktree, base)
    aff = run_profile(worktree, "affected", base=base, timeout=timeout)
    full = run_profile(worktree, "full", base=base, timeout=timeout)
    return ShadowResult(
        subject,
        kind,
        dec,
        aff,
        full,
        compare_results(aff, full),
        list(notes or []),
        started,
        time.monotonic() - t0,
    )


def create_worktree(name: str, sha: str) -> Path:
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    wt = WORKTREES_DIR / name
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(wt), sha],
        cwd=ROOT,
        check=True,
        capture_output=True,
        timeout=120,
    )
    return wt


def cleanup_worktree(wt: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        timeout=60,
    )


def _print_result(r: ShadowResult, i: int | None = None, n: int | None = None):
    prefix = f"[{i}/{n}] " if i is not None else ""
    tag = "MISS!" if r.missed_failures else "ok"
    print(
        f"  {prefix}aff={r.affected.outcome} full={r.full.outcome} {tag} ({r.wall_s:.1f}s)",
        flush=True,
    )


def shadow_self(timeout: int = 600) -> ShadowResult:
    r = shadow_one(ROOT, subject="worktree", kind="worktree", timeout=timeout)
    _print_result(r)
    return r


def shadow_commits(rev_range: str, timeout: int = 600) -> list[ShadowResult]:
    p = subprocess.run(
        ["git", "rev-list", "--reverse", rev_range],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    shas = [line.strip() for line in p.stdout.splitlines() if line.strip()]
    results = []
    for i, sha in enumerate(shas, 1):
        short = sha[:12]
        print(f"shadow: [{i}/{len(shas)}] {short}", flush=True)
        wt = create_worktree(f"cmt-{short}", sha)
        try:
            r = shadow_one(
                wt,
                subject=sha,
                kind="commit",
                base="HEAD",
                timeout=timeout,
                notes=[f"from {rev_range}"],
            )
            results.append(r)
            _print_result(r)
        finally:
            cleanup_worktree(wt)
    return results


def shadow_inject(slug: str, base: str = "HEAD", timeout: int = 600) -> ShadowResult:
    wt = create_worktree(f"inj-{slug}", base)
    try:
        desc = shadow_regressions.apply_injection(wt, slug)
        print(f"shadow: inject {slug} -> {desc}", flush=True)
        r = shadow_one(
            wt,
            subject=f"injection:{slug}",
            kind="injection",
            base="HEAD",
            timeout=timeout,
            notes=[f"{slug}: {desc}"],
        )
        _print_result(r)
        return r
    finally:
        cleanup_worktree(wt)


def shadow_all_injections(base: str = "HEAD", timeout: int = 600) -> list[ShadowResult]:
    return [
        shadow_inject(i.slug, base=base, timeout=timeout) for i in shadow_regressions.REGRESSIONS
    ]


def append_log(r: ShadowResult) -> None:
    SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "schema": "msl.shadow-record/1",
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "subject": r.subject,
        "subject_kind": r.subject_kind,
        "decision": r.decision,
        "affected": {
            "outcome": r.affected.outcome,
            "wall_s": round(r.affected.wall_s, 2),
            "label": r.affected.label,
            "per_step": r.affected.per_step,
        },
        "full": {
            "outcome": r.full.outcome,
            "wall_s": round(r.full.wall_s, 2),
            "label": r.full.label,
            "per_step": r.full.per_step,
        },
        "missed_failures": r.missed_failures,
        "notes": r.notes,
        "wall_s": round(r.wall_s, 2),
        "started": r.started,
    }
    with SHADOW_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def print_summary(results: list[ShadowResult]) -> None:
    n = len(results)
    misses = sum(1 for r in results if r.missed_failures)
    aff_ok = sum(1 for r in results if r.affected.ok)
    full_ok = sum(1 for r in results if r.full.ok)
    docs = sum(1 for r in results if r.decision.get("profile") == "docs")
    pkgs = sum(1 for r in results if r.decision.get("profile") == "packages")
    full_p = sum(1 for r in results if r.decision.get("profile") == "full")
    print(f"\n{'=' * 64}")
    print(f"shadow: {n} candidates (docs={docs} packages={pkgs} full={full_p})")
    print(f"  affected passed: {aff_ok}/{n}")
    print(f"  full passed:     {full_ok}/{n}")
    print(f"  MISSED FAILURES: {misses}/{n}")
    for r in results:
        if r.missed_failures:
            print(f"  {r.subject} ({r.subject_kind}): {r.missed_failures}")
    print(f"{'=' * 64}")
    print(f"log: {SHADOW_LOG.relative_to(ROOT)}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self", action="store_true")
    mode.add_argument("--commit", metavar="RANGE")
    mode.add_argument("--inject", metavar="SLUG")
    mode.add_argument("--injections", action="store_true")
    mode.add_argument("--list-injections", action="store_true")
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--no-log", action="store_true")
    args = parser.parse_args(argv)

    if args.list_injections:
        for s, c, d in shadow_regressions.list_injections():
            print(f"  {s:20s} [{c:10s}] {d}")
        return 0

    if args.self:
        results = [shadow_self(args.timeout)]
    elif args.commit:
        results = shadow_commits(args.commit, args.timeout)
    elif args.inject:
        results = [shadow_inject(args.inject, args.base, args.timeout)]
    else:
        results = shadow_all_injections(args.base, args.timeout)

    if not args.no_log:
        for r in results:
            append_log(r)
    print_summary(results)
    return 1 if any(r.missed_failures for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
