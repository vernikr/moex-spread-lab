"""Validated pair-plugin unit intents and funded sizing for proof replays.

Daily perpetual windows require near-leg funding evidence; hourly windows follow
native proof policy and explicitly exclude daily funding, not interpolate it.
Only trusted offline inputs are accepted.
"""
from __future__ import annotations

import math
from copy import deepcopy
from collections.abc import Mapping
from datetime import date, datetime

from moex_spread_lab.accounting.position_sizing import size_proof
from moex_spread_lab.core.contracts import InstrumentContext, WindowSnapshot
from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.core.w0 import W0Error, _run_id
from moex_spread_lab.strategies.pair_screener.intents import pair_price_intent_trades
from .pair_funding_replay import attach_daily_unit_funding


def prepare_daily_pair_unit(plugin, window, params, fill, funding_window, benchmark_marks, settings, *, through=None):
    """Validate decisions and size them against resolved funding and one account.

    Zero-volume rows stay in the journal without revaluation or funding. Only the
    eligible prefix reaches the plugin; no later bars or benchmark marks are used.
    Inputs are not mutated; plugin-supplied money fields are ignored.
    Daily perpetual funding evidence must match the near contract. Missing daily
    evidence is allowed only for registry-confirmed non-perpetual pairs; hourly
    prices follow native proof and omit funding rather than invent clearing times.
    """
    if (not isinstance(window, WindowSnapshot) or window.timeframe not in ("1d", "1h")
            or not isinstance(settings, ResearchSettings) or settings.timeframe != window.timeframe
            or not isinstance(params, dict) or not window.near or not window.far
            or window.near == window.far or not isinstance(benchmark_marks, Mapping)
            or not window.rows or any(not isinstance(row, Mapping) for row in window.rows)):
        raise W0Error("Для расчёта требуется корректное дневное окно пары.")
    try:
        dates = [row["date"] for row in window.rows]
        def valid_key(key, daily):
            if not isinstance(key, str):
                return False
            if daily:
                return len(key) == 10 and date.fromisoformat(key).isoformat() == key
            return (len(key) == 19 and datetime.fromisoformat(key).isoformat(timespec="seconds") == key)
        if (any(not valid_key(day, window.timeframe == "1d") for day in dates)
                or dates != sorted(set(dates))
                or (through is not None and not valid_key(through, len(through) == 10))):
            raise ValueError
        prefix = [row for row in window.rows if through is None or
                  (row["date"][:10] if len(through) == 10 else row["date"]) <= through]
        def eligible(row):
            values = [row.get("near_volume"), row.get("far_volume")]
            if any(v is not None and (type(v) not in (int, float) or not math.isfinite(v) or v < 0) for v in values):
                raise ValueError
            return all(v is not None and v > 0 for v in values)
        analysis_rows = [row for row in prefix if eligible(row)]
        if len(analysis_rows) < 2:
            raise ValueError
    except (ValueError, KeyError, TypeError, OverflowError):
        raise W0Error("Некорректный дневной период или объём пары.") from None
    # Do not hand a plugin the original snapshot: benchmark/funding/extra fields
    # may contain future observations or unvalidated external data.
    analysis_window = WindowSnapshot(
        universe=window.universe, pair=window.pair, timeframe=window.timeframe, asset=window.asset,
        rows=deepcopy(analysis_rows), contracts=deepcopy(window.contracts),
        near=window.near, far=window.far,
        initial_capital_rub=settings.initial_capital_rub, slippage_ticks=settings.slippage_ticks,
    )
    context = InstrumentContext(universe=window.universe, pair=window.pair, timeframe=window.timeframe)
    validated = plugin.validate_parameters(deepcopy(params), context)
    if not isinstance(validated, dict):
        raise W0Error("Плагин вернул некорректные параметры.")
    verdict = plugin.check_compatibility(deepcopy(analysis_window), settings)
    if not verdict.compatible:
        raise W0Error(verdict.reason or "Стратегия несовместима с окном.")
    trades = pair_price_intent_trades(plugin, analysis_window, validated, fill)
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
        for raw in prefix:
            near, far = positive(raw["near_close"]), positive(raw["far_close"])
            near_open, far_open = positive(raw["near_open"]), positive(raw["far_open"])
            spread = near * multipliers[0] - far * multipliers[1]
            if not math.isfinite(spread) or not math.isfinite(near_open * multipliers[0] - far_open * multipliers[1]):
                raise ValueError
            positive(benchmark_marks[raw["date"]])
            rows.append({"date": raw["date"], "timestamp": raw.get("timestamp", raw["date"]),
                         "session_date": raw.get("session_date", raw["date"]),
                         "benchmark_mark_date": raw.get("benchmark_mark_date"), "near": near, "far": far,
                         "near_open": near_open, "far_open": far_open,
                         "spread": round(spread, 8), "volume_eligible": eligible(raw)})
    except (TypeError, KeyError, ValueError, OverflowError, ZeroDivisionError):
        raise W0Error("Нет достоверных цен или параметров обеих ног пары.") from None
    if any(not math.isfinite(t[event]["spread_fill"]) for t in trades for event in ("entry", "exit")):
        raise W0Error("Переполнение цены исполнения пары.")
    if window.timeframe == "1h":
        if funding_window is not None:
            raise W0Error("Дневную ставку нельзя применять к каждому часовому бару.")
        from moex_spread_lab.strategies.pair_screener.pair_screener import _pair
        declared = _pair(window.pair)
        if declared["near"] != window.near or declared["far"] != window.far:
            raise W0Error("Идентификатор пары не совпадает с ногами.")
        prepared = {"provenance": {"funding_applicability": "intraday_not_included"}}
        journal = [{**row, "funding_rub": None, "funding_contracts": 0} for row in rows]
    elif funding_window is None:
        # Only known dated-contract pairs may omit rates; a missing perpetual
        # funding source must not silently become zero.
        from moex_spread_lab.strategies.pair_screener.pair_screener import _pair
        declared = _pair(window.pair)
        if (declared["near"] != window.near or declared["far"] != window.far
                or declared["family"] not in {"dated_calendar_same_asset", "cross_contract_same_expiry"}):
            raise W0Error("Для вечного фьючерса требуется история фондирования.")
        prepared = {"provenance": {"funding_applicability": "no_perpetual_leg"}}
        journal = [{**row, "funding_rub": None, "funding_contracts": 0} for row in rows]
    else:
        from moex_spread_lab.strategies.funding_carry.proof_funding import FundingWindow
        from moex_spread_lab.core.universe import PAIRS
        declared = next((p for p in PAIRS if p["pair_id"] == window.pair), None)
        if declared is not None and (declared["near"] != window.near or declared["far"] != window.far
                                     or declared["family"] != "perpetual_quarterly_same_asset"):
            raise W0Error("История фондирования неприменима к этой паре.")
        if not isinstance(funding_window, FundingWindow) or funding_window.book.secid != window.near:
            raise W0Error("История фондирования не соответствует ближней ноге пары.")
        eligible_rows = [row for row in rows if row["volume_eligible"]]
        prepared = attach_daily_unit_funding(eligible_rows, trades, funding_window, through=through, near_lot_size=lot)
        by_date = {row["date"]: row for row in prepared["series"]}
        journal = [by_date.get(row["date"], {**row, "funding_rub": None, "funding_contracts": 0}) for row in rows]
    marks = {row["date"]: benchmark_marks[row["date"]] for row in rows}
    selection = {"universe": window.universe, "pair": window.pair, "strategy": plugin.strategy_id,
                 "timeframe": window.timeframe, "fill": fill, "parameters": validated}
    try:
        identity = _run_id({"selection": selection, "series": journal, "trades": trades,
                            "contracts": window.contracts, "benchmark_marks": marks,
                            "funding_provenance": prepared["provenance"]})
    except (TypeError, ValueError, OverflowError):
        raise W0Error("Некорректные данные идентичности расчёта.") from None
    unit = {"run_id": identity, "universe": window.universe, "selection": selection,
            "instrument": {"near": window.near, "far": window.far}, "contract": {},
            "window": {}, "series": journal, "trades": trades,
            "provenance": prepared["provenance"], "summary": {}, "notes": []}
    return unit, marks


def size_daily_pair_plugin(plugin, window, params, fill, funding_window, benchmark_marks, settings, *, through=None):
    unit, marks = prepare_daily_pair_unit(plugin, window, params, fill, funding_window, benchmark_marks, settings, through=through)
    return size_proof(unit, marks, settings, window.contracts)
