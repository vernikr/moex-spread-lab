#!/usr/bin/env python3
"""Tier-1 repository gate behind ``scripts/check_health.sh``.

Full mode runs the seven checks in waves of at most the usable CPU count, one
line per passing check (``--verbose`` prints every log) and a log tail per
failure. ``--changed [--base REV]`` scopes ruff/basedpyright/ratchets to touched
files, runs ``js-modules`` only for touched static JS and ``architecture --fast``
only when imports moved, and names what it skipped. Each check runs in its own
process group under ``--timeout`` (``MSL_HEALTH_TIMEOUT``, default 900 s).
Exit 0 = all run checks passed, 1 = a failure or timeout, 2 = a check could not
run; a skipped check is never reported as passed. ``MSL_HEALTH_STATUS_FILE``
receives the per-check status JSON.
"""

import argparse
import fnmatch
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.change_scope import changed_paths, imports_changed  # noqa: E402
from scripts.code_health_gate_common import tool  # noqa: E402
from scripts.verify import cpu_budget  # noqa: E402

TASKS = ("ruff-check", "ruff-format", "basedpyright", "vulture", "custom-gate")
TASKS += ("architecture", "js-modules")
# Changed mode overlaps the import probe with basedpyright startup.
CHANGED_ORDER = ("basedpyright", "architecture", "custom-gate", "ruff-check", "ruff-format")
CHANGED_ORDER += ("js-modules", "vulture")
HEAVY = ("basedpyright", "custom-gate")
LINT_ROOTS = ["packages", "src", "tests", "scripts"]
GRACE_SECONDS = 5.0


def script(name: str, *args: str) -> list[str]:
    return [*tool("python"), f"scripts/{name}", *args]


@dataclass
class Scope:
    changed: bool
    base: str
    touched: list[str] = field(default_factory=list)
    imports: bool = False

    @property
    def py(self) -> list[str]:
        return [path for path in self.touched if path.lower().endswith(".py")]

    @property
    def static_js(self) -> bool:
        return any(fnmatch.fnmatch(p, "packages/web/src/*/static/js/*.js") for p in self.touched)


def check_command(name: str, scope: Scope) -> list[str] | None:
    py, changed = scope.py, scope.changed
    lint = py if changed else LINT_ROOTS
    if changed and (
        (name in ("ruff-check", "ruff-format", "basedpyright") and not py)
        or name == "vulture"
        or (name == "architecture" and not scope.imports)
        or (name == "js-modules" and not scope.static_js)
    ):
        return None
    return {
        "ruff-check": [*tool("ruff"), "check", *lint],
        "ruff-format": [*tool("ruff"), "format", "--check", *lint],
        "basedpyright": script("typecheck_workspace.py", *py),
        "vulture": [*tool("vulture"), "packages", "src", "scripts", "--min-confidence", "80"],
        "custom-gate": script(
            "code_health_gate.py", *(["--changed", "--base", scope.base] * changed)
        ),
        "architecture": script("architecture_gate.py", *(["--fast"] * changed)),
        "js-modules": script("check_js_modules.py"),
    }[name]


def _status(returncode: int) -> str:
    if returncode in (124, 137, 143):
        return "timeout"
    return {0: "passed", 2: "unavailable", 127: "unavailable"}.get(returncode, "failed")


