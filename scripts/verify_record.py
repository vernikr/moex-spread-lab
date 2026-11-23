#!/usr/bin/env python3
"""Wrap a verification command and append a sanitized measurement record (plan §2).

Thin instrumentation only: it never changes what the wrapped command does. Every
record is a single JSON line appended to a gitignored JSONL file (default
``reports/perf/records.jsonl``; override with ``--records`` or
``MSL_VERIFICATION_RECORDS``). Raw command output goes to the terminal or to the
caller's log, never into the record.

Environment pinning (plan S0.2): the child runs with ``MOEX_OFFLINE_ONLY=1`` and
``UV_NO_SYNC=1`` (``uv run`` then never re-syncs; run ``uv sync --frozen`` once per
machine instead). The CPU worker count is recorded explicitly (``-n``/``-p`` for
pytest is honoured, otherwise ``os.cpu_count()``).

Sanitization (plan §2, S0 risk): absolute home/repo paths are replaced by
``<HOME>``/``<ROOT>`` and credential-shaped tokens are redacted before anything is
written. Records must never contain account data; the sanitizer is deliberately
conservative and drops nothing else.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECORDS = ROOT / "reports" / "perf" / "records.jsonl"
SCHEMA = "msl.verification-record/1"

_REDACTIONS = (
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "ghp_<redacted>"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github_pat_<redacted>"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]+=*", re.I), "Bearer <redacted>"),
    (re.compile(r"sk-[A-Za-z0-9_-]{10,}"), "sk-<redacted>"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA<redacted>"),
    (
        re.compile(r"((?:token|secret|password|api[_-]?key)\W{1,4})[^\s,;&]{8,}", re.I),
        r"\1<redacted>",
    ),
)


def sanitize(text: str) -> str:
    text = text.replace(str(ROOT), "<ROOT>")
    home = str(Path.home())
    if home:
        text = text.replace(home, "<HOME>")
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True, timeout=120
        )
        return out.stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _candidate_fingerprint(head: str) -> str | None:
    digest = hashlib.sha256()
    digest.update(head.encode())
    diff = _git("diff", "--binary", "HEAD")
    if diff is None:
        return None
    digest.update(diff.encode(errors="surrogateescape"))
    untracked = _git("ls-files", "--others", "--exclude-standard", "-z") or ""
    for rel in sorted(path for path in untracked.split("\0") if path):
        try:
            digest.update(rel.encode())
            digest.update((ROOT / rel).read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return "sha256:" + digest.hexdigest()


def _workers_from_argv(argv: list[str]) -> int | None:
    for index, item in enumerate(argv):
        if item in ("-n", "--numprocesses", "-p"):
            value = argv[index + 1] if index + 1 < len(argv) else ""
        elif item.startswith("--numprocesses="):
            value = item.split("=", 1)[1]
        elif item.startswith("-n") and len(item) > 2 and item[2:].isdigit():
            value = item[2:]
        else:
            value = ""
        if value in ("auto", "logical"):
            return os.cpu_count() or 1
        if value.isdigit():
            return int(value)
    return None


def _detail_outcome(detail: dict[str, str]) -> str | None:
    statuses = list(detail.values())
    for wanted in ("failed", "timeout", "unavailable"):
        if wanted in statuses:
            return wanted
    return "passed" if statuses and all(status == "passed" for status in statuses) else None


def _outcome(exit_code: int, detail: dict[str, str] | None) -> str:
    if detail:
        resolved = _detail_outcome(detail)
        if resolved is not None:
            return resolved
    if exit_code == 0:
        return "passed"
    if exit_code in (124, 137, 143):
        return "timeout"
    return "unavailable" if exit_code == 127 else "failed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--task-class", required=True, help="e.g. full-gate, changed-gate, full-suite"
    )
    parser.add_argument("--phase", default="", help="plan phase, e.g. S0")
    parser.add_argument(
        "--check",
        action="append",
        default=[],
        metavar="NAME:REASON",
        help="selected check and why (repeatable)",
    )
    parser.add_argument("--workers", type=int, default=None, help="override recorded worker count")
    parser.add_argument("--cold-warm", choices=("cold", "warm", "unknown"), default="unknown")
    parser.add_argument("--retries", type=int, default=0)
    parser.add_argument("--publication", choices=("none", "committed", "pushed"), default="none")
    parser.add_argument(
        "--promotion", choices=("none", "synced", "health-confirmed"), default="none"
    )
    parser.add_argument("--notes", default="", help="sanitized before writing")
    parser.add_argument(
        "--records", default=None, help="JSONL path (default reports/perf/records.jsonl)"
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- the command to wrap")
    args = parser.parse_args(argv)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command to wrap is required after `--`")

    head = (_git("rev-parse", "HEAD") or "").strip() or "unknown"
    base = (_git("merge-base", "HEAD", "origin/main") or "").strip() or None
    started = datetime.now(UTC)
    fd, status_tmp = tempfile.mkstemp(prefix="msl-health-status-", suffix=".json")
    os.close(fd)
    os.unlink(status_tmp)  # check_health.sh creates it; we only read it
    timer = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        env={
            **os.environ,
            "MOEX_OFFLINE_ONLY": "1",
            "UV_NO_SYNC": "1",
            "MSL_HEALTH_STATUS_FILE": status_tmp,
        },
    )
    wall = (datetime.now(UTC) - started).total_seconds()

    checks_detail = None
    try:
        raw = json.loads(Path(status_tmp).read_text(encoding="utf-8"))
        checks_detail = {str(k): str(v) for k, v in raw.get("checks", {}).items()}
    except (OSError, json.JSONDecodeError, AttributeError):
        checks_detail = None
    with contextlib.suppress(OSError):
        os.unlink(status_tmp)

    ctx = _RunContext(
        head=head,
        base=base,
        started=started,
        wall=wall,
        checks_detail=checks_detail,
        exit_code=timer.returncode,
    )
    record = _build_record(args, command, ctx)
    records_path = Path(
        args.records or os.environ.get("MSL_VERIFICATION_RECORDS") or DEFAULT_RECORDS
    )
    records_path.parent.mkdir(parents=True, exist_ok=True)
    with records_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    shown = records_path
    try:
        shown = records_path.relative_to(ROOT)
    except ValueError:
        shown = Path(sanitize(str(records_path)))
    print(f"verify-record: outcome={record['outcome']} wall={ctx.wall:.2f}s record={shown}")
    return timer.returncode


@dataclass
class _RunContext:
    head: str
    base: str | None
    started: datetime
    wall: float
    checks_detail: dict[str, str] | None
    exit_code: int


def _build_record(args: argparse.Namespace, command: list[str], ctx: _RunContext) -> dict:
    workers = args.workers or _workers_from_argv(command) or (os.cpu_count() or 1)
    checks = []
    for item in args.check:
        name, _, reason = item.partition(":")
        checks.append({"name": sanitize(name.strip()), "reason": sanitize(reason.strip())})
    return {
        "schema": SCHEMA,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "task_class": args.task_class,
        "phase": args.phase,
        "base_sha": ctx.base,
        "head_sha": ctx.head,
        "candidate_fingerprint": _candidate_fingerprint(ctx.head),
        "selected_checks": checks,
        "runner": {
            "platform": sys.platform,
            "machine": platform.machine(),
            "cpu_count": os.cpu_count() or 1,
            "python": platform.python_version(),
        },
        "workers": workers,
        "offline_only": True,
        "cold_warm": args.cold_warm,
        "durations": {
            "queue_s": None,
            "setup_s": None,
            "collection_s": None,
            "call_s": round(ctx.wall, 3),
            "teardown_s": None,
        },
        "outcome": _outcome(ctx.exit_code, ctx.checks_detail),
        "retries": args.retries,
        "publication": args.publication,
        "promotion": args.promotion,
        "phase_started": ctx.started.isoformat(timespec="seconds"),
        "phase_finished": datetime.now(UTC).isoformat(timespec="seconds"),
        "command": [sanitize(item) for item in command],
        "checks_detail": ctx.checks_detail,
        "notes": sanitize(args.notes),
    }


if __name__ == "__main__":
    raise SystemExit(main())
