"""MOEX instrument (asset) metadata, contract naming, and calendar pair specs."""

from __future__ import annotations

from typing import Any

QUARTER_CODES = "HMUZ"
MONTH_CODES = "FGHJKMNQUVXZ"
ASSETS: dict[str, dict[str, Any]] = {
    "SR": {"group": "stock", "convergence": True, "name": "SBRF"},
    "GZ": {"group": "stock", "convergence": True, "name": "GAZR"},
    "LK": {"group": "stock", "name": "LKOH"},
    "RN": {"group": "stock", "name": "ROSN"},
    "SP": {"group": "stock", "name": "SBPR"},
    "MG": {"group": "stock", "name": "MGNT"},
    "YD": {"group": "stock", "name": "YDEX"},
    "AL": {"group": "stock", "name": "ALRS"},
    "TN": {"group": "stock", "name": "TATN"},
    "SG": {"group": "stock", "name": "SNGP"},
    "MM": {"group": "index", "name": "MXI (mini IMOEX)"},
    "MX": {"group": "index", "name": "MIX (IMOEX)"},
    "RI": {"group": "index", "fx": "usd", "name": "RTS"},
    "RB": {"group": "interest", "name": "RGBI"},
    "Si": {"group": "currency", "name": "USD/RUB"},
    "Eu": {"group": "currency", "name": "EUR/RUB"},
    "CR": {"group": "currency", "name": "CNY/RUB"},
    "ED": {"group": "currency", "fx": "usd", "name": "EUR/USD"},
    "UC": {"group": "currency", "fx": "cny", "name": "USD/CNY"},
    "GD": {"group": "commodity", "fx": "usd", "name": "GOLD"},
    "GN": {"group": "commodity", "fx": "usd", "name": "GOLDM"},
    "PT": {"group": "commodity", "fx": "usd", "name": "PLT"},
    "SV": {"group": "commodity", "fx": "usd", "name": "SILV"},
    "BR": {"group": "commodity", "fx": "usd", "monthly": True, "name": "Brent"},
    "NG": {"group": "commodity", "fx": "usd", "monthly": True, "name": "Natural gas"},
    "WT": {"group": "commodity", "fx": "usd", "monthly": True, "name": "WTI"},
}
FX_SOURCE: dict[str, str] = {"usd": "Si", "cny": "CR"}
CROSS_EXPIRY_PAIRS: tuple[dict[str, str], ...] = (
    {
        "pair_id": "MX/MM",
        "near": "MX",
        "far": "MM",
        "family": "cross_contract_same_expiry",
        "name": "MIX vs MXI",
    },
    {
        "pair_id": "GD/GN",
        "near": "GD",
        "far": "GN",
        "family": "cross_contract_same_expiry",
        "name": "GOLD vs GOLDM",
    },
)
SYNTHETIC_TRIANGLE_PAIRS: tuple[dict[str, Any], ...] = (
    {
        "pair_id": "Si+Eu/ED",
        "legs": ("Si", "Eu", "ED"),
        "family": "synthetic_triangle_same_expiry",
        "name": "USD/RUB + EUR/RUB vs EUR/USD",
    },
    {
        "pair_id": "Si+CR/UC",
        "legs": ("Si", "CR", "UC"),
        "family": "synthetic_triangle_same_expiry",
        "name": "USD/RUB + CNY/RUB vs USD/CNY",
    },
)


def contract_chain(asset: str, first_year: int = 3, last_year: int = 7) -> list[str]:
    monthly = ASSETS.get(asset, {}).get("monthly", False)
    codes, last = (MONTH_CODES, "F") if monthly else (QUARTER_CODES, "H")
    chain = [f"{asset}{code}{year}" for year in range(first_year, last_year + 1) for code in codes]
    return chain[: chain.index(f"{asset}{last}{last_year}") + 1]


__all__ = [
    "ASSETS",
    "CROSS_EXPIRY_PAIRS",
    "FX_SOURCE",
    "MONTH_CODES",
    "QUARTER_CODES",
    "SYNTHETIC_TRIANGLE_PAIRS",
    "contract_chain",
]