def run_check(command: list[str], log: Path, timeout: float) -> tuple[str, int]:
    started = time.monotonic()
    env = {
        **os.environ,
        "PATH": f"{ROOT / '.venv' / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    with log.open("w", encoding="utf-8") as out:
        try:
            proc = subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            out.write(f"cannot run {command[0]}: {exc}\n")
            return "unavailable", 0
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            returncode = 124
            for sig, wait in ((signal.SIGTERM, GRACE_SECONDS), (signal.SIGKILL, None)):
                try:
                    os.killpg(proc.pid, sig)
                    proc.wait(timeout=wait)
                    break
                except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
                    continue
    return _status(returncode), int(time.monotonic() - started)


def _waves(tasks: list[str], scope: Scope, ncpus: int) -> list[list[str]]:
    if scope.changed:
        tasks = [name for name in CHANGED_ORDER if name in tasks]
    alone = [[name] for name in HEAVY if name in tasks] if not scope.changed and ncpus <= 2 else []
    rest = [name for name in tasks if [name] not in alone]
    return alone + [rest[i : i + ncpus] for i in range(0, len(rest), ncpus)]


def run_all(scope: Scope, logs: Path, timeout: float) -> list[tuple[str, str, int]]:
    results: list[tuple[str, str, int]] = []
    ncpus = cpu_budget()
    for wave in _waves(list(TASKS), scope, ncpus):
        commands = {name: check_command(name, scope) for name in wave}
        with ThreadPoolExecutor(max_workers=len(wave)) as pool:
            futures = {
                name: pool.submit(run_check, cmd, logs / f"{name}.log", timeout)
                for name, cmd in commands.items()
                if cmd is not None
            }
        for name in wave:
            status, secs = futures[name].result() if name in futures else ("skipped", 0)
            results.append((name, status, secs))
    return results


def _read_lines(log: Path) -> list[str]:
    try:
        return log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _advisories(log: Path) -> list[str]:
    return [line for line in _read_lines(log) if line.startswith("advisory: ")]


def _tail(log: Path, count: int) -> str:
    return "\n".join(_read_lines(log)[-count:])


def report(
    results: list[tuple[str, str, int]], scope: Scope, logs: Path, verbose: bool, timeout: float
) -> None:
    for name, status, secs in results:
        log = logs / f"{name}.log"
        if verbose and log.is_file():
            print(log.read_text(encoding="utf-8", errors="replace"), end="")
        if status == "passed" and not scope.changed:
            advisories = _advisories(log)
            print(
                f"{name}: passed ({secs}s{f', {len(advisories)} advisory' if advisories else ''})"
            )
            print("".join(f"  {line}\n" for line in advisories[:5]), end="")
            if len(advisories) > 5:
                print(f"  advisory: … (see {name} log)")
        elif status in ("timeout", "unavailable", "failed"):
            header = {
                "timeout": f"TIMEOUT after {int(timeout)}s",
                "unavailable": "UNAVAILABLE (could not run)",
            }.get(status, "failed")
            print(f"=== {name}: {header} ===")
            print(_tail(log, 10 if status == "unavailable" else 40))


def _scope(args: argparse.Namespace) -> Scope:
    if not args.changed:
        return Scope(False, args.base)
    touched = changed_paths(args.base)
    return Scope(True, args.base, touched, bool(touched) and imports_changed(args.base))


def _write_status(mode: str, results: list[tuple[str, str, int]]) -> None:
    target = os.environ.get("MSL_HEALTH_STATUS_FILE")
    if target:
        checks = {name: status for name, status, _ in results}
        Path(target).write_text(
            json.dumps({"mode": mode, "checks": checks}) + "\n", encoding="utf-8"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_health.sh", description=__doc__)
    parser.add_argument("--verbose", "-v", action="store_true", help="print every check's log")
    parser.add_argument("--changed", action="store_true", help="only check touched files")
    parser.add_argument("--base", default="HEAD", help="base revision for --changed")
    parser.add_argument(
        "--timeout", type=float, default=float(os.environ.get("MSL_HEALTH_TIMEOUT", 900))
    )
    args = parser.parse_args(argv)
    started = time.monotonic()
    mode = "changed" if args.changed else "full"
    try:
        scope = _scope(args)
    except RuntimeError as exc:
        print(
            f"check_health: changed-files detection failed (base {args.base}): {exc}",
            file=sys.stderr,
        )
        return 2
    if scope.changed and not scope.touched:
        print(f"check_health (changed): no touched files (base {args.base})")
        _write_status(mode, [])
        return 0
    with tempfile.TemporaryDirectory(prefix="msl-health.") as tmp:
        logs = Path(tmp)
        results = run_all(scope, logs, args.timeout)
        report(results, scope, logs, args.verbose, args.timeout)
        statuses = [status for _, status, _ in results]
        failures = sum(status in ("failed", "timeout") for status in statuses)
        unavailable = statuses.count("unavailable")
        elapsed = int(time.monotonic() - started)
        if scope.changed:
            ran = [name for name, status, _ in results if status != "skipped"]
            advisory = sum(len(_advisories(logs / f"{name}.log")) for name in ran)
            summary = ", ".join(ran) + (f"; {advisory} advisory" if advisory else "")
            skipped = ", ".join(n for n, s, _ in results if s == "skipped")
            if not failures and not unavailable:
                print(f"check_health (changed): passed in {elapsed}s ({summary})")
            print(f"skipped (full gate): {skipped or 'none'}")
            print("full verification: ./scripts/check_health.sh")
        elif not failures and not unavailable:
            print(f"check_health: passed in {elapsed}s")
    _write_status(mode, results)
    if failures:
        print("check_health: failed", file=sys.stderr)
        return 1
    if unavailable:
        print(f"check_health: {unavailable} check(s) unavailable", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
