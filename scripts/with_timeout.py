#!/usr/bin/env python3
"""Run COMMAND with a wall-clock timeout and process-group cleanup.

Portable replacement for GNU ``timeout(1)`` (absent on stock macOS). Unlike bare
``timeout``, expiry signals the whole process group, so grandchildren spawned by
the command cannot outlive it — this is the process-tree cleanup promised by the
health gate.

Exit status: the command's own status, ``124`` on timeout, ``127`` if the command
cannot be executed.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time

GRACE_SECONDS = 5.0
POLL_SECONDS = 0.02


def _kill_group(pgid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, sig)


def _exit_code(status: int, term_sent_at: float | None) -> int:
    code = os.waitstatus_to_exitcode(status)
    if code >= 0:
        return code
    return 124 if term_sent_at is not None else 128 + (-code)


def _escalate(pid: int, now: float, term_sent_at: float | None) -> float:
    if term_sent_at is None:
        _kill_group(pid, signal.SIGTERM)
        return now
    if now >= term_sent_at + GRACE_SECONDS:
        _kill_group(pid, signal.SIGKILL)
    return term_sent_at


def _wait(pid: int, deadline: float) -> int:
    term_sent_at: float | None = None
    while True:
        try:
            done, status = os.waitpid(pid, os.WNOHANG)
        except InterruptedError:
            continue
        except OSError:
            _kill_group(pid, signal.SIGKILL)
            return 124
        if done == pid:
            return _exit_code(status, term_sent_at)
        now = time.monotonic()
        if now >= deadline:
            term_sent_at = _escalate(pid, now, term_sent_at)
        time.sleep(POLL_SECONDS)


def main() -> int:
    argv = sys.argv[1:]
    try:
        sep = argv.index("--")
        timeout = float(argv[0])
    except (ValueError, IndexError):
        print("usage: with_timeout.py SECONDS -- COMMAND [ARG ...]", file=sys.stderr)
        return 126
    cmd = argv[sep + 1 :]
    if not cmd:
        print("usage: with_timeout.py SECONDS -- COMMAND [ARG ...]", file=sys.stderr)
        return 126

    pid = os.fork()
    if pid == 0:  # child: new process group, then exec
        os.setpgid(0, 0)
        try:
            os.execvp(cmd[0], cmd)
        except OSError as exc:
            print(f"with_timeout: cannot exec {cmd[0]}: {exc}", file=sys.stderr)
            os._exit(127)
    os.setpgid(pid, pid)  # race-safe: whoever runs first wins

    def _forward(signum: int, _frame: object) -> None:
        _kill_group(pid, signal.SIGTERM)
        os._exit(128 + signum)

    signal.signal(signal.SIGINT, _forward)
    signal.signal(signal.SIGTERM, _forward)
    return _wait(pid, time.monotonic() + timeout)


if __name__ == "__main__":
    raise SystemExit(main())
