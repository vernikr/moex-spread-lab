"""Shared universe registry, historical pair windows, and strategy parameter defaults."""

from __future__ import annotations

from .settings import (
    DEFAULT_INITIAL_CAPITAL_RUB,
    INITIAL_CAPITAL,
    MAX_QUANTITY,
    MIN_DAILY_VOLUME,
)

PAIR_UNIVERSE = "pair_screen"
CALENDAR_UNIVERSE = "calendar_native"

SCREEN_PAIRS = (
    {
        "pair_id": "GLDRUBF/GLZ6",
        "near": "GLDRUBF",
        "far": "GLZ6",
        "family": "perpetual_quarterly_same_asset",
    },
    {
        "pair_id": "IMOEXF/MMZ6",
        "near": "IMOEXF",
        "far": "MMZ6",
        "family": "perpetual_quarterly_same_asset",
    },
    {
        "pair_id": "GAZPF/GZZ6",
        "near": "GAZPF",
        "far": "GZZ6",
        "family": "perpetual_quarterly_same_asset",
    },
    {
        "pair_id": "SBERF/SRZ6",
        "near": "SBERF",
        "far": "SRZ6",
        "family": "perpetual_quarterly_same_asset",
    },
    {
        "pair_id": "RGBIF/RBZ6",
        "near": "RGBIF",
        "far": "RBZ6",
        "family": "perpetual_quarterly_same_asset",
    },
    {
        "pair_id": "USDRUBF/SiZ6",
        "near": "USDRUBF",
        "far": "SiZ6",
        "family": "perpetual_quarterly_same_asset",
    },
)

EXTRA_PAIRS = (
    {
        "pair_id": "SLVRUBF/S2Z6",
        "near": "SLVRUBF",
        "far": "S2Z6",
        "family": "perpetual_quarterly_same_asset",
    },
    {
        "pair_id": "EURRUBF/EuZ6",
        "near": "EURRUBF",
        "far": "EuZ6",
        "family": "perpetual_quarterly_same_asset",
    },
    {
        "pair_id": "BTCUSDF/BTZ6",
        "near": "BTCUSDF",
        "far": "BTZ6",
        "family": "perpetual_quarterly_same_asset",
    },
    {
        "pair_id": "GLZ6/GLH7",
        "near": "GLZ6",
        "far": "GLH7",
        "family": "dated_calendar_same_asset",
    },
    {
        "pair_id": "CRZ6/CRH7",
        "near": "CRZ6",
        "far": "CRH7",
        "family": "dated_calendar_same_asset",
    },
    {
        "pair_id": "MXZ6/MMZ6",
        "near": "MXZ6",
        "far": "MMZ6",
        "family": "cross_contract_same_expiry",
    },
    {
        "pair_id": "GDZ6/GNZ6",
        "near": "GDZ6",
        "far": "GNZ6",
        "family": "cross_contract_same_expiry",
    },
)

PERPETUAL_HISTORY_STARTS = {
    "CNYRUBF": "2022-04-26",
    "GLDRUBF": "2023-07-11",
    "IMOEXF": "2023-11-14",
    "GAZPF": "2024-10-01",
    "SBERF": "2024-10-01",
    "RGBIF": "2025-12-23",
    "USDRUBF": "2022-04-26",
    "SLVRUBF": "2026-03-31",
    "EURRUBF": "2022-04-26",
    "BTCUSDF": "2026-09-22",
}
PERPETUAL_HISTORY_TILL = "2026-10-27"

