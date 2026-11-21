"""Fail-closed validation of funding-carry strategy intents.

This validates decisions only; proof separately owns evidence and accounting.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from datetime import date

from moex_spread_lab.core.contracts import StrategyIntentRun, WindowSnapshot
from moex_spread_lab.core.universe import PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error


def carry_settlement_intents(plugin, window: WindowSnapshot, params: dict) -> list[dict]:
    """Validate short-near/long-far settlement decisions against the study prefix.

    Prices, funding, costs and P&L supplied by the plugin are ignored. The caller
    still must resolve funding evidence and enforce expiry/assignment and account
    constraints; this function is only one part of the proof adapter.
    """
    if (not isinstance(window, WindowSnapshot) or window.universe != PAIR_UNIVERSE
            or window.timeframe != "1d" or "same_day_settlement" not in plugin.supported_fills
            or not isinstance(params, dict) or not isinstance(window.carry_study, Mapping)):
        raise W0Error("Некорректное окно или исполнение carry-стратегии.")
    rows = window.carry_study.get("rows")
    if not isinstance(rows, list) or not rows or any(not isinstance(r, Mapping) for r in rows):
        raise W0Error("Нет пригодного ряда расчётных цен carry.")
    try:
        days = [r["date"] for r in rows]
        if (any(not isinstance(d, str) or len(d) != 10 or date.fromisoformat(d).isoformat() != d for d in days)
                or days != sorted(set(days))):
            raise ValueError
        for row in rows:
            for key in ("near_settle", "far_settle"):
                value = row[key]
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise ValueError
    except (ValueError, TypeError, OverflowError, KeyError):
        raise W0Error("Некорректные расчётные цены или порядок дат carry.") from None
    indices = {d: i for i, d in enumerate(days)}
    result = plugin.evaluate_intents(deepcopy(window), deepcopy(params), "same_day_settlement")
    if (not isinstance(result, StrategyIntentRun) or result.strategy_id != plugin.strategy_id
            or result.fill != "same_day_settlement" or result.parameters != params
            or not isinstance(result.intents, tuple)):
        raise W0Error("Плагин вернул несогласованный набор сигналов carry.")
    trades = []
    previous_exit = -1
    for item in result.intents:
        if not isinstance(item, Mapping):
            raise W0Error("Некорректная сделка carry.")
        try:
            entry, exit_, signal = (indices[item[k]] for k in ("entry_date", "exit_date", "signal_date"))
            side, reason, terminal = item["side"], item["reason"], item["open_at_end"]
        except (KeyError, TypeError):
            raise W0Error("Даты или поля carry-сделки отсутствуют в окне.") from None
        if (side != "short_spread" or type(terminal) is not bool or not isinstance(reason, str) or not reason
                or signal > entry or entry >= exit_ or entry <= previous_exit
                or (terminal and exit_ != len(days) - 1)
                or (not terminal and reason == "open_at_data_end_no_executable_exit")):
            raise W0Error("Нарушен порядок или направление carry-сделки.")
        for i in (signal, entry, exit_):
            if i == exit_ and terminal:
                continue
            for leg in ("near_volume", "far_volume"):
                value = rows[i].get(leg)
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise W0Error("Недостаточные данные объёмов для carry-сделки.")
        entry_reason = item.get("entry_reason")
        if entry_reason is not None and (not isinstance(entry_reason, str) or not entry_reason):
            raise W0Error("Некорректная причина входа carry.")
        trades.append({"side": "short_spread", "entry_date": days[entry], "exit_date": days[exit_],
                       "signal_date": days[signal], "reason": reason, "open_at_end": terminal,
                       **({"entry_reason": entry_reason} if entry_reason is not None else {})})
        previous_exit = exit_
    return trades
