"""Pool-tier synthetic workspace (plan S4a).

Separate from ``perf_equivalence_fixtures`` because it is only needed by the
worker-pool and streaming tiers: both files must stay inside the health gate's
new-file token budget, and the canary/pair/C tiers never build this workspace.
"""

from __future__ import annotations

import json
from pathlib import Path

from moex_spread_lab.core.settings import save_settings
from perf_equivalence_fixtures import _build_h1_workspace


def _build_pool_test_workspace(root: Path) -> None:
    _build_h1_workspace(root)
    intake_path = root / "data/w1/current-intake.json"
    intake = json.loads(intake_path.read_text(encoding="utf-8"))
    crz6 = next(item for item in intake["instruments"] if item.get("secid") == "CRZ6")
    intake["instruments"] = [item for item in intake["instruments"] if item.get("secid") != "CRM6"]
    intake["instruments"].append(
        {
            **crz6,
            "secid": "CRM6",
            "expiry": "2026-06-18",
            "settlement": 13.45,
            "tariff_group": "currency",
        }
    )
    intake["instruments"].append(
        {
            **crz6,
            "secid": "CRU6",
            "expiry": "2026-09-17",
            "settlement": 13.55,
            "tariff_group": "currency",
        }
    )
    intake_path.write_text(json.dumps(intake), encoding="utf-8")
    crz6_candles = root / "data/w1/candles-screen/CRZ6_1h.json"
    (root / "data/w1/candles-screen/CRU6_1h.json").write_text(
        crz6_candles.read_text(encoding="utf-8").replace("CRZ6", "CRU6"),
        encoding="utf-8",
    )
    save_settings(
        root,
        {
            "timeframe": "1h",
            "validation_percent": 20,
            "allow_failed_volume": True,
            "allow_failed_capital": True,
            "initial_capital_rub": 100000.0,
            # A fixed seed keeps two runs comparable; seed 0 would draw a new one.
            "random_seed": 41,
        },
    )
