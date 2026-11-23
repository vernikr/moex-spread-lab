"""Regression coverage for ``proof_runtime.request_validation`` (Issue #95).

Unique-name suffix:
``p72_packages_proof_src_moex_spread_lab_proof_runtime_request_validation``

These tests lock the strict-typing refactor to the pre-existing contract: facade
identity, execution-axis selection, window identities, compact results with an
unchanged ``run_id``, option/parameter validation order and injected registries.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import Any, cast

import pytest

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING
from moex_spread_lab.core.contracts import StrategyPlugin
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE, PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof_runtime import (
    axis,
    cohort_prepare,
    proof_kernel,
    proof_search,
    request_validation,
)
from moex_spread_lab.proof_runtime.axis import Axis
from moex_spread_lab.proof_runtime.strategy_catalog import get_strategy_axes, get_strategy_registry
from moex_spread_lab.strategies import StrategyRegistry
from moex_spread_lab.strategies.calendar_zscore.plugin import CalendarZscorePlugin

__all__ = [
    "test_compact_result_preserves_payload_p72_packages_proof_src_moex_spread_lab_proof_runtime_request_validation",
    "test_execution_axes_preserves_automatic_sizing_and_identity_p72_packages_proof_src_moex_spread_lab_proof_runtime_request_validation",
    "test_facade_hooks_and_all_p72_packages_proof_src_moex_spread_lab_proof_runtime_request_validation",
    "test_identity_preserves_window_keys_and_validation_p72_packages_proof_src_moex_spread_lab_proof_runtime_request_validation",
    "test_validate_options_preserves_error_order_and_messages_p72_packages_proof_src_moex_spread_lab_proof_runtime_request_validation",
    "test_validate_parameters_preserves_axes_and_ordering_p72_packages_proof_src_moex_spread_lab_proof_runtime_request_validation",
    "test_validate_preserves_dispatch_and_registry_p72_packages_proof_src_moex_spread_lab_proof_runtime_request_validation",
]

_SUFFIX = "p72_packages_proof_src_moex_spread_lab_proof_runtime_request_validation"
_VALID_PARAMS: dict[str, Any] = {"window": 10, "entry_z": 2.0, "exit_z": 0.5}


def _valid_request() -> dict[str, Any]:
    return {"strategy": "zscore_mean_reversion", "parameters": dict(_VALID_PARAMS)}


def _custom_registry() -> StrategyRegistry:
    class CustomCalendar(CalendarZscorePlugin):
        strategy_id = f"test_calendar_{_SUFFIX}"
        family_id = f"test_calendar_{_SUFFIX}"
        proof_adapter = "calendar_price_intents.v1"

    plugin = cast(StrategyPlugin, CustomCalendar())
    plugins: dict[str, StrategyPlugin] = dict(get_strategy_registry().as_dict())
    plugins[plugin.strategy_id] = plugin
    return StrategyRegistry(plugins)


def test_facade_hooks_and_all_p72_packages_proof_src_moex_spread_lab_proof_runtime_request_validation():
    assert request_validation._ordered is axis._ordered
    assert cohort_prepare.execution_axes is request_validation.execution_axes
    for name in ("EXECUTION_AXES", "execution_axes", "_identity", "_validate", "compact_result"):
        assert getattr(proof_kernel, name) is getattr(request_validation, name)
        assert getattr(proof_search, name) is getattr(request_validation, name)
    expected = {
        "AUTO_SIZING",
        "Axis",
        "CALENDAR_UNIVERSE",
        "EXECUTION_AXES",
        "PAIR_UNIVERSE",
        "W0Error",
        "_identity",
        "_validate",
        "automatic",
        "compact_result",
        "execution_axes",
        "get_strategy_axes",
    }
    assert expected <= set(request_validation.__all__)
    assert all(hasattr(request_validation, name) for name in request_validation.__all__)


def test_execution_axes_preserves_automatic_sizing_and_identity_p72_packages_proof_src_moex_spread_lab_proof_runtime_request_validation():
    assert {
        "quantity": Axis(1, 50, 1),
        "slippage_ticks": Axis(0, 10, 0.5),
    } == request_validation.EXECUTION_AXES
    assert request_validation.execution_axes({"sizing": AUTO_SIZING}) == {}
    assert request_validation.execution_axes({"sizing": AUTO_SIZING}) is not (
        request_validation.EXECUTION_AXES
    )
    assert request_validation.execution_axes({}) is request_validation.EXECUTION_AXES
    assert request_validation.execution_axes({"sizing": "manual"}) is (
        request_validation.EXECUTION_AXES
    )


def test_identity_preserves_window_keys_and_validation_p72_packages_proof_src_moex_spread_lab_proof_runtime_request_validation():
    calendar = {"universe": CALENDAR_UNIVERSE, "pair": "SBER", "asset": "SBER"}
    assert request_validation._identity(calendar) == {
        "universe": CALENDAR_UNIVERSE,
        "pair": "SBER",
        "asset": "SBER",
    }
    assert request_validation._identity(MappingProxyType(calendar)) == calendar
    assert request_validation._identity({"pair": "SBER", "asset": "SBER"}) == calendar
    assert request_validation._identity({"universe": PAIR_UNIVERSE, "pair": "SBER"}) == {
        "universe": PAIR_UNIVERSE,
        "pair": "SBER",
    }
    for bad in (
        {"universe": "unknown", "pair": "SBER"},
        {"universe": CALENDAR_UNIVERSE, "pair": None},
        {"universe": CALENDAR_UNIVERSE, "pair": "SBER", "asset": 7},
        {"universe": CALENDAR_UNIVERSE, "pair": "SBER", "asset": "x" * 81},
    ):
        with pytest.raises(W0Error, match="Некорректный идентификатор окна"):
            request_validation._identity(bad)


def test_compact_result_preserves_payload_p72_packages_proof_src_moex_spread_lab_proof_runtime_request_validation():
    proof: dict[str, Any] = {
        "summary": {"trades": 3, "closed_trades": 2, "open_positions": 1},
        "selection": {"strategy": "zscore_mean_reversion"},
        "run_id": "run-123",
        "evaluation": {"result": {"annualized_excess_pp": 1.25}, "score": 0.5},
        "window": {"universe": PAIR_UNIVERSE, "pair": "SBER"},
    }
    before = deepcopy(proof)
    result = request_validation.compact_result(proof)
    assert result == {
        "status": "measured",
        "selection": proof["selection"],
        "run_id": "run-123",
        "trades": 3,
        "closed_trades": 2,
        "open_positions": 1,
        "annualized_excess_pp": 1.25,
        "evaluation": proof["evaluation"],
        "window": proof["window"],
        "execution_reference": False,
    }
    assert result["run_id"] is proof["run_id"]
    assert result["evaluation"] is proof["evaluation"]
    assert proof == before
    assert (
        request_validation.compact_result({**proof, "execution_reference": True})[
            "execution_reference"
        ]
        is True
    )
    no_excess = deepcopy(proof)
    no_excess["evaluation"] = {"result": {}}
    assert request_validation.compact_result(no_excess)["annualized_excess_pp"] is None
    assert request_validation.compact_result(MappingProxyType(proof))["run_id"] == "run-123"


def test_validate_options_preserves_error_order_and_messages_p72_packages_proof_src_moex_spread_lab_proof_runtime_request_validation():
    for valid in (
        {},
        {"sizing": None},
        {"sizing": AUTO_SIZING},
        {"optimize_strategy": False, "price_fill": "next_open"},
        {"optimize_strategy": True, "fill": "same_day_settlement"},
    ):
        assert request_validation._validate_options(valid) is None
    assert (
        request_validation._validate_options({"sizing": AUTO_SIZING, "slippage_ticks": 1.0}) is None
    )
    with pytest.raises(W0Error, match="optimize_strategy: укажите true или false"):
        request_validation._validate_options({"optimize_strategy": 1, "price_fill": "bad"})
    with pytest.raises(W0Error, match="Неизвестное исполнение ценовых стратегий"):
        request_validation._validate_options({"price_fill": "bad"})
    with pytest.raises(W0Error, match="Неизвестное исполнение"):
        request_validation._validate_options({"optimize_strategy": True, "fill": "bad"})
    with pytest.raises(W0Error, match="Неизвестный режим расчёта размера позиции"):
        request_validation._validate_options({"sizing": "manual"})
    with pytest.raises(W0Error, match="количество не задаётся вручную"):
        request_validation._validate_options({"sizing": AUTO_SIZING, "quantity": 1})


def test_validate_parameters_preserves_axes_and_ordering_p72_packages_proof_src_moex_spread_lab_proof_runtime_request_validation():
    registry = get_strategy_registry()
    assert get_strategy_axes(registry)["zscore_mean_reversion"]["window"].contains(10)
    assert request_validation._validate_parameters(_valid_request(), registry) is None
    with_execution = {**_valid_request(), "quantity": 5, "slippage_ticks": 1.0}
    assert request_validation._validate_parameters(with_execution, registry) is None
    with pytest.raises(W0Error, match="Неизвестная стратегия подбора"):
        request_validation._validate_parameters({"strategy": "unknown"}, registry)
    with pytest.raises(W0Error, match="Переданы параметры другой стратегии"):
        request_validation._validate_parameters(
            {"strategy": "zscore_mean_reversion", "parameters": []}, registry
        )
    with pytest.raises(W0Error, match="Переданы параметры другой стратегии"):
        request_validation._validate_parameters(
            {"strategy": "zscore_mean_reversion", "parameters": {"bogus": 1}}, registry
        )
    with pytest.raises(W0Error, match="допустимый диапазон"):
        request_validation._validate_parameters(
            {"strategy": "zscore_mean_reversion", "parameters": {"window": 100}}, registry
        )
    with pytest.raises(W0Error, match="допустимый диапазон"):
        request_validation._validate_parameters(
            {"strategy": "zscore_mean_reversion", "parameters": {"window": 10.5}}, registry
        )
    with pytest.raises(W0Error, match="Порог выхода должен быть ниже порога входа"):
        request_validation._validate_parameters(
            {
                "strategy": "zscore_mean_reversion",
                "parameters": {"window": 10, "entry_z": 1.0, "exit_z": 1.0},
            },
            registry,
        )


def test_validate_preserves_dispatch_and_registry_p72_packages_proof_src_moex_spread_lab_proof_runtime_request_validation(
    monkeypatch: pytest.MonkeyPatch,
):
    with pytest.raises(W0Error, match="Выбор должен быть объектом"):
        request_validation._validate(MappingProxyType(_valid_request()))
    assert request_validation._validate(_valid_request()) is None
    with pytest.raises(W0Error, match="optimize_strategy: укажите true или false"):
        request_validation._validate({**_valid_request(), "optimize_strategy": 1})
    with pytest.raises(W0Error, match="количество не задаётся вручную"):
        request_validation._validate({**_valid_request(), "sizing": AUTO_SIZING, "quantity": 1})
    registry = _custom_registry()
    injected = {"strategy": f"test_calendar_{_SUFFIX}", "parameters": {"window": 10}}
    assert request_validation._validate(injected, registry=registry) is None
    with pytest.raises(W0Error, match="Неизвестная стратегия подбора"):
        request_validation._validate(injected)

    def always_unordered(_params: Mapping[str, Any]) -> bool:
        return False

    monkeypatch.setattr(request_validation, "_ordered", always_unordered)
    with pytest.raises(W0Error, match="Порог выхода должен быть ниже порога входа"):
        request_validation._validate(_valid_request())
