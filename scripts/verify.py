#!/usr/bin/env python3
"""Documented launcher for agent task verification (plans S2 item 1, S4a item 1).

Profiles: ``quick`` (``check_health.sh --changed``), ``affected`` (docs-only
scan or router-scoped legs with advisory testmon and the always-run set; full
fallback when no path matches), and ``full``/``pre-push`` (full health gate and
whole suite, including every parity tier). stdlib-only launcher; it does not
replace the health ratchet. Steps run through ``scripts/verify_record.py``.
Browser smokes are run separately by the scheduled ``scripts/run_browser_smokes.py``
job and are reported as not run here. pytest uses ``-n <cpu_count> --dist loadfile
-q --tb=short`` with ``MOEX_OFFLINE_ONLY=1``; ``TESTMON_DATAFILE`` defaults to
``<repo>/.testmondata`` (gitignored).
``TESTMON_DATAFILE`` defaults to ``<repo>/.testmondata`` (gitignored).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import change_scope  # noqa: E402  (path inserted above)
from scripts.verify_workers import worker_count  # noqa: E402

EQUIVALENCE_TIERS = (
    "packages/proof/tests/test_perf_equivalence.py",
    "packages/proof/tests/test_perf_equivalence_full.py",
    "packages/proof/tests/test_perf_equivalence_pair.py",
    "packages/proof/tests/test_perf_equivalence_pool.py",
    "packages/proof/tests/test_perf_equivalence_stream.py",
)
FASTCORE_PARITY = "packages/proof/tests/test_fastcore_parity.py"
PARITY_TRIGGER_PACKAGES = frozenset({"core", "proof", "accounting"})
PROFILES = ("quick", "affected", "full", "pre-push")
TIMEOUT_CODES = frozenset({124, 137, 143})
BROWSER_SKIPPED = "browser smokes (separate scheduled job; not run by verify profiles)"
ALWAYS_RUN_RE = re.compile(
    r"(subprocess|proof_pool|moex_spread_lab[._]cli|cli\.main|moex-spread-lab)"
)


def python_cmd() -> list[str]:
    venv_python = ROOT / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return [str(venv_python)]
    return ["uv", "run", "--no-sync", "python"]


HEALTH_CMD = ["bash", "scripts/check_health.sh"]
STEP_META = {
    "docs-scan": ("docs-check", "docs-scan"),
    "health-changed": ("changed-gate", "check_health.sh"),
    "health-full": ("full-gate", "check_health.sh"),
    "architecture": ("architecture-gate", "architecture_gate.py"),
    "always-run": ("always-run-suite", "pytest"),
    "pytest-affected": ("affected-suite", "pytest"),
    "suite-full": ("full-suite", "pytest"),
}


@dataclass(frozen=True)
class Step:
    key: str
    task_class: str
    check: str
    reason: str
    command: list[str]


def step(key: str, reason: str, command: list[str]) -> Step:
    task_class, check = STEP_META[key]
    return Step(key, task_class, check, reason, command)


@dataclass
class StepResult:
    key: str
    status: str
    secs: float


def _status_of(returncode: int) -> str:
    """Exit code -> reported status (124/137/143 are the runner's timeout codes)."""
    if returncode in TIMEOUT_CODES:
        return "timeout"
    return {0: "passed", 127: "unavailable"}.get(returncode, "failed")


def run_step(*, step: Step, records: str | None, dry_run: bool) -> StepResult:
    if dry_run:
        print(f"dry-run [{step.key}]: {' '.join(step.command)}")
        return StepResult(step.key, "not run", 0.0)
    started = time.monotonic()
    env = os.environ.copy()
    if records:
        env["MSL_VERIFICATION_RECORDS"] = records
    label = f"{step.check}:{step.reason}"
    cmd = [
        *python_cmd(),
        str(ROOT / "scripts" / "verify_record.py"),
        "--task-class",
        step.task_class,
        "--phase",
        "S2",
        "--check",
        label,
        *(["--records", records] if records else []),
        "--",
        *step.command,
    ]
    try:
        rc = subprocess.run(cmd, cwd=ROOT, env=env, check=False).returncode
    except OSError as exc:
        print(f"verify: cannot launch {step.check}: {exc}", file=sys.stderr)
        rc = 127
    return StepResult(step.key, _status_of(rc), time.monotonic() - started)


def pytest_cmd(extra: list[str], marker: str = "not slow_equiv") -> list[str]:
    """pytest command with an explicit marker selection.

    ``pyproject.toml`` deselects ``slow_equiv`` (plan S4a), so ``marker=""`` must reach
    pytest as ``-m ""``; omitting the flag lets ``addopts`` drop parity tests silently.
    """
    flags = ["-m", "pytest", "-m", marker, "-n", str(worker_count()), "--dist", "loadfile"]
    return [*python_cmd(), *flags, "-q", "--tb=short", *extra]


def always_run_files(legs: list[str]) -> list[str]:
    """Test modules spawning subprocesses / the CLI / proof_pool (testmon-unsafe)."""
    files: list[str] = []
    for leg in legs:
        tests_dir = ROOT / "packages" / leg / "tests"
        for file in sorted(tests_dir.rglob("test_*.py")) if tests_dir.is_dir() else ():
            if "browser" in file.relative_to(tests_dir).parts:
                continue
            text = file.read_text(encoding="utf-8", errors="ignore")
            if ALWAYS_RUN_RE.search(text):
                files.append(str(file.relative_to(ROOT)))
    return files


def plan_affected(base: str) -> tuple[change_scope.ScopeDecision, list[str]]:
    try:
        paths, nod = change_scope.local_change_set(base)
    except RuntimeError as exc:
        print(f"verify: {exc}; falling back to full profile")
        paths, nod = ["__router_error__"], set()
    return change_scope.decide_paths(paths, new_or_deleted=nod), paths


def _testmon_available() -> bool:
    try:
        probe = subprocess.run(
            [*python_cmd(), "-c", "import testmon"], cwd=ROOT, capture_output=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def _affected_steps(
    base: str,
) -> tuple[list[Step], list[str], str]:
    decision, paths = plan_affected(base)
    touched = change_scope.touched_packages(paths)
    legs = list(decision.packages)
    os.environ["TESTMON_DATAFILE"] = os.environ.get("TESTMON_DATAFILE", str(ROOT / ".testmondata"))
    skipped = [BROWSER_SKIPPED]

    if decision.profile == "docs":
        docs_changed = [p for p in paths if change_scope.is_docs_path(p)]
        steps = [
            step(
                "docs-scan",
                (
                    f"docs-only change ({len(docs_changed)} file(s)): whitespace, "
                    "conflict markers, credential tokens"
                ),
                [*python_cmd(), "scripts/docs_scan.py", *docs_changed],
            )
        ]
        return (
            steps,
            [
                *skipped,
                "check_health.sh (docs-only)",
                "pytest (docs-only)",
                "architecture gate (docs-only)",
            ],
            "affected/docs",
        )

    if decision.profile == "full":
        print(
            f"verify: no scoped path applies — falling back to full ({'; '.join(decision.reasons)})"
        )
        return build_steps("full", base)

    always = always_run_files(legs)
    parity_triggered = bool(PARITY_TRIGGER_PACKAGES.intersection(touched))
    parity = [rel for rel in (*EQUIVALENCE_TIERS, FASTCORE_PARITY) if (ROOT / rel).is_file()]
    triggered_parity: list[str] = parity if parity_triggered else []
    always_files: list[str] = list(dict.fromkeys([*always, *triggered_parity]))
    browser_dirs = [
        f"packages/{leg}/tests/browser"
        for leg in legs
        if (ROOT / "packages" / leg / "tests" / "browser").is_dir()
    ]
    ignore_args: list[str] = [
        f"--ignore={rel}" for rel in dict.fromkeys([*always, *parity, *browser_dirs])
    ]

    steps = [
        step(
            "health-changed",
            f"affected profile (--changed --base {base})",
            [*HEALTH_CMD, "--changed", "--base", base],
        ),
        step(
            "architecture",
            "affected profile always runs the architecture gate",
            [*python_cmd(), "scripts/architecture_gate.py"],
        ),
    ]
    if always_files:
        trigger = (
            "perf-equivalence/fastcore-parity triggered by "
            + ",".join(sorted(PARITY_TRIGGER_PACKAGES.intersection(touched)))
            + "; "
            if parity_triggered
            else ""
        )
        steps.append(
            step(
                "always-run",
                f"subprocess/CLI/proof_pool and {trigger}{len(always_files)} file(s), testmon disabled",
                pytest_cmd(["-p", "no:testmon", *always_files]),
            )
        )
    leg_dirs = [f"packages/{leg}/tests" for leg in legs]
    if decision.testmon and _testmon_available():
        steps.append(
            step(
                "pytest-affected",
                f"affected (advisory) selection over legs: {','.join(legs)}",
                pytest_cmd(["--testmon", *leg_dirs, *ignore_args]),
            )
        )
    else:
        if decision.testmon:
            print("verify: pytest-testmon not importable; legs run WITHOUT deselection")
        else:
            print(f"verify: testmon disabled ({'; '.join(decision.reasons)}); legs run in full")
        steps.append(
            step(
                "pytest-affected",
                f"full legs (no deselection): {','.join(legs)}",
                pytest_cmd(leg_dirs, marker=""),
            )
        )
    skipped += ["vulture (full gate only)", "full suite (use --profile full)", *decision.skipped]
    return steps, skipped, "affected"


def build_steps(profile: str, base: str) -> tuple[list[Step], list[str], str]:
    """Return ``(steps, skipped, label)`` for a profile (no execution)."""
    if profile == "quick":
        return (
            [
                step(
                    "health-changed",
                    f"quick profile (--changed --base {base})",
                    [*HEALTH_CMD, "--changed", "--base", base],
                )
            ],
            [
                BROWSER_SKIPPED,
                "pytest (quick profile)",
                "vulture (full gate only)",
            ],
            "quick",
        )
    if profile in ("pre-push", "full"):
        extra = (
            ["testmon deselection (never on the pre-push path)"] if profile == "pre-push" else []
        )
        return (
            [
                step("health-full", f"{profile} profile (full gate)", list(HEALTH_CMD)),
                step(
                    "suite-full",
                    f"{profile}: whole suite, every parity tier (no deselection)",
                    pytest_cmd([], marker=""),
                ),
            ],
            [BROWSER_SKIPPED, *extra],
            profile,
        )
    return _affected_steps(base)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--base", default="HEAD", help="base revision for the change set")
    parser.add_argument("--records", default=None, help="JSONL path for measurement records")
    parser.add_argument("--dry-run", action="store_true", help="print steps, run nothing")
    args = parser.parse_args(argv)

    if args.records:
        os.environ["MSL_VERIFICATION_RECORDS"] = args.records
    started = time.monotonic()
    steps, skipped, label = build_steps(args.profile, args.base)
    results = [run_step(step=step, records=args.records, dry_run=args.dry_run) for step in steps]
    wall = time.monotonic() - started

    statuses = {result.status for result in results}
    if statuses & {"failed", "timeout"}:
        overall, rc = "FAILED", 1
    elif "unavailable" in statuses:
        overall, rc = "UNAVAILABLE", 2
    else:
        overall, rc = "PASSED", 0

    advisory = label.startswith("affected")
    print()
    print(f"verify ({label}): {overall} in {wall:.1f}s")
    if advisory:
        print("note: affected is advisory, NOT full verification")
    for result in results:
        print(f"  {result.status:<12} {result.key:<16} {result.secs:.1f}s")
    if not args.dry_run and skipped:
        print(f"skipped: {'; '.join(dict.fromkeys(skipped))}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
