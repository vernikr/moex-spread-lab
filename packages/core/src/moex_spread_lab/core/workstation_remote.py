"""Workstation-side actions transported by the authenticated bridge client."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

LABEL = "local.moex-spread-lab.screener"


def command(root: Path, *args: str) -> str:
    result = subprocess.run(args, cwd=root, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(
            f"{args[0]} failed ({result.returncode}): {result.stderr.strip()[:1000]}"
        )
    return result.stdout.strip()


def service(root: Path) -> dict[str, Any]:
    target = f"gui/{os.getuid()}/{LABEL}"
    result = subprocess.run(
        ["launchctl", "print", target], cwd=root, capture_output=True, text=True, check=False
    )
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/", timeout=5) as response:
            http_status = response.status
    except (OSError, urllib.error.URLError):
        http_status = None
    return {"launchd_loaded": result.returncode == 0, "http_status": http_status}


def status(root: Path) -> dict[str, Any]:
    tracked = command(root, "git", "status", "--porcelain", "--untracked-files=no")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/dev/health", timeout=5) as response:
            dev_health = json.load(response)
    except (OSError, urllib.error.URLError, ValueError):
        dev_health = None
    return {
        "head": command(root, "git", "rev-parse", "HEAD"),
        "branch": command(root, "git", "branch", "--show-current"),
        "tracked_dirty": bool(tracked),
        "loadavg": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
        "server": service(root),
        "dev_health": dev_health,
    }


def sync(root: Path) -> dict[str, Any]:
    if command(root, "git", "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("tracked checkout changes detected; refusing to sync")
    if command(root, "git", "branch", "--show-current") != "main":
        raise RuntimeError("checkout must be on main; refusing to sync")
    if not service(root)["launchd_loaded"]:
        raise RuntimeError("launchd service is not installed; refusing to sync")
    old = command(root, "git", "rev-parse", "HEAD")
    command(root, "git", "fetch", "origin", "main")
    changed = command(root, "git", "diff", "--name-only", f"{old}..origin/main").splitlines()
    command(root, "git", "merge", "--ff-only", "origin/main")
    # Always reconcile: a prior sync may have advanced HEAD before uv failed.
    command(root, "uv", "sync", "--frozen")
    fastcore_changed = "packages/core/src/moex_spread_lab/core/_fastcore.c" in changed
    # Also check on retries, since HEAD may have advanced before a failed build.
    command(
        root,
        str(root / ".venv/bin/python3"),
        "-c",
        "from pathlib import Path; from moex_spread_lab.core import _fastcore_loader as l; "
        "m=l.load_fastcore(); assert m is not None, 'fastcore compilation failed'; "
        "assert Path(m.__file__).stat().st_mtime_ns >= Path(l.__file__).with_name('_fastcore.c').stat().st_mtime_ns, 'fastcore binary is stale'",
    )
    command(root, "launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}")
    return {
        "old_head": old,
        "head": command(root, "git", "rev-parse", "HEAD"),
        "dependencies_synced": True,
        "fastcore_source_changed": fastcore_changed,
        "fastcore_verified": True,
        "restart_requested": True,
    }


def probe(payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        "http://127.0.0.1:8765/api/dev/probe",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=150) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"probe HTTP {exc.code}: {exc.read(2000).decode('utf-8', errors='replace')}"
        ) from None


def main() -> int:
    action = sys.argv[1]
    root = (
        Path(sys.argv[2]).expanduser().resolve()
        if len(sys.argv) > 2
        else Path.home() / "Downloads/projects/moex-spread-lab"
    )
    try:
        if action not in {"status", "sync", "probe", "bench"}:
            raise ValueError("unknown action")
        if action == "status":
            result = status(root)
        elif action == "sync":
            result = sync(root)
        elif action == "probe":
            result = probe(json.loads(sys.argv[3]))
        else:
            result = json.loads(
                command(
                    root,
                    str(root / ".venv/bin/python3"),
                    "-m",
                    "moex_spread_lab.core.workstation_bench",
                    sys.argv[3],
                )
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
