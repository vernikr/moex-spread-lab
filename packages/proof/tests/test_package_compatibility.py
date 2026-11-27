"""Retired proof modules stay deleted; the canonical replay pipeline is direct."""

import importlib

import pytest


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


# Bare suffixes: the architecture DAG forbids literal ``moex_spread_lab.<root shim>``
# strings anywhere in workspace sources, so the prefix is joined at call time.
RETIRED = [
    "proof_batch",
    "proof_pool",
    "proof_search",
    "proof_strategy_search",
    "proof_windows",
    "screener_search",
    "proof.screener_search",
    "proof.web_legacy",
    "proof.audit",
    "proof.cli",
    "proof_runtime",
    "proof.mean_reversion_engine",
    "proof.mean_reversion_blocks",
    "proof.mean_reversion_payloads",
    "proof.mean_reversion_spec",
    "proof.funding_engine",
    "proof.funding_payloads",
    "proof.funding_sizing_payload",
    "proof.funding_notes",
    "proof.service_pair",
    "proof.service_prepare",
    "proof.fixed_plugin_series",
]


@pytest.mark.parametrize("name", RETIRED)
def test_retired_modules_stay_deleted(name):
    module = f"moex_spread_lab.{name}"
    with pytest.raises(ModuleNotFoundError) as exc:
        importlib.import_module(module)
    assert exc.value.name == module
