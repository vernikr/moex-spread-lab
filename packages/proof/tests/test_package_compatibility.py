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
    ],
)
def test_legacy_module_is_same_object(old, new):
    assert importlib.import_module(f"moex_spread_lab.{old}") is importlib.import_module(
        f"moex_spread_lab.proof.{new}"
    )


def test_builtin_replays_use_the_canonical_intent_pipeline():
    """#220 retired the service façade hooks; prepare_pair dispatches directly."""
    from unittest.mock import patch

    from moex_spread_lab.proof import carry_payload, pair_route

    assert pair_route.run_carry is carry_payload.run_carry
    spec = pair_route.ReplaySpec(
        inputs=None, pair={}, execution={}, params={}, terms={}, risk={}, prepared=()
    )
    with (
        patch.object(pair_route, "builtin_pair_proof") as mean_reversion,
        patch.object(pair_route, "run_carry") as funding,
    ):
        pair_route._replay_once(spec, True, metrics_only=False)
        pair_route._replay_once(spec, False, metrics_only=False)
    mean_reversion.assert_called_once()
    funding.assert_called_once()


@pytest.mark.parametrize(
    "module",
    [
        "moex_spread_lab.proof.screener_search",
        "moex_spread_lab.proof.web_legacy",
        "moex_spread_lab.proof.audit",
        "moex_spread_lab.proof.cli",
        "moex_spread_lab.proof_runtime",
    ],
)
def test_retired_proof_modules_stay_deleted(module):
    with pytest.raises(ModuleNotFoundError) as exc:
        importlib.import_module(module)
    assert exc.value.name == module


def test_retired_root_screener_search_shim_stays_deleted():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("moex_spread_lab.screener_search")


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
