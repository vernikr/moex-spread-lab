"""``calendar.contracts`` keeps re-exporting the Layer-1 asset catalog (issue #16)."""

from __future__ import annotations

from moex_spread_lab.data import universe as data_universe
from moex_spread_lab.strategies.calendar import contracts


def test_asset_catalog_is_reexported_not_redefined() -> None:
    for name in (
        "ASSETS",
        "CROSS_EXPIRY_PAIRS",
        "FX_SOURCE",
        "MONTH_CODES",
        "QUARTER_CODES",
        "SYNTHETIC_TRIANGLE_PAIRS",
        "contract_chain",
    ):
        assert getattr(contracts, name) is getattr(data_universe, name), name


def test_contract_chain_matches_the_data_catalog() -> None:
    assert contracts.contract_chain("SR") == data_universe.contract_chain("SR")
    assert contracts.contract_chain("BR", 4, 5) == data_universe.contract_chain("BR", 4, 5)
