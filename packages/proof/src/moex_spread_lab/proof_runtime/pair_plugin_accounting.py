"""Experimental, isolated daily pair-plugin accounting composition.

This is not a proof route: catalog, journal-only bars, fixed-size execution,
price provenance, capital/volume screens, holdout orchestration and hourly bars
still require integration. Only trusted offline inputs are accepted.
"""
from __future__ import annotations

import math
from copy import deepcopy
from collections.abc import Mapping

from moex_spread_lab.accounting.position_sizing import size_proof
from moex_spread_lab.core.contracts import InstrumentContext, WindowSnapshot
from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.core.w0 import W0Error, _run_id
from moex_spread_lab.strategies.pair_screener.intents import pair_price_intent_trades
from .pair_funding_replay import attach_daily_unit_funding


def size_daily_pair_plugin(plugin, window, params, fill, funding_window, benchmark_marks, settings):
    """Validate decisions and size them against resolved funding and one account.

    Market rows are positive-volume synchronized analysis bars, not a complete
    proof journal. Inputs are not mutated; plugin-supplied money fields are ignored.
    Requires daily funding evidence, so pairs with no funding require a separate
    explicit accounting contract rather than an invented zero-rate window.
    """
    if (not isinstance(window, WindowSnapshot) or window.timeframe != "1d"
            or not isinstance(settings, ResearchSettings) or settings.timeframe != "1d"
            or not isinstance(params, dict) or not window.near or not window.far
            or window.near == window.far or not isinstance(benchmark_marks, Mapping)
            or not window.rows or any(not isinstance(row, Mapping) for row in window.rows)):
        raise W0Error("Для расчёта требуется корректное дневное окно пары.")
    context = InstrumentContext(universe=window.universe, pair=window.pair, timeframe=window.timeframe)
    validated = plugin.validate_parameters(deepcopy(params), context)
    if not isinstance(validated, dict):
        raise W0Error("Плагин вернул некорректные параметры.")
    verdict = plugin.check_compatibility(deepcopy(window), settings)
    if not verdict.compatible:
        raise W0Error(verdict.reason or "Стратегия несовместима с окном.")
    trades = pair_price_intent_trades(plugin, window, validated, fill)
    try:
        legs = [window.contracts[window.near], window.contracts[window.far]]
        def positive(value):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError
            return float(value)
        multipliers = [positive(c["tick_value"]) / positive(c["tick_size"]) for c in legs]
        lot = positive(legs[0]["lot_size"])
        if not all(math.isfinite(mult) and mult > 0 for mult in multipliers):
            raise ValueError
        rows = []
        for raw in window.rows:
            near, far = positive(raw["near_close"]), positive(raw["far_close"])
            near_open, far_open = positive(raw["near_open"]), positive(raw["far_open"])
            spread = near * multipliers[0] - far * multipliers[1]
            if not math.isfinite(spread) or not math.isfinite(near_open * multipliers[0] - far_open * multipliers[1]):
                raise ValueError
            positive(benchmark_marks[raw["date"]])
            rows.append({"date": raw["date"], "near": near, "far": far,
                         "near_open": near_open, "far_open": far_open,
                         "spread": round(spread, 8), "volume_eligible": True})
    except (TypeError, KeyError, ValueError, OverflowError, ZeroDivisionError):
        raise W0Error("Нет достоверных цен или параметров обеих ног пары.") from None
    if any(not math.isfinite(t[event]["spread_fill"]) for t in trades for event in ("entry", "exit")):
        raise W0Error("Переполнение цены исполнения пары.")
    prepared = attach_daily_unit_funding(rows, trades, funding_window, near_lot_size=lot)
    selection = {"universe": window.universe, "pair": window.pair, "strategy": plugin.strategy_id,
                 "timeframe": "1d", "fill": fill, "parameters": validated}
    try:
        identity = _run_id({"selection": selection, "series": prepared["series"], "trades": trades,
                            "contracts": window.contracts, "benchmark_marks": benchmark_marks,
                            "funding_provenance": prepared["provenance"]})
    except (TypeError, ValueError, OverflowError):
        raise W0Error("Некорректные данные идентичности расчёта.") from None
    unit = {"run_id": identity, "universe": window.universe, "selection": selection,
            "instrument": {"near": window.near, "far": window.far}, "contract": {},
            "window": {}, "series": prepared["series"], "trades": trades,
            "provenance": prepared["provenance"], "summary": {}, "notes": []}
    return size_proof(unit, benchmark_marks, settings, window.contracts)
