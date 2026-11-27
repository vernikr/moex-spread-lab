"""Compatibility and dependency injection for extracted request validation."""

from typing import Any, cast

import pytest

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof import proof_kernel, proof_search, request_validation
from moex_spread_lab.proof.strategy_catalog import get_strategy_registry
from moex_spread_lab.strategies import StrategyRegistry
from moex_spread_lab.strategies.calendar_zscore.plugin import CalendarZscorePlugin


def test_request_helpers_remain_reexported():
    for name in ("EXECUTION_AXES", "execution_axes", "_identity", "_validate", "compact_result"):
        assert getattr(proof_kernel, name) is getattr(request_validation, name.lstrip("_"))
        assert getattr(proof_search, name) is getattr(request_validation, name.lstrip("_"))


def test_validation_uses_injected_registry_and_rejects_invalid_options_first():
    class CustomCalendar(CalendarZscorePlugin):
        strategy_id = "test_calendar"
        family_id = "test_calendar"
        proof_adapter = "calendar_price_intents.v1"

    plugin = CustomCalendar()
    registry = StrategyRegistry(
        {**get_strategy_registry().as_dict(), plugin.strategy_id: cast(Any, plugin)}
    )
    request = {"strategy": plugin.strategy_id, "parameters": {"window": 10}}
    request_validation.validate(request, registry=registry)
    with pytest.raises(W0Error, match="Неизвестная стратегия"):
        request_validation.validate(request)
    with pytest.raises(W0Error, match="optimize_strategy"):
        request_validation.validate({**request, "optimize_strategy": 1}, registry=registry)
    with pytest.raises(W0Error, match="количество"):
        request_validation.validate(
            {**request, "sizing": AUTO_SIZING, "quantity": 1}, registry=registry
        )
    with pytest.raises(W0Error, match="Порог выхода"):
        request_validation.validate(
            {**request, "parameters": {"window": 10, "entry_z": 1.0, "exit_z": 1.0}},
            registry=registry,
        )
