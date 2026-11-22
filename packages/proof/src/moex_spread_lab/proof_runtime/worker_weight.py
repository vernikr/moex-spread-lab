"""Estimate cohort shard weights without loading underlying market bars."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from moex_spread_lab.data.lake.bar_types import SealedH1Reader


def _seal_data(root: Path, cache: dict[str, Any] | None):
    if cache is not None and "reader" in cache:
        return cache["reader"], cache["seal"], cache["by_legacy"]
    reader = SealedH1Reader(root)
    seal = reader.payload()
    by_legacy = {e["legacy_path"]: e for e in seal["plan"]["files"]} if seal is not None else {}
    if cache is not None:
        cache.update(reader=reader, seal=seal, by_legacy=by_legacy)
    return reader, seal, by_legacy


def _pair_h1_weight(root: Path, contract: str, cache: dict[str, Any] | None) -> int:
    reader, seal, by_legacy = _seal_data(root, cache)
    if seal is not None:
        selected = reader.select(contract).relative_to(reader.root).as_posix()
        entry = by_legacy.get(selected)
        return max(100, entry["logical_bytes"] // 128) if entry is not None else 1000
    for subdir in ("candles-screen", "candles"):
        path = root / "data/w1" / subdir / f"{contract}_1h.json"
        try:
            return max(100, path.stat().st_size // 128)
        except OSError:
            pass
    return 1000


def _estimate_weight(
    root: Path, item: dict[str, Any], timeframe: str, *, _seal_cache: dict[str, Any] | None = None
) -> int:
    if timeframe == "1h" and item.get("universe") == "pair_screen":
        parts = str(item.get("pair") or "").split("/")
        if len(parts) == 2:
            return _pair_h1_weight(root, parts[1], _seal_cache)
    return 1000 if timeframe == "1h" else 200
