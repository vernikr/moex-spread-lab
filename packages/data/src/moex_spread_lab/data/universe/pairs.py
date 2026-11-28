"""Concrete MOEX perpetual pair catalog and historical observation windows."""

from __future__ import annotations

PERPETUAL = "perpetual_quarterly_same_asset"


def _pair(near: str, far: str, family: str = PERPETUAL) -> dict[str, str]:
    return {"pair_id": f"{near}/{far}", "near": near, "far": far, "family": family}


def _pairs(spec: str, family: str = PERPETUAL) -> tuple[dict[str, str], ...]:
    return tuple(_pair(*item.split("/"), family=family) for item in spec.split())


def _codes(spec: str) -> tuple[str, ...]:
    return tuple(spec.split())


SCREEN_PAIRS = _pairs("GLDRUBF/GLZ6 IMOEXF/MMZ6 GAZPF/GZZ6 SBERF/SRZ6 RGBIF/RBZ6 USDRUBF/SiZ6")
EXTRA_PAIRS = (
    *_pairs("SLVRUBF/S2Z6 EURRUBF/EuZ6 BTCUSDF/BTZ6"),
    *_pairs("GLZ6/GLH7 CRZ6/CRH7", "dated_calendar_same_asset"),
    *_pairs("MXZ6/MMZ6 GDZ6/GNZ6", "cross_contract_same_expiry"),
)
PERPETUAL_HISTORY_STARTS: dict[str, str] = {
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
HISTORICAL_FAR_BY_NEAR: dict[str, tuple[str, ...]] = {
    "CNYRUBF": _codes(
        "CRH3 CRH4 CRH5 CRH6 CRM2 CRM3 CRM4 CRM5 CRM6 CRU2 CRU3 CRU4 CRU5 CRU6 CRZ2 CRZ3 CRZ4 CRZ5"
    ),
    "IMOEXF": _codes("MMH4 MMH5 MMH6 MMM4 MMM5 MMM6 MMU4 MMU5 MMU6 MMZ3 MMZ4 MMZ5"),
    "GAZPF": _codes("GZH5 GZH6 GZM5 GZM6 GZU5 GZU6 GZZ4 GZZ5"),
    "SBERF": _codes("SRH5 SRH6 SRM5 SRM6 SRU5 SRU6 SRZ4 SRZ5"),
    "RGBIF": _codes("RBH6 RBM6 RBU6"),
    "USDRUBF": _codes(
        "SiH3 SiH4 SiH5 SiH6 SiM2 SiM3 SiM4 SiM5 SiM6 SiU2 SiU3 SiU4 SiU5 SiU6 SiZ2 SiZ3 SiZ4 SiZ5"
    ),
    "SLVRUBF": _codes("S2M6 S2U6"),
    "EURRUBF": _codes(
        "EuH3 EuH4 EuH5 EuH6 EuM2 EuM3 EuM4 EuM5 EuM6 EuU2 EuU3 EuU4 EuU5 EuU6 EuZ2 EuZ3 EuZ4 EuZ5"
    ),
}
HISTORICAL_PERPETUAL_PAIRS = tuple(
    _pair(near, far) for near, fars in HISTORICAL_FAR_BY_NEAR.items() for far in fars
)
GLD_HISTORICAL_FAR = _codes("GLU6 GLM6 GLH6 GLZ5 GLU5 GLM5 GLH5 GLZ4 GLU4 GLM4 GLH4 GLZ3 GLU3")
GLD_HISTORICAL_PAIRS = tuple(_pair("GLDRUBF", far) for far in GLD_HISTORICAL_FAR)
BASE_PAIRS = (_pair("CNYRUBF", "CRZ6"), *SCREEN_PAIRS, *EXTRA_PAIRS)
PAIRS = (*BASE_PAIRS, *HISTORICAL_PERPETUAL_PAIRS, *GLD_HISTORICAL_PAIRS)
HISTORY_WINDOWS: dict[str, tuple[str, str]] = {
    **{
        pair["pair_id"]: (
            PERPETUAL_HISTORY_STARTS.get(pair["near"], "2026-03-23"),
            PERPETUAL_HISTORY_TILL,
        )
        for pair in PAIRS
    },
    "CNYRUBF/CRZ6": ("2025-06-17", PERPETUAL_HISTORY_TILL),
}

__all__ = [
    "BASE_PAIRS",
    "EXTRA_PAIRS",
    "GLD_HISTORICAL_FAR",
    "GLD_HISTORICAL_PAIRS",
    "HISTORICAL_FAR_BY_NEAR",
    "HISTORICAL_PERPETUAL_PAIRS",
    "HISTORY_WINDOWS",
    "PAIRS",
    "PERPETUAL_HISTORY_STARTS",
    "PERPETUAL_HISTORY_TILL",
    "SCREEN_PAIRS",
]
