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


def test_extracted_strict_helpers_preserve_private_aliases():
    from moex_spread_lab.proof_runtime import (
        fixed_plugin_payload,
        fixed_plugin_series,
        mean_reversion_engine,
        mean_reversion_payloads,
    )

    assert fixed_plugin_series.scaled_series is fixed_plugin_series._scaled_series
    assert fixed_plugin_payload._scaled_series is fixed_plugin_series._scaled_series
    assert mean_reversion_engine.resolve_window is mean_reversion_engine._resolve_window
    assert mean_reversion_payloads._resolve_window is mean_reversion_engine._resolve_window