HISTORICAL_FAR_BY_NEAR = {
    "CNYRUBF": (
        "CRH3",
        "CRH4",
        "CRH6",
        "CRM2",
        "CRM3",
        "CRM4",
        "CRM5",
        "CRM6",
        "CRU2",
        "CRU4",
        "CRU5",
        "CRU6",
        "CRZ2",
        "CRZ3",
        "CRZ4",
        "CRZ5",
    ),
    "IMOEXF": (
        "MMH4",
        "MMH5",
        "MMH6",
        "MMM4",
        "MMM5",
        "MMM6",
        "MMU4",
        "MMU5",
        "MMU6",
        "MMZ3",
        "MMZ4",
        "MMZ5",
    ),
    "GAZPF": ("GZH5", "GZH6", "GZM5", "GZM6", "GZU5", "GZU6", "GZZ4", "GZZ5"),
    "SBERF": ("SRH5", "SRH6", "SRM5", "SRM6", "SRU5", "SRU6", "SRZ4", "SRZ5"),
    "RGBIF": ("RBH6", "RBM6", "RBU6"),
    "USDRUBF": (
        "SiH3",
        "SiH4",
        "SiH5",
        "SiH6",
        "SiM2",
        "SiM3",
        "SiM4",
        "SiM5",
        "SiM6",
        "SiU2",
        "SiU3",
        "SiU4",
        "SiU5",
        "SiU6",
        "SiZ2",
        "SiZ3",
        "SiZ4",
        "SiZ5",
    ),
    "SLVRUBF": ("S2M6", "S2U6"),
    "EURRUBF": (
        "EuH3",
        "EuH4",
        "EuH5",
        "EuH6",
        "EuM2",
        "EuM3",
        "EuM4",
        "EuM5",
        "EuM6",
        "EuU2",
        "EuU3",
        "EuU4",
        "EuU5",
        "EuU6",
        "EuZ2",
        "EuZ3",
        "EuZ4",
        "EuZ5",
    ),
}

HISTORICAL_PERPETUAL_PAIRS = tuple(
    {
        "pair_id": f"{near}/{far}",
        "near": near,
        "far": far,
        "family": "perpetual_quarterly_same_asset",
    }
    for near, far_values in HISTORICAL_FAR_BY_NEAR.items()
    for far in far_values
)

GLD_HISTORICAL_FAR = (
    "GLU6",
    "GLM6",
    "GLH6",
    "GLZ5",
    "GLU5",
    "GLM5",
    "GLH5",
    "GLZ4",
    "GLU4",
    "GLM4",
    "GLH4",
    "GLZ3",
    "GLU3",
)
GLD_HISTORICAL_PAIRS = tuple(
    {
        "pair_id": f"GLDRUBF/{far}",
        "near": "GLDRUBF",
        "far": far,
        "family": "perpetual_quarterly_same_asset",
    }
    for far in GLD_HISTORICAL_FAR
)

BASE_PAIRS = (
    {
        "pair_id": "CNYRUBF/CRZ6",
        "near": "CNYRUBF",
        "far": "CRZ6",
        "family": "perpetual_quarterly_same_asset",
    },
    *SCREEN_PAIRS,
    *EXTRA_PAIRS,
)

PAIRS = (
    *BASE_PAIRS,
    *HISTORICAL_PERPETUAL_PAIRS,
    *GLD_HISTORICAL_PAIRS,
)

STRATEGIES = {
    "mean_reversion": {
        "label": "Возврат спреда к среднему",
        "parameters": {"lookback": 20, "entry_z": 2.0, "exit_z": 0.5},
    },
    "funding_only": {
        "label": "Фондирование перпетуального свопа (carry)",
        "parameters": {"lookback": 20},
    },
}

HISTORY_WINDOWS = {
    **{
        pair["pair_id"]: (
            PERPETUAL_HISTORY_STARTS.get(pair["near"], "2026-03-23"),
            PERPETUAL_HISTORY_TILL,
        )
        for pair in PAIRS
    },
    "CNYRUBF/CRZ6": ("2025-06-17", PERPETUAL_HISTORY_TILL),
    **{pair["pair_id"]: ("2023-07-11", PERPETUAL_HISTORY_TILL) for pair in GLD_HISTORICAL_PAIRS},
}

__all__ = [
    "BASE_PAIRS",
    "CALENDAR_UNIVERSE",
    "DEFAULT_INITIAL_CAPITAL_RUB",
    "EXTRA_PAIRS",
    "GLD_HISTORICAL_FAR",
    "GLD_HISTORICAL_PAIRS",
    "HISTORICAL_FAR_BY_NEAR",
    "HISTORICAL_PERPETUAL_PAIRS",
    "HISTORY_WINDOWS",
    "INITIAL_CAPITAL",
    "MAX_QUANTITY",
    "MIN_DAILY_VOLUME",
    "PAIRS",
    "PAIR_UNIVERSE",
    "PERPETUAL_HISTORY_STARTS",
    "PERPETUAL_HISTORY_TILL",
    "SCREEN_PAIRS",
    "STRATEGIES",
]
