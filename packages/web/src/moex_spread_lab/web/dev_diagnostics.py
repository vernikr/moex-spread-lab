"""Small, read-only health and profiling helpers for loopback diagnostics."""

from __future__ import annotations

import cProfile
import io
import os
import pstats
import sysconfig
from pathlib import Path

from moex_spread_lab.core import _fastcore_loader


def fastcore_binary(root: Path) -> tuple[bool, str | None]:
    """Report the normal binary's presence and privacy-preserving location, not load state."""
    path = (
        Path(_fastcore_loader.__file__)
        .resolve()
        .with_name("_fastcore" + (sysconfig.get_config_var("EXT_SUFFIX") or ".so"))
    )
    if not path.is_file():
        return False, None
    try:
        return True, "$ROOT/" + path.relative_to(root.resolve()).as_posix()
    except ValueError:
        return True, "$EXTERNAL/" + path.name


def calendar_cache_count(root: Path) -> int | None:
    """Count regular JSON files directly in the calendar EOD cache; None means unreadable."""
    try:
        with os.scandir(root / "data/spread-history-cache") as entries:
            return sum(entry.name.endswith(".json") and entry.is_file() for entry in entries)
    except FileNotFoundError:
        return 0
    except OSError:
        return None


def profile_summary(profiler: cProfile.Profile) -> str:
    """Top 20 cumulative functions, retaining all rows and bounding each line."""
    stream = io.StringIO()
    pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(20)
    return "\n".join(line[:240] for line in stream.getvalue().splitlines()) + "\n"
