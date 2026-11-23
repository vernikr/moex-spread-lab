"""Shared fixtures for the ``pair_screener.catalog`` strict-typing regression (Issue #114).

The fixtures mirror the offline intake layout the catalog reads: an instrument intake, the
LQDT benchmark lineage and two candle files per pair. They stay in a helper module so the
behavioural test module stays inside the new-file token budget.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

NEAR = "CNYRUBF"
FAR = "CRZ6"
PAIR_ID = f"{NEAR}/{FAR}"
PAIR = {"pair_id": PAIR_ID, "near": NEAR, "far": FAR, "family": "perpetual_quarterly_same_asset"}
DAYS = [f"2026-01-0{index}" for index in range(1, 6)]
CAPITAL = 100_000.0

ENTRY_KEYS = {
    "id",
    "timeframe",
    "near",
    "far",
    "family",
    "from",
    "to",
    "observations",
    "expiry",
    "current_margin_pct",
    "max_pair_margin_rub",
    "passes_capital_screen",
    "tradable_every_day",
    "min_daily_volume_rule",
    "min_leg_daily_volume",
    "strategies",
    "proof_strategies",
    "data_reasons",
    "status",
}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _candles(secid: str, base: float, volumes: list[float]) -> dict[str, Any]:
    return {
        "schema_version": "iss-candles.v1",
        "secid": secid,
        "interval": 24,
        "rows": [
            {
                "begin": f"{day} 00:00:00",
                "open": base + index,
                "close": base + index,
                "high": base + index + 0.1,
                "low": base + index - 0.1,
                "volume": volume,
                "price_proxy_kind": "candle_close",
            }
            for index, (day, volume) in enumerate(zip(DAYS, volumes, strict=True))
        ],
    }


def build_root(root: Path, *, far_volume: float = 1500, initial_margin: float = 1000) -> None:
    write_json(
        root / "data/w1/current-intake.json",
        {
            "schema_version": "iss-intake.v1",
            "instruments": [
                {
                    "secid": secid,
                    "stable_id": secid,
                    "board": "RFUD",
                    "expiry": "2026-12-17",
                    "lot_size": 1000,
                    "tick_size": 0.001,
                    "tick_value": 1,
                    "initial_margin": initial_margin,
                    "source_vintage": "current_snapshot",
                }
                for secid in (NEAR, FAR)
            ],
        },
    )
    write_json(
        root / "data/benchmark/lqdt-lineage.json",
        {
            "schema_version": "lqdt-lineage.v1",
            "rows": [
                {
                    "date": day,
                    "price": 1 + index * 0.0004,
                    "source_kind": "observed",
                    "source_ticker": "LQDT",
                }
                for index, day in enumerate(DAYS)
            ],
        },
    )
    write_json(
        root / f"data/w1/candles/{NEAR}_1d.json",
        _candles(NEAR, 12.0, [5000, 4000, 3000, 1000, 2000]),
    )
    write_json(root / f"data/w1/candles/{FAR}_1d.json", _candles(FAR, 13.1, [far_volume] * 5))


class StubFunding:
    """Records the funding-evidence call and returns a fixed availability payload."""

    def __init__(self, available: bool = True, reason: str | None = None) -> None:
        self.calls: list[tuple[Any, Any, Any, Any]] = []
        self.available = available
        self.reason = reason

    def statuses(
        self,
        pair: Any,
        positive_days: Any,
        price_files: Any,
        funding_files: Any,
    ) -> tuple[dict[str, bool], dict[str, str]]:
        self.calls.append((pair, positive_days, price_files, funding_files))
        reasons = {} if self.reason is None else {"mean_reversion": self.reason}
        return {"mean_reversion": self.available, "funding_only": self.available}, reasons
