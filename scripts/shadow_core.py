#!/usr/bin/env python3
"""Core types and helpers for S3 shadow verification."""

from __future__ import annotations

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
ALL_SIX = ("core", "data", "accounting", "strategies", "proof", "web")
MAIN_VENV_PYTHON = ROOT / ".venv/bin/python"


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
    expected_profile: str = ""
    expected_packages: tuple[str, ...] = ()
    expected_testmon: bool | None = None
    routing_mismatch: list[str] = field(default_factory=list)
    affected: ProfileResult = field(default_factory=lambda: ProfileResult("UNAVAILABLE", 0.0, ""))
    full: ProfileResult = field(default_factory=lambda: ProfileResult("UNAVAILABLE", 0.0, ""))
    missed_failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    started: str = ""
    wall_s: float = 0.0

    @property
    def problems(self) -> list[str]:
        return self.missed_failures + self.routing_mismatch


def _venv_python(worktree: Path) -> list[str]:
    vp = worktree / ".venv/bin/python"
    if vp.is_file():
        return [str(vp)]
    if MAIN_VENV_PYTHON.is_file():
        return [str(MAIN_VENV_PYTHON)]
    return [sys.executable]


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
    for key, fstatus in full.per_step.items():
        if fstatus in ("passed", "not run"):
            continue
        if aff.per_step.get(key, "not run") == "passed":
            missed.append(f"{key}: aff=passed full={fstatus}")
    return missed


def _check_routing(
    decision: dict,
    expected_profile: str,
    expected_packages: tuple[str, ...],
    expected_testmon: bool | None,
) -> list[str]:
    mismatches: list[str] = []
    actual_profile = decision.get("profile", "?")
    if actual_profile != expected_profile:
        mismatches.append(f"router: expected profile={expected_profile}, got {actual_profile}")
    if expected_packages:
        actual_pkgs = tuple(sorted(decision.get("packages_list", [])))
        expected_sorted = tuple(sorted(expected_packages))
        if actual_pkgs != expected_sorted:
            mismatches.append(
                f"router: expected packages={list(expected_sorted)}, got {list(actual_pkgs)}"
            )
    if expected_testmon is not None:
        actual_tm = bool(decision.get("testmon"))
        if actual_tm != expected_testmon:
            mismatches.append(f"router: expected testmon={expected_testmon}, got {actual_tm}")
    return mismatches


def get_router_decision(worktree: Path, base: str = "HEAD") -> dict:
    try:
        vp = _venv_python(worktree)[0]
        proc = subprocess.run(
            [vp, "scripts/change_scope.py", "--base", base, "--format", "json"],
            cwd=worktree,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode == 0:
            return json.loads(proc.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return {"profile": "unknown", "error": "router decision unavailable"}


def create_worktree(name: str, sha: str = "HEAD") -> Path:
    WORKTREES_DIR = ROOT / ".shadow-worktrees"
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
    # Symlink .venv so verify.py (which resolves .venv/bin/python relative to
    # its own ROOT) finds the main repo's venv. The symlink is matched by
    # .git/info/exclude so it doesn't show up as untracked to the router.
    if MAIN_VENV_PYTHON.is_file():
        wt_venv = wt / ".venv"
        if not wt_venv.exists():
            wt_venv.symlink_to(ROOT / ".venv")
    # Copy primed .testmondata so --testmon can deselect in affected profile.
    main_tm = ROOT / ".testmondata"
    if main_tm.is_file():
        shutil.copy2(main_tm, wt / ".testmondata")
    return wt


def cleanup_worktree(wt: Path) -> None:
    wt_venv = wt / ".venv"
    if wt_venv.is_symlink():
        wt_venv.unlink()
    elif wt_venv.is_dir():
        shutil.rmtree(wt_venv, ignore_errors=True)
    wt_tm = wt / ".testmondata"
    if wt_tm.exists():
        wt_tm.unlink()
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        timeout=60,
    )


def shadow_one(  # noqa: PLR0913
    worktree: Path,
    *,
    subject: str,
    kind: str,
    base: str = "HEAD",
    timeout: int = 600,
    notes: list[str] | None = None,
    expected_profile: str = "",
    expected_packages: tuple[str, ...] = (),
    expected_testmon: bool | None = None,
) -> ShadowResult:
    started = datetime.now(UTC).isoformat(timespec="seconds")
    t0 = time.monotonic()
    dec = get_router_decision(worktree, base)
    aff = run_profile(worktree, "affected", base=base, timeout=timeout)
    full = run_profile(worktree, "full", base=base, timeout=timeout)
    routing_mm = (
        _check_routing(dec, expected_profile, expected_packages, expected_testmon)
        if expected_profile
        else []
    )
    return ShadowResult(
        subject,
        kind,
        dec,
        expected_profile,
        expected_packages,
        expected_testmon,
        routing_mm,
        aff,
        full,
        compare_results(aff, full),
        list(notes or []),
        started,
        time.monotonic() - t0,
    )


def append_log(result: ShadowResult) -> None:
    SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "schema": "msl.shadow-record/1",
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "subject": result.subject,
        "subject_kind": result.subject_kind,
        "decision": result.decision,
        "expected_profile": result.expected_profile,
        "expected_packages": list(result.expected_packages),
        "expected_testmon": result.expected_testmon,
        "affected": {
            "outcome": result.affected.outcome,
            "wall_s": round(result.affected.wall_s, 2),
            "label": result.affected.label,
            "per_step": result.affected.per_step,
        },
        "full": {
            "outcome": result.full.outcome,
            "wall_s": round(result.full.wall_s, 2),
            "label": result.full.label,
            "per_step": result.full.per_step,
        },
        "missed_failures": result.missed_failures,
        "routing_mismatches": result.routing_mismatch,
        "problems": result.problems,
        "notes": result.notes,
        "wall_s": round(result.wall_s, 2),
        "started": result.started,
    }
    with SHADOW_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def print_summary(results: list[ShadowResult]) -> None:
    n = len(results)
    problems = sum(1 for r in results if r.problems)
    misses = sum(1 for r in results if r.missed_failures)
    routing = sum(1 for r in results if r.routing_mismatch)
    aff_ok = sum(1 for r in results if r.affected.ok)
    full_ok = sum(1 for r in results if r.full.ok)
    by_profile: dict[str, int] = {}
    for res in results:
        key = res.decision.get("profile", "?")
        by_profile[key] = by_profile.get(key, 0) + 1
    print(f"\n{'=' * 64}")
    print(f"shadow: {n} candidates (profile breakdown: {dict(by_profile)})")
    print(f"  affected passed: {aff_ok}/{n}")
    print(f"  full passed:     {full_ok}/{n}")
    print(f"  ROUTING MISMATCHES: {routing}/{n}")
    print(f"  MISSED FAILURES: {misses}/{n}")
    print(f"  TOTAL PROBLEMS: {problems}/{n}")
    for res in results:
        if res.problems:
            print(f"  {res.subject} ({res.subject_kind}):")
            for msg in res.problems:
                print(f"      {msg}")
    print(f"{'=' * 64}")
    print(f"log: {SHADOW_LOG.relative_to(ROOT)}")
