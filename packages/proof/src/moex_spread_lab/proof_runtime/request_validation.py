"""Validate search requests and build compact window identities and results."""

from __future__ import annotations

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING, automatic
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE, PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error

from .axis import Axis, _ordered
from .strategy_catalog import get_strategy_axes

# Fixed-size API/CLI compatibility only. The workbench uses automatic sizing and
# one immutable global slippage preference, so neither is an optimization axis.
EXECUTION_AXES = {"quantity": Axis(1, 50, 1), "slippage_ticks": Axis(0, 10, 0.5)}


def execution_axes(request):
    return {} if automatic(request) else EXECUTION_AXES


def _identity(request):
    u = request.get("universe", CALENDAR_UNIVERSE)
    t = {"universe": u, "pair": request.get("pair")}
    if u == CALENDAR_UNIVERSE:
        t["asset"] = request.get("asset")
    if u not in (PAIR_UNIVERSE, CALENDAR_UNIVERSE) or not all(
        isinstance(v, str) and 0 < len(v) <= 80 for v in t.values()
    ):
        raise W0Error("Некорректный идентификатор окна.")
    return t


def compact_result(proof):
    summary = proof["summary"]
    return {
        "status": "measured",
        "selection": proof["selection"],
        "run_id": proof["run_id"],
        "trades": summary["trades"],
        "closed_trades": summary["closed_trades"],
        "open_positions": summary["open_positions"],
        "annualized_excess_pp": proof["evaluation"]["result"].get("annualized_excess_pp"),
        "evaluation": proof["evaluation"],
        "window": proof["window"],
        "execution_reference": proof.get("execution_reference", False),
    }


def _validate_options(request):
    if "optimize_strategy" in request and type(request["optimize_strategy"]) is not bool:
        raise W0Error("optimize_strategy: укажите true или false.")
    if "price_fill" in request and request["price_fill"] not in (
        "next_open",
        "next_close",
        "reference_same_bar",
    ):
        raise W0Error("Неизвестное исполнение ценовых стратегий.")
    if request.get("optimize_strategy") and request.get("fill", "reference_same_bar") not in (
        "next_open",
        "next_close",
        "reference_same_bar",
        "same_day_settlement",
    ):
        raise W0Error("Неизвестное исполнение.")
    if request.get("sizing") not in (None, AUTO_SIZING):
        raise W0Error("Неизвестный режим расчёта размера позиции.")
    if automatic(request) and request.get("quantity") is not None:
        raise W0Error("При автоматическом размере количество не задаётся вручную.")


def _validate_parameters(request, registry):
    strategy = request.get("strategy", "zscore_mean_reversion")
    strategy_axes = get_strategy_axes(registry)
    if not isinstance(strategy, str) or strategy not in strategy_axes:
        raise W0Error("Неизвестная стратегия подбора")
    params = request.get("parameters", {})
    axes = strategy_axes[strategy]
    if not isinstance(params, dict) or params.keys() - axes.keys():
        raise W0Error("Переданы параметры другой стратегии")
    domains = axes | EXECUTION_AXES
    values = {
        **params,
        **{
            k: request[k]
            for k in EXECUTION_AXES
            if k in request and (not automatic(request) or k != "quantity")
        },
    }
    for name, value in values.items():
        axis = domains[name]
        if not axis.contains(value):
            raise W0Error(f"{name}: допустимый диапазон {axis.low}–{axis.high}")
    if not _ordered(params):
        raise W0Error("Порог выхода должен быть ниже порога входа")


def _validate(request, *, registry=None):
    if not isinstance(request, dict):
        raise W0Error("Выбор должен быть объектом.")
    _validate_options(request)
    _validate_parameters(request, registry)
