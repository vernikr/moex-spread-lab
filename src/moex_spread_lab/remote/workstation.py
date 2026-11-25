"""Workstation-side actions transported by the authenticated bridge client."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

LABEL = "local.moex-spread-lab.screener"
FASTCORE_SOURCE = "packages/core/src/moex_spread_lab/core/_fastcore.c"
READINESS_TIMEOUT_S = 60.0
READINESS_INTERVAL_S = 0.25
_NON_RUNTIME_PREFIXES = (
    "docs/",
    "tests/",
    "reports/",
    "scripts/",
    "config/",
    "fixtures/",
    ".github/",
)


def patch_paths(patch: str) -> list[str]:
    """Extract the changed paths from a unified diff (both sides of renames)."""
    paths: set[str] = set()
    for line in patch.splitlines():
        if not line.startswith("diff --git "):
            continue
        match = re.match(
            r'^(?:"a/(?P<aq>[^"]+)"|a/(?P<a>\S+))\s+(?:"b/(?P<bq>[^"]+)"|b/(?P<b>\S+))$',
            line[len("diff --git ") :],
        )
        if match is None:
            continue
        paths.update(group for group in match.groups() if group is not None)
    return sorted(paths)


def is_dependency_input(path: str) -> bool:
    if path in {
        "uv.lock",
        ".python-version",
        "pyproject.toml",
        "uv.toml",
        "setup.py",
        "setup.cfg",
    }:
        return True
    return path.endswith("/pyproject.toml")


def _is_runtime_path(path: str) -> bool:
    """True when the served process can import or execute the file.

    Unknown paths fail safe towards a restart; docs, tests, workflows, helper
    scripts and root-level files never restart the service.
    """
    if is_dependency_input(path) or path == FASTCORE_SOURCE:
        return True
    parts = path.split("/")
    if len(parts) == 1:
        return False  # root-level file: docs, dotfiles, governance
    if parts[0] in {prefix.rstrip("/") for prefix in _NON_RUNTIME_PREFIXES}:
        return False
    if parts[0] == "src":
        return True
    if parts[0] == "packages":
        return len(parts) > 2 and parts[2] == "src"
    return True


def _verify_fastcore(root: Path) -> None:
    command(
        root,
        str(root / ".venv/bin/python3"),
        "-c",
        "from pathlib import Path; from moex_spread_lab.core import _fastcore_loader as l; "
        "m=l.load_fastcore(); assert m is not None, 'fastcore compilation failed'; "
        "assert Path(m.__file__).stat().st_mtime_ns >= Path(l.__file__).with_name('_fastcore.c').stat().st_mtime_ns, 'fastcore binary is stale'",
    )


def await_readiness(
    root: Path,
    expected_head: str,
    *,
    timeout_s: float = READINESS_TIMEOUT_S,
    interval_s: float = READINESS_INTERVAL_S,
) -> dict[str, Any]:
    """Poll loopback readiness until the daemon serves ``expected_head``."""
    deadline = time.monotonic() + timeout_s
    last_error = "no attempt made"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:8765/api/dev/health", timeout=2
            ) as response:
                payload: object = json.load(response)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            last_error = str(exc)
        else:
            if not isinstance(payload, dict):
                last_error = "health endpoint returned a non-object payload"
            else:
                payload_map = cast(dict[str, Any], payload)
                if payload_map.get("git_head") == expected_head:
                    return payload_map
                last_error = (
                    f"daemon serves {payload_map.get('git_head')}, expected {expected_head}"
                )
        time.sleep(interval_s)
    raise RuntimeError(
        f"workstation service did not become ready within {timeout_s:.0f}s ({last_error})"
    )


def _sync_result(
    old: str,
    head: str,
    *,
    published: str,
    synced: str,
    health_confirmed: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    return {
        "old_head": old,
        "head": head,
        "restart_requested": bool(state.get("restarted", False)),
        **state,
        "outcomes": {
            "published": published,
            "synced": synced,
            "health-confirmed": health_confirmed,
        },
    }


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
    new = command(root, "git", "rev-parse", "origin/main")
    if old == new:
        # Repeated-SHA no-op: the daemon already serves this head.
        return _sync_result(
            old,
            old,
            published="up-to-date",
            synced="skipped-up-to-date",
            health_confirmed="not-required",
            state={
                "restarted": False,
                "health": None,
                "dependencies_synced": False,
                "fastcore_source_changed": False,
                "fastcore_verified": False,
                "changed_files": 0,
            },
        )
    changed = command(root, "git", "diff", "--name-only", f"{old}..{new}").splitlines()
    command(root, "git", "merge", "--ff-only", "origin/main")
    dependencies_synced = False
    if (
        any(is_dependency_input(path) for path in changed)
        or not (root / ".venv/bin/python3").exists()
    ):
        command(root, "uv", "sync", "--frozen")
        dependencies_synced = True
    fastcore_changed = FASTCORE_SOURCE in changed
    fastcore_verified = False
    if dependencies_synced or fastcore_changed:
        # A prior sync may have advanced HEAD before a build failed, so the
        # freshness check runs whenever the environment was reconciled.
        _verify_fastcore(root)
        fastcore_verified = True
    head = command(root, "git", "rev-parse", "HEAD")
    runtime_changed = dependencies_synced or any(_is_runtime_path(path) for path in changed)
    if not runtime_changed:
        return _sync_result(
            old,
            head,
            published="fast-forwarded",
            synced="skipped-no-dependency-changes",
            health_confirmed="not-required",
            state={
                "restarted": False,
                "health": None,
                "dependencies_synced": dependencies_synced,
                "fastcore_source_changed": fastcore_changed,
                "fastcore_verified": fastcore_verified,
                "changed_files": len(changed),
            },
        )
    command(root, "launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}")
    health = await_readiness(root, head)
    return _sync_result(
        old,
        head,
        published="fast-forwarded",
        synced="uv-sync" if dependencies_synced else "skipped-no-dependency-changes",
        health_confirmed="confirmed",
        state={
            "restarted": True,
            "health": health,
            "dependencies_synced": dependencies_synced,
            "fastcore_source_changed": fastcore_changed,
            "fastcore_verified": fastcore_verified,
            "changed_files": len(changed),
        },
    )


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
                    "moex_spread_lab.remote.bench",
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
