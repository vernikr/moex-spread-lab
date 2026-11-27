#!/usr/bin/env python3
"""Agent verification launcher: ``quick`` | ``affected`` | ``full`` | ``pre-push``.

``quick`` runs ``check_health.sh --changed``; ``affected`` runs a docs-only scan or the
router-scoped legs (advisory testmon plus the always-run set; full fallback when no
path matches); ``full``/``pre-push`` run the full gate and the whole suite with every
parity tier. Browser smokes live in the scheduled ``run_browser_smokes.py`` job.

Steps run with ``MOEX_OFFLINE_ONLY=1``/``UV_NO_SYNC=1`` and append one sanitized
``msl.verification-record/1`` line to ``reports/perf/records.jsonl`` (``--records`` or
``MSL_VERIFICATION_RECORDS`` override); paths and credential-shaped tokens are redacted
and raw output never enters a record. pytest workers default to the usable CPU budget;
``MSL_VERIFY_WORKERS`` overrides.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import change_scope  # noqa: E402  (path inserted above)

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
BROWSER_SKIPPED = "browser smokes (separate scheduled job; not run by verify profiles)"
ALWAYS_RUN_RE = re.compile(
    r"(subprocess|proof_pool|moex_spread_lab[._]cli|cli\.main|moex-spread-lab)"
)
DEFAULT_RECORDS = ROOT / "reports" / "perf" / "records.jsonl"
HEALTH_CMD = ["bash", "scripts/check_health.sh"]
_REDACTIONS = (
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "ghp_<redacted>"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github_pat_<redacted>"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]+=*", re.I), "Bearer <redacted>"),
    (re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{10,}"), "sk-<redacted>"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA<redacted>"),
    (
        re.compile(r"((?:token|secret|password|api[_-]?key)\W{1,4})[^\s,;&]{8,}", re.I),
        r"\1<redacted>",
    ),
)
STEP_TASK_CLASS = {
    "docs-scan": "docs-check",
    "health-changed": "changed-gate",
    "health-full": "full-gate",
    "architecture": "architecture-gate",
    "always-run": "always-run-suite",
    "pytest-affected": "affected-suite",
    "suite-full": "full-suite",
}


def cpu_budget() -> int:
    """CPUs this process may actually use: host count, affinity, then cgroup quota."""
    count = os.cpu_count() or 1
    if hasattr(os, "sched_getaffinity"):
        count = min(count, len(os.sched_getaffinity(0)))
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            count = min(count, max(1, int(quota) // int(period)))
    except (OSError, ValueError):
        pass
    return max(1, count)


def worker_count() -> int:
    """``MSL_VERIFY_WORKERS`` wins when it is a positive integer, else the CPU budget."""
    override = os.environ.get("MSL_VERIFY_WORKERS", "")
    return int(override) if override.isdigit() and int(override) > 0 else cpu_budget()


def sanitize(text: str) -> str:
    text = text.replace(str(ROOT), "<ROOT>").replace(str(Path.home()), "<HOME>")
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def python_cmd() -> list[str]:
    venv_python = ROOT / ".venv" / "bin" / "python"
    return [str(venv_python)] if venv_python.is_file() else ["uv", "run", "--no-sync", "python"]


@dataclass(frozen=True)
class Step:
    key: str
    reason: str
    command: list[str]


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True, timeout=120
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def _fingerprint(head: str) -> str:
    digest = hashlib.sha256(head.encode())
    digest.update(_git("diff", "--binary", "HEAD").encode(errors="surrogateescape"))
    for rel in sorted(
        filter(None, _git("ls-files", "--others", "--exclude-standard", "-z").split("\0"))
    ):
        digest.update(rel.encode())
        try:
            digest.update((ROOT / rel).read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return "sha256:" + digest.hexdigest()


def _status_of(returncode: int, detail: dict[str, str] | None = None) -> str:
    """Exit code (or check_health detail) -> status; 124/137/143 are timeouts."""
    statuses = list((detail or {}).values())
    for wanted in ("failed", "timeout", "unavailable"):
        if wanted in statuses:
            return wanted
    if returncode in (124, 137, 143):
        return "timeout"
    return {0: "passed", 127: "unavailable"}.get(returncode, "failed")


def _record(step: Step, started: datetime, wall: float, rc: int, detail: dict | None) -> dict:
    head = _git("rev-parse", "HEAD").strip() or "unknown"
    workers = worker_count() if "pytest" in step.command else os.cpu_count() or 1
    return {
        "schema": "msl.verification-record/1",
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "task_class": STEP_TASK_CLASS[step.key],
        "phase": "S2",
        "base_sha": _git("merge-base", "HEAD", "origin/main").strip() or None,
        "head_sha": head,
        "candidate_fingerprint": _fingerprint(head),
        "selected_checks": [{"name": step.key, "reason": sanitize(step.reason)}],
        "runner": {
            "platform": sys.platform,
            "machine": platform.machine(),
            "cpu_count": os.cpu_count() or 1,
            "python": platform.python_version(),
        },
        "workers": workers,
        "offline_only": True,
        "durations": {"call_s": round(wall, 3)},
        "outcome": _status_of(rc, detail),
        "phase_started": started.isoformat(timespec="seconds"),
        "phase_finished": datetime.now(UTC).isoformat(timespec="seconds"),
        "command": [sanitize(item) for item in step.command],
        "checks_detail": detail,
    }


def run_step(step: Step, records: Path, dry_run: bool) -> tuple[str, float]:
    if dry_run:
        print(f"dry-run [{step.key}]: {' '.join(step.command)}")
        return "not run", 0.0
    started, t0 = datetime.now(UTC), time.monotonic()
    with tempfile.TemporaryDirectory(prefix="msl-verify-") as tmp:
        status_file = Path(tmp) / "health.json"
        env = {
            **os.environ,
            "MOEX_OFFLINE_ONLY": "1",
            "UV_NO_SYNC": "1",
            "MSL_HEALTH_STATUS_FILE": str(status_file),
        }
        try:
            rc = subprocess.run(step.command, cwd=ROOT, env=env, check=False).returncode
        except OSError as exc:
            print(f"verify: cannot launch {step.key}: {exc}", file=sys.stderr)
            rc = 127
        try:
            detail = {
                str(k): str(v) for k, v in json.loads(status_file.read_text())["checks"].items()
            }
        except (OSError, ValueError, KeyError, AttributeError):
            detail = None
    wall = time.monotonic() - t0
    record = _record(step, started, wall, rc, detail)
    records.parent.mkdir(parents=True, exist_ok=True)
    with records.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    shown = records.relative_to(ROOT) if records.is_relative_to(ROOT) else sanitize(str(records))
    print(f"verify-record: outcome={record['outcome']} wall={wall:.2f}s record={shown}")
    return record["outcome"], wall


def pytest_cmd(extra: list[str], marker: str = "not slow_equiv") -> list[str]:
    """pytest with an explicit marker: ``marker=""`` must reach pytest as ``-m ""``,
    or ``addopts`` silently drops the slow_equiv parity tier (plan S4a)."""
    flags = ["-m", "pytest", "-m", marker, "-n", str(worker_count()), "--dist", "loadfile"]
    return [*python_cmd(), *flags, "-q", "--tb=short", *extra]


def always_run_files(legs: list[str]) -> list[str]:
    """Test modules spawning subprocesses / the CLI / proof_pool (testmon-unsafe)."""
    files: list[str] = []
    for leg in legs:
        tests_dir = ROOT / "packages" / leg / "tests"
        for file in sorted(tests_dir.rglob("test_*.py")) if tests_dir.is_dir() else ():
            text = file.read_text(encoding="utf-8", errors="ignore")
            if "browser" not in file.relative_to(tests_dir).parts and ALWAYS_RUN_RE.search(text):
                files.append(str(file.relative_to(ROOT)))
    return files


def _testmon_available() -> bool:
    try:
        probe = subprocess.run(
            [*python_cmd(), "-c", "import testmon"], cwd=ROOT, capture_output=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def _docs_steps(paths: list[str]) -> tuple[list[Step], list[str], str]:
    docs = [p for p in paths if change_scope.is_docs_path(p)]
    reason = (
        f"docs-only change ({len(docs)} file(s)): whitespace, conflict markers, credential tokens"
    )
    steps = [Step("docs-scan", reason, [*python_cmd(), "scripts/docs_scan.py", *docs])]
    skipped = [
        BROWSER_SKIPPED,
        *(f"{name} (docs-only)" for name in ("check_health.sh", "pytest", "architecture gate")),
    ]
    return steps, skipped, "affected/docs"


def _affected_steps(base: str) -> tuple[list[Step], list[str], str]:
    try:
        paths, nod = change_scope.local_change_set(base)
    except RuntimeError as exc:
        print(f"verify: {exc}; falling back to full profile")
        paths, nod = ["__router_error__"], set()
    decision = change_scope.decide_paths(paths, new_or_deleted=nod)
    if decision.profile == "docs":
        return _docs_steps(paths)
    if decision.profile == "full":
        print(
            f"verify: no scoped path applies — falling back to full ({'; '.join(decision.reasons)})"
        )
        return build_steps("full", base)
    os.environ.setdefault("TESTMON_DATAFILE", str(ROOT / ".testmondata"))
    legs = list(decision.packages)
    triggers = sorted(PARITY_TRIGGER_PACKAGES.intersection(change_scope.touched_packages(paths)))
    parity = [rel for rel in (*EQUIVALENCE_TIERS, FASTCORE_PARITY) if (ROOT / rel).is_file()]
    always = always_run_files(legs)
    always_files: list[str] = list(dict.fromkeys([*always, *(parity if triggers else [])]))
    browser = [
        f"packages/{leg}/tests/browser"
        for leg in legs
        if (ROOT / f"packages/{leg}/tests/browser").is_dir()
    ]
    ignores = [f"--ignore={rel}" for rel in dict.fromkeys([*always, *parity, *browser])]
    steps = [
        Step(
            "health-changed",
            f"affected profile (--changed --base {base})",
            [*HEALTH_CMD, "--changed", "--base", base],
        ),
        Step(
            "architecture",
            "affected profile always runs the architecture gate",
            [*python_cmd(), "scripts/architecture_gate.py"],
        ),
    ]
    if always_files:
        trigger = (
            f"perf-equivalence/fastcore-parity triggered by {','.join(triggers)}; "
            if triggers
            else ""
        )
        reason = (
            f"subprocess/CLI/proof_pool and {trigger}{len(always_files)} file(s), testmon disabled"
        )
        steps.append(Step("always-run", reason, pytest_cmd(["-p", "no:testmon", *always_files])))
    leg_dirs = [f"packages/{leg}/tests" for leg in legs]
    if decision.testmon and _testmon_available():
        steps.append(
            Step(
                "pytest-affected",
                f"affected (advisory) selection over legs: {','.join(legs)}",
                pytest_cmd(["--testmon", *leg_dirs, *ignores]),
            )
        )
    else:
        print(
            "verify: pytest-testmon not importable; legs run WITHOUT deselection"
            if decision.testmon
            else f"verify: testmon disabled ({'; '.join(decision.reasons)}); legs run in full"
        )
        steps.append(
            Step(
                "pytest-affected",
                f"full legs (no deselection): {','.join(legs)}",
                pytest_cmd(leg_dirs, marker=""),
            )
        )
    skipped = [
        BROWSER_SKIPPED,
        "vulture (full gate only)",
        "full suite (use --profile full)",
        *decision.skipped,
    ]
    return steps, skipped, "affected"


def build_steps(profile: str, base: str) -> tuple[list[Step], list[str], str]:
    """Return ``(steps, skipped, label)`` for a profile (no execution)."""
    if profile == "quick":
        steps = [
            Step(
                "health-changed",
                f"quick profile (--changed --base {base})",
                [*HEALTH_CMD, "--changed", "--base", base],
            )
        ]
        return (
            steps,
            [BROWSER_SKIPPED, "pytest (quick profile)", "vulture (full gate only)"],
            "quick",
        )
    if profile in ("pre-push", "full"):
        steps = [
            Step("health-full", f"{profile} profile (full gate)", list(HEALTH_CMD)),
            Step(
                "suite-full",
                f"{profile}: whole suite, every parity tier (no deselection)",
                pytest_cmd([], marker=""),
            ),
        ]
        extra = (
            ["testmon deselection (never on the pre-push path)"] if profile == "pre-push" else []
        )
        return steps, [BROWSER_SKIPPED, *extra], profile
    return _affected_steps(base)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--base", default="HEAD", help="base revision for the change set")
    parser.add_argument("--records", default=None, help="JSONL path for measurement records")
    parser.add_argument("--dry-run", action="store_true", help="print steps, run nothing")
    args = parser.parse_args(argv)
    records = Path(args.records or os.environ.get("MSL_VERIFICATION_RECORDS") or DEFAULT_RECORDS)
    started = time.monotonic()
    steps, skipped, label = build_steps(args.profile, args.base)
    results = [(step.key, *run_step(step, records, args.dry_run)) for step in steps]
    statuses = {status for _, status, _ in results}
    if statuses & {"failed", "timeout"}:
        overall, rc = "FAILED", 1
    elif "unavailable" in statuses:
        overall, rc = "UNAVAILABLE", 2
    else:
        overall, rc = "PASSED", 0
    print(f"\nverify ({label}): {overall} in {time.monotonic() - started:.1f}s")
    if label.startswith("affected"):
        print("note: affected is advisory, NOT full verification")
    for key, status, secs in results:
        print(f"  {status:<12} {key:<16} {secs:.1f}s")
    if not args.dry_run and skipped:
        print(f"skipped: {'; '.join(dict.fromkeys(skipped))}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
