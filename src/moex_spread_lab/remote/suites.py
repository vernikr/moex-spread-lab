"""Curated deterministic workstation benchmark workloads."""

from __future__ import annotations

from typing import Any


def suite(name: str, request: dict[str, Any], repeat: int, warmup: int) -> list[dict[str, Any]]:
    pair = {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion"}
    calendar = {
        "universe": "calendar_native",
        "asset": "SR",
        "pair": "SRU6/SRZ6",
        "strategy": "zscore_mean_reversion",
    }
    si_pairs = (
        "SiZ3/SiH4",
        "SiH4/SiM4",
        "SiM4/SiU4",
        "SiU4/SiZ4",
        "SiZ4/SiH5",
        "SiH5/SiM5",
        "SiM5/SiU5",
        "SiU5/SiZ5",
        "SiZ5/SiH6",
        "SiH6/SiM6",
    )
    targets = [{"universe": "calendar_native", "asset": "Si", "pair": p} for p in si_pairs]
    si_windows = [{**t, "strategy": "zscore_mean_reversion"} for t in targets]
    cohort = {**si_windows[-1], "optimize_strategy": True, "targets": targets}
    pair_sized = {**pair, "sizing": "max_affordable"}
    gold = {**pair_sized, "pair": "GLDRUBF/GLZ6"}
    jobs: dict[str, list[tuple[str, dict[str, Any], dict[str, Any]]]] = {
        "catalog_boot": [("catalog", {}, {"timeframe": tf}) for tf in ("1d", "1h")],
        "single_proof_1d_1h": [
            ("run", item, {"timeframe": tf}) for item in (pair_sized, gold) for tf in ("1d", "1h")
        ],
        "calendar_proof": [
            ("run", {**calendar, "strategy": strategy}, {"timeframe": "1d"})
            for strategy in ("zscore_mean_reversion", "convergence_to_ruonia")
        ],
        "window_table_batch": [("windows", {"requests": si_windows}, {"timeframe": "1d"})],
        "cohort_optimize": [("optimize", cohort, {"timeframe": "1d", "random_seed": 41})],
        "ab_fastcore": [("optimize", cohort, {"timeframe": "1d", "random_seed": 41})],
        "custom": [
            (
                str(request.get("action", "catalog")),
                request.get("payload", {}),
                request.get("settings_override", {}),
            )
        ],
    }
    if name not in jobs:
        raise ValueError("unknown benchmark suite")
    return [
        {
            "action": action,
            "payload": payload,
            "settings_override": {"random_seed": 41, **settings},
            "repeat": repeat,
            "warmup": warmup,
            "response_mode": "full",
        }
        for action, payload, settings in jobs[name]
    ]
