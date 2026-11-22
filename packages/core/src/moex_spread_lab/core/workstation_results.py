"""Normalize root-bound provenance for isolated worktree result comparison."""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Any, cast


def normalize_provenance(full: Any, root: Path | None) -> Any:
    if root is None or not isinstance(full, dict):
        return full
    mapping = cast(dict[str, Any], full)
    raw: Any = mapping.get("provenance")
    if not isinstance(raw, dict):
        return mapping
    provenance = dict(cast(dict[str, Any], raw))
    for key, entry in provenance.items():
        if key.endswith("_path") and isinstance(entry, str) and Path(entry).is_absolute():
            # External paths stay unchanged, and cannot compare equal accidentally.
            with suppress(ValueError):
                provenance[key] = "$ROOT/" + str(Path(entry).resolve().relative_to(root.resolve()))
    return {**mapping, "provenance": provenance}
