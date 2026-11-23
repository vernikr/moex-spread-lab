#!/usr/bin/env python3
"""Documented launcher for agent task verification (plan S2, work item 1).

Profiles: ``quick`` (``check_health.sh --changed``), ``affected`` (docs-scan for
docs-only; router-scoped package legs under testmon as an *advisory* signal +
the always-run set otherwise, full fallback on unmatched paths), ``full``
(unchanged full gate + full suite). stdlib only; does not replace the health
ratchet. Steps run through ``scripts/verify_record.py`` (plan S0 record).
Browser smokes are out of scope (plan S4b) and reported as ``not run``.
pytest runs ``-n <cpu_count> --dist loadfile -q --tb=short`` with ``MOEX_OFFLINE_ONLY=1``;
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

PERF_EQUIVALENCE = "packages/proof/tests/test_perf_equivalence.py"
FASTCORE_PARITY = "packages/proof/tests/test_fastcore_parity.py"
PARITY_TRIGGER_PACKAGES = frozenset({"core", "proof", "accounting"})
ALWAYS_RUN_RE = re.compile(
    r"(subprocess|proof_pool|moex_spread_lab[._]cli|cli\.main|moex-spread-lab)"
)


def python_cmd() -> list[str]:
    venv_python = ROOT / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return [str(venv_python)]
    return ["uv", "run", "--no-sync", "python"]


def worker_count() -> int:
    override = os.environ.get("MSL_VERIFY_WORKERS")
    if override and override.isdigit() and int(override) > 0:
        return int(override)
    return os.cpu_count() or 1


@dataclass(frozen=True)
class Step:
    key: str
    task_class: str
    check: str
    reason: str
    command: list[str]


@dataclass
class StepResult:
    key: str
    status: str
    secs: float


def _status_of(returncode: int) -> str:
    if returncode == 0:
        return "passed"
    if returncode in (124, 137, 143):
        return "timeout"
    if returncode == 127:
        return "unavailable"
    return "failed"


def run_step(*, step: Step, records: str | None, dry_run: bool) -> StepResult:
    if dry_run:
        print(f"dry-run [{step.key}]: {' '.join(step.command)}")
        return StepResult(step.key, "not run", 0.0)
    started = time.monotonic()
    env = os.environ.copy()
    if records:
        env["MSL_VERIFICATION_RECORDS"] = records
    cmd = [
        *python_cmd(),
        str(ROOT / "scripts" / "verify_record.py"),
        "--task-class",
        step.task_class,
        "--phase",
        "S2",
        "--check",
        f"{step.check}:{step.reason}",
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


def pytest_cmd(extra: list[str]) -> list[str]:
    return [
        *python_cmd(),
        "-m",
        "pytest",
        "-n",
        str(worker_count()),
        "--dist",
        "loadfile",
        "-q",
        "--tb=short",
        *extra,
    ]


def always_run_files(legs: list[str]) -> list[str]:
    """Test modules spawning subprocesses / the CLI / proof_pool (testmon-unsafe)."""
    files: list[str] = []
    for leg in legs:
        tests_dir = ROOT / "packages" / leg / "tests"
        if not tests_dir.is_dir():
            continue
        for file in sorted(tests_dir.rglob("test_*.py")):
            if "browser" in file.relative_to(tests_dir).parts:
                continue
            try:
                text = file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
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
            [*python_cmd(), "-c", "import testmon"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            timeout=60,
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
    skipped = ["browser smokes (out of scope; plan S4b)"]

    if decision.profile == "docs":
        docs_changed = [p for p in paths if change_scope.is_docs_path(p)]
        steps = [
            Step(
                key="docs-scan",
                task_class="docs-check",
                check="docs-scan",
                reason=(
                    f"docs-only change ({len(docs_changed)} file(s)): whitespace, "
                    "conflict markers, credential tokens"
                ),
                command=[*python_cmd(), "scripts/docs_scan.py", *docs_changed],
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
    parity: list[str] = [
        rel for rel in (PERF_EQUIVALENCE, FASTCORE_PARITY) if (ROOT / rel).is_file()
    ]
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
        Step(
            key="health-changed",
            task_class="changed-gate",
            check="check_health.sh",
            reason=f"affected profile (--changed --base {base})",
            command=["bash", "scripts/check_health.sh", "--changed", "--base", base],
        ),
        Step(
            key="architecture",
            task_class="architecture-gate",
            check="architecture_gate.py",
            reason="affected profile always runs the architecture gate",
            command=[*python_cmd(), "scripts/architecture_gate.py"],
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
            Step(
                key="always-run",
                task_class="always-run-suite",
                check="pytest",
                reason=f"subprocess/CLI/proof_pool and {trigger}{len(always_files)} file(s), testmon disabled",
                command=pytest_cmd(["-p", "no:testmon", *always_files]),
            )
        )
    leg_dirs = [f"packages/{leg}/tests" for leg in legs]
    if decision.testmon and _testmon_available():
        steps.append(
            Step(
                key="pytest-affected",
                task_class="affected-suite",
                check="pytest-testmon",
                reason=f"affected (advisory) selection over legs: {','.join(legs)}",
                command=pytest_cmd(["--testmon", *leg_dirs, *ignore_args]),
            )
        )
    else:
        if decision.testmon:
            print("verify: pytest-testmon not importable; legs run WITHOUT deselection")
        else:
            print(f"verify: testmon disabled ({'; '.join(decision.reasons)}); legs run in full")
        steps.append(
            Step(
                key="pytest-affected",
                task_class="affected-suite",
                check="pytest",
                reason=f"full legs (no deselection): {','.join(legs)}",
                command=pytest_cmd(leg_dirs),
            )
        )
    skipped += ["vulture (full gate only)", "full suite (use --profile full)", *decision.skipped]
    return steps, skipped, "affected"


def build_steps(profile: str, base: str) -> tuple[list[Step], list[str], str]:
    """Return ``(steps, skipped, label)`` for a profile (no execution)."""
    if profile == "quick":
        return (
            [
                Step(
                    key="health-changed",
                    task_class="changed-gate",
                    check="check_health.sh",
                    reason=f"quick profile (--changed --base {base})",
                    command=["bash", "scripts/check_health.sh", "--changed", "--base", base],
                )
            ],
            [
                "browser smokes (out of scope; plan S4b)",
                "pytest (quick profile)",
                "vulture (full gate only)",
            ],
            "quick",
        )
    if profile == "full":
        return (
            [
                Step(
                    key="health-full",
                    task_class="full-gate",
                    check="check_health.sh",
                    reason="full profile",
                    command=["bash", "scripts/check_health.sh"],
                ),
                Step(
                    key="suite-full",
                    task_class="full-suite",
                    check="pytest",
                    reason="full profile: whole suite, -n <cpu_count> --dist loadfile",
                    command=pytest_cmd([]),
                ),
            ],
            ["browser smokes (out of scope; plan S4b)"],
            "full",
        )
    return _affected_steps(base)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("quick", "affected", "full"), required=True)
    parser.add_argument(
        "--base", default="HEAD", help="base revision for the change set (default: HEAD)"
    )
    parser.add_argument("--records", default=None, help="JSONL path for measurement records")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the planned steps, run nothing"
    )
    args = parser.parse_args(argv)

    if args.records:
        os.environ["MSL_VERIFICATION_RECORDS"] = args.records
    started = time.monotonic()
    steps, skipped, label = build_steps(args.profile, args.base)
    results = [run_step(step=step, records=args.records, dry_run=args.dry_run) for step in steps]
    wall = time.monotonic() - started

    failures = [r for r in results if r.status in ("failed", "timeout")]
    unavailable = [r for r in results if r.status == "unavailable"]
    if failures:
        overall, rc = "FAILED", 1
    elif unavailable:
        overall, rc = "UNAVAILABLE", 2
    else:
        overall, rc = "PASSED", 0

    advisory = (
        " — affected (advisory) selection, NOT full verification"
        if label.startswith("affected")
        else ""
    )
    print()
    print(f"verify ({label}): {overall} in {wall:.1f}s{advisory}")
    for result in results:
        print(f"  {result.status:<12} {result.key:<16} {result.secs:.1f}s")
    if not args.dry_run and skipped:
        print(f"skipped: {'; '.join(dict.fromkeys(skipped))}")
    if label.startswith("affected"):
        print("note: affected is advisory; run `./scripts/verify.py --profile full` before push")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
