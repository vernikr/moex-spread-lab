"""Atomic, ownership-checked discovery record for a running screener."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def write_instance(root: Path, port: int) -> str:
    folder = root / "data/analytics"
    folder.mkdir(parents=True, exist_ok=True)
    identity = uuid.uuid4().hex
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False
    )
    record: dict[str, Any] = {
        "pid": os.getpid(),
        "port": port,
        "started_at": datetime.now(UTC).isoformat(),
        "git_head": result.stdout.strip() if result.returncode == 0 else None,
        "instance_id": identity,
    }
    path = folder / "server-instance.json"
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=folder, delete=False
        ) as handle:
            temporary = handle.name
            json.dump(record, handle)
        os.replace(temporary, path)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    return identity


def remove_instance(root: Path, identity: str) -> None:
    path = root / "data/analytics/server-instance.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(record, dict) and record.get("instance_id") == identity:
            path.unlink(missing_ok=True)
    except (OSError, ValueError):
        # Do not prevent server shutdown if another process changed the file.
        pass
