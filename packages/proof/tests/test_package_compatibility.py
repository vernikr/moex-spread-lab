"""Legacy proof imports and extracted modules share identity for monkey-patches."""

import importlib

import pytest


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("proof", "service"),
        ("proof_kernel", "proof_kernel"),
        ("proof_search", "proof_search"),
        ("proof_batch", "proof_batch"),
        ("proof_strategy_search", "proof_strategy_search"),
        ("proof_windows", "proof_windows"),
        ("proof_pool", "proof_pool"),
        ("screener_search", "screener_search"),
        ("audit", "audit"),
    ],
)
def test_legacy_module_is_same_object(old, new):
    assert importlib.import_module(f"moex_spread_lab.{old}") is importlib.import_module(
        f"moex_spread_lab.proof_runtime.{new}"
    )
