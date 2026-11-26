"""Retired one-time H1 migration command; retained until the CLI is cleaned up."""

from pathlib import Path
from typing import Any

from moex_spread_lab.core.w0 import W0Error


def execute(
    root: Path,
    plan_path: Path,
    mode: str,
    archive: Path | None = None,
    evidence: Path | None = None,
    retry_restored: bool = False,
) -> dict[str, Any]:
    raise W0Error("H1 migration archive has been retired; use data-archive for rebuildable caches")
