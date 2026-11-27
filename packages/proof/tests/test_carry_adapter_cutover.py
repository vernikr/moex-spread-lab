"""The built-in carry engine proves itself through ``carry_settlement_intents.v1``."""

from __future__ import annotations

from typing import Any

from moex_spread_lab.proof.pair_route import _validate_carry_result
from moex_spread_lab.proof.strategy_catalog import get_proof_adapter_ids
from moex_spread_lab.strategies.base import StrategyRegistry
from moex_spread_lab.strategies.funding_carry.plugin import FundingCarryPlugin


def _result() -> dict[str, Any]:
    return {"summary": {"ledger_reconciliation_rub": 0.0}}


def test_builtin_carry_is_discovered_as_an_adapted_strategy() -> None:
    from moex_spread_lab.strategies.base import carry_proof_plugin_ids

    registry = StrategyRegistry({"funding_only": FundingCarryPlugin()})
    assert carry_proof_plugin_ids(registry) == {"funding_only"}
    assert "funding_only" in get_proof_adapter_ids(registry)


def test_builtin_carry_keeps_its_native_payload_shape() -> None:
    result = _result()
    _validate_carry_result(result, FundingCarryPlugin(), metrics_only=False)
    assert "accounting_policy" not in result


def test_drop_in_carry_marks_the_settlement_accounting() -> None:
    class DropIn(FundingCarryPlugin):
        strategy_id = "drop_in_carry"
        family_id = "drop_in_carry"

    result = _result()
    _validate_carry_result(result, DropIn(), metrics_only=False)
    assert result["accounting_policy"] == "carry_settlement.v1"
