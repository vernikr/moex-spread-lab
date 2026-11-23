"""Provisioning of the isolated benchmark candidate checkout (agent-performance-2, S4c).

Three provisioning modes for the market-data trees (``data/``, ``data-lake/``):

- ``clone`` — APFS copy-on-write (``cp -cR``). Cloned files share extents with the
  live trees and decouple on write, so the candidate is private and setup stays
  seconds even for large datasets. Only available on macOS/APFS.
- ``copy`` — private byte copy (``shutil.copytree``). Portable default on every
  non-APFS filesystem; preserves the isolation invariant everywhere.
- ``symlink`` — read-only-by-convention symlink farm. The fastest fallback for very
  large non-APFS datasets, but the candidate *can* write through the links into the
  live trees. Opt in via ``MOEX_BENCH_DATA=symlink`` only for trusted candidates;
  H1 seal files and ``data/analytics`` always stay private copies.

``MOEX_BENCH_DATA`` selects the mode (``auto`` by default: ``clone`` on macOS,
``copy`` elsewhere). Any ``clone`` failure falls back to ``copy`` — a half-cloned
tree is worse than a slow one. The candidate is trusted code, not a security
sandbox; ``data/analytics`` (mutable runtime state) is never linked or cloned,
and ``research-settings.json`` is always a private copy.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

from .w0 import canonical_bytes, sha256_bytes

SEAL_FILES = frozenset({"h1-seal.json", "h1-seal.enabled.json"})
_DATA_MODE_ENV = "MOEX_BENCH_DATA"
_DATA_NAMES = ("data", "data-lake")


def _has_symlinks(src: Path) -> bool:
    return src.is_symlink() or any(path.is_symlink() for path in src.rglob("*"))


def _check_source(root: Path) -> None:
    for name in _DATA_NAMES:
        src = root / name
        if src.exists() and _has_symlinks(src):
            raise RuntimeError(f"{name} contains symlinks; refusing to provision external targets")


def _private_analytics(root: Path, candidate: Path) -> None:
    analytics = candidate / "data" / "analytics"
    analytics.mkdir(parents=True, exist_ok=True)
    original = root / "data" / "analytics" / "research-settings.json"
    if original.is_file():
        shutil.copy2(original, analytics / "research-settings.json")


def copy_data(root: Path, candidate: Path) -> None:
    _check_source(root)
    for name in _DATA_NAMES:
        src = root / name
        if not src.exists():
            continue
        ignored = shutil.ignore_patterns("analytics") if name == "data" else None
        shutil.copytree(src, candidate / name, ignore=ignored)
    _private_analytics(root, candidate)


def clone_data(root: Path, candidate: Path) -> None:
    """APFS copy-on-write clone of the top-level entries (millisecond metadata)."""
    _check_source(root)
    for name in _DATA_NAMES:
        src = root / name
        if not src.exists():
            continue
        for child in sorted(src.iterdir()):
            if name == "data" and child.name == "analytics":
                continue
            dest = candidate / name / child.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["cp", "-cR", str(child), str(dest)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                raise RuntimeError(f"cp -cR failed for {name}/{child.name}: {result.stderr[:300]}")
    _private_analytics(root, candidate)


def link_data(root: Path, candidate: Path) -> None:
    _check_source(root)
    for name in _DATA_NAMES:
        src = root / name
        if not src.exists():
            continue
        dest = candidate / name
        dest.mkdir(parents=True, exist_ok=True)
        for child in sorted(src.iterdir()):
            if name == "data" and child.name == "analytics":
                _private_analytics(root, candidate)
                continue
            if name == "data-lake" and child.name in SEAL_FILES:
                shutil.copy2(child, dest / child.name)
                continue
            os.symlink(child, dest / child.name)


def provision_data(root: Path, candidate: Path, mode: str | None = None) -> str:
    mode = (mode or os.environ.get(_DATA_MODE_ENV, "auto")).strip().lower()
    if mode not in {"auto", "clone", "copy", "symlink"}:
        raise ValueError(f"{_DATA_MODE_ENV} must be one of auto|clone|copy|symlink")
    if mode == "auto":
        mode = "clone" if sys.platform == "darwin" else "copy"
    if mode == "clone":
        try:
            clone_data(root, candidate)
            return "clone"
        except (OSError, RuntimeError, subprocess.SubprocessError):
            # Partial clones must never leak into the candidate: drop cloned trees
            # and fall back to a private byte copy.
            for name in _DATA_NAMES:
                shutil.rmtree(candidate / name, ignore_errors=True)
            copy_data(root, candidate)
            return "copy"
    if mode == "symlink":
        link_data(root, candidate)
        return "symlink"
    copy_data(root, candidate)
    return "copy"


def relocate_copied_seal(root: Path, candidate: Path) -> None:
    """Rebind only an integrity-checked private seal copy to its temporary root."""
    seal = candidate / "data-lake/h1-seal.json"
    marker = candidate / "data-lake/h1-seal.enabled.json"
    if not seal.exists() and not marker.exists():
        return
    if not seal.is_file() or not marker.is_file():
        raise RuntimeError("incomplete H1 seal; refusing candidate benchmark")
    if seal.is_symlink() or marker.is_symlink():
        raise RuntimeError("H1 seal must be a private copy; refusing to rebind a live link")
    value = json.loads(seal.read_text(encoding="utf-8"))
    activation = json.loads(marker.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(activation, dict):
        raise RuntimeError("invalid source H1 seal; refusing candidate benchmark")
    value = cast(dict[str, Any], value)
    activation = cast(dict[str, Any], activation)
    if (
        value.get("root") != str(root.resolve())
        or value.get("schema_version") != "h1-sealed-read.v1"
        or activation.get("schema_version") != value.get("schema_version")
        or activation.get("seal_sha256") != sha256_bytes(canonical_bytes(value))
    ):
        raise RuntimeError("invalid source H1 seal; refusing candidate benchmark")
    value["root"] = str(candidate.resolve())
    activation["seal_sha256"] = sha256_bytes(canonical_bytes(value))
    seal.write_text(json.dumps(value, sort_keys=True, indent=2), encoding="utf-8")
    marker.write_text(json.dumps(activation, sort_keys=True, indent=2), encoding="utf-8")
