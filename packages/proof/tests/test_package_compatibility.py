"""Legacy proof imports and extracted modules share identity for monkey-patches."""

import importlib

import pytest


@pytest.mark.parametrize(
    ("old", "new"),
    [
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
        f"moex_spread_lab.proof.{new}"
    )


@pytest.mark.parametrize(
    "module",
    [
        "service",
        "proof_kernel",
        "proof_search",
        "proof_batch",
        "proof_pool",
        "proof_windows",
        "audit",
        "screener_search",
        "calendar_adapter",
        "cohort_models",
    ],
)
def test_proof_runtime_alias_keeps_one_identity(module):
    """The pre-``proof`` namespace stays importable during the migration window."""
    canonical = importlib.import_module(f"moex_spread_lab.proof.{module}")
    alias = importlib.import_module(f"moex_spread_lab.proof_runtime.{module}")
    assert alias is canonical


def test_proof_runtime_alias_resolves_from_imports():
    from moex_spread_lab.proof_runtime import proof_kernel, service

    assert service is importlib.import_module("moex_spread_lab.proof.service")
    assert proof_kernel is importlib.import_module("moex_spread_lab.proof.proof_kernel")


def test_builtin_replays_use_the_canonical_intent_pipeline():
    from moex_spread_lab.proof import carry_payload, pair_route, service

    assert service.run_mean_reversion is pair_route.builtin_pair_proof
    assert service.run_funding is carry_payload.run_carry


@pytest.mark.parametrize(
    "module",
    [
        "mean_reversion_engine",
        "mean_reversion_blocks",
        "mean_reversion_payloads",
        "mean_reversion_spec",
        "funding_engine",
        "funding_payloads",
        "funding_sizing_payload",
        "funding_notes",
        "service_pair",
        "service_prepare",
        "fixed_plugin_series",
    ],
)
def test_retired_legacy_replay_modules_stay_deleted(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f"moex_spread_lab.proof.{module}")
