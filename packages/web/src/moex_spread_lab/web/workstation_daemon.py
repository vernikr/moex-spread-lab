"""Install the canonical loopback screener as a macOS per-user launch agent."""

from __future__ import annotations

import os
import plistlib
import socket
import subprocess
import sys
from pathlib import Path

LABEL = "local.moex-spread-lab.screener"
PORT = 8765


def agent_definition(root: Path, home: Path) -> dict:
    """Use the checkout's locked interpreter and avoid shell quoting or network binds."""
    python = root / ".venv/bin/python3"
    logs = home / "Library/Logs"
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(python),
            "-m",
            "moex_spread_lab.cli",
            "screener",
            "--root",
            str(root),
            "--port",
            str(PORT),
        ],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "StandardOutPath": str(logs / f"{LABEL}.out.log"),
        "StandardErrorPath": str(logs / f"{LABEL}.err.log"),
    }


def _preflight(root: Path, destination: Path) -> bool:
    if sys.platform != "darwin":
        print("Workstation daemon installation requires macOS", file=sys.stderr)
        return False
    if destination.exists():
        print(f"Refusing to overwrite existing {destination}", file=sys.stderr)
        return False
    if not (root / ".venv/bin/python3").is_file():
        print(
            "Checkout virtual environment is missing; run uv sync --frozen first", file=sys.stderr
        )
        return False
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            print(
                f"Port {PORT} is already in use; stop its owner before installing", file=sys.stderr
            )
            return False
    return True


def install(root: Path, *, apply: bool, home: Path | None = None) -> int:
    root = root.resolve()
    home = home or Path.home()
    destination = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    config = agent_definition(root, home)
    if not apply:
        print(f"Would install {destination} and bootstrap gui/<uid>/{LABEL}")
        print(plistlib.dumps(config).decode())
        return 0
    if not _preflight(root, destination):
        return 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    (home / "Library/Logs").mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents overwriting a file installed concurrently.
    try:
        with destination.open("xb") as file:
            plistlib.dump(config, file)
    except FileExistsError:
        print(f"Refusing to overwrite existing {destination}", file=sys.stderr)
        return 1
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(destination)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        destination.unlink()
        print(f"launchctl bootstrap failed: {result.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"Installed {LABEL}; restart with launchctl kickstart -k gui/{os.getuid()}/{LABEL}")
    return 0
