"""Isolated regressions for the strict-typed pair screener package facade."""

from __future__ import annotations

import moex_spread_lab.strategies.pair_screener as facade
from moex_spread_lab.strategies.pair_screener import pair_screener, screener


def test_p72_packages_strategies_src_moex_spread_lab_strategies_pair_screener___init___reexports_identity() -> (
    None
):
    assert facade.SCREEN_SCHEMA is screener.SCREEN_SCHEMA
    assert facade.ScreenError is screener.ScreenError
    assert facade.screen_pairs is screener.screen_pairs
    assert facade.SCREEN_SCHEMA == "pair-screen.v1"


def test_p72_packages_strategies_src_moex_spread_lab_strategies_pair_screener___init___star_surface() -> (
    None
):
    assert set(pair_screener.__all__) <= set(vars(facade))
    for name in pair_screener.__all__:
        assert getattr(facade, name) is getattr(pair_screener, name)


def test_p72_packages_strategies_src_moex_spread_lab_strategies_pair_screener___init___screen_contract() -> (
    None
):
    assert facade.ScreenError is not screener.screen_pairs
    assert issubclass(facade.ScreenError, Exception)
    assert callable(facade.screen_pairs)
    assert facade.screen_pairs.__module__ == screener.__name__
