"""Synthetic NEAR/FAR contract snapshots and LQDT lineage files shared by strategy tests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

NEAR_CONTRACT: dict[str, Any] = {
    "secid": "NEAR",
    "stable_id": "n",
    "board": "RFUD",
    "expiry": "2100-01-01",
    "lot_size": 1000,
    "tick_size": 0.001,
    "tick_value": 1,
    "initial_margin": 900,
    "source_vintage": "current_snapshot",
}
FAR_CONTRACT: dict[str, Any] = {
    **NEAR_CONTRACT,
    "secid": "FAR",
    "stable_id": "f",
    "expiry": "2026-12-17",
    "initial_margin": 1000,
}


def write_near_far_contracts(
    path: Path,
    *,
    near: dict[str, Any] | None = None,
    far: dict[str, Any] | None = None,
    schema_version: str | None = None,
) -> Path:
    """Write a two-leg `contracts.json`; overrides keep the default key order."""
    document: dict[str, Any] = {} if schema_version is None else {"schema_version": schema_version}
    document["instruments"] = [{**NEAR_CONTRACT, **(near or {})}, {**FAR_CONTRACT, **(far or {})}]
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def write_lqdt_lineage(path: Path, prices: Mapping[str, float]) -> Path:
    """Write an observed `lqdt-lineage.v1` benchmark with one row per `date -> price`."""
    rows = [
        {"date": date, "price": price, "source_kind": "observed", "source_ticker": "LQDT"}
        for date, price in prices.items()
    ]
    path.write_text(
        json.dumps({"schema_version": "lqdt-lineage.v1", "rows": rows}), encoding="utf-8"
    )
    return path
