"""Mean-reversion payload parts derived from one cut-off window.

Trades, chart/series rows, selection, instrument and notes are pure mappings over the
window the engine resolved; splitting them out keeps the engine under the code-health
file ceilings and lets the payload module import only what it publishes.
"""

from __future__ import annotations

from typing import Any

from moex_spread_lab.core.timeframes import get_timeframe
from moex_spread_lab.core.universe import MIN_DAILY_VOLUME, PAIR_UNIVERSE
from moex_spread_lab.strategies import REFERENCE_WARNING
from moex_spread_lab.strategies.pair_screener.pair_screener import _session_volumes

from .mean_reversion_spec import (
    CARRY_FILL_NOTE,
    OPEN_POSITION_NOTE,
    SPREAD_NOTES,
    VOLUME_RULE_NOTE,
    MeanReversionWindow,
)
from .payload_tools import TradeSpec, fill_block, positive_pair_volume, rel_pct, trade_block


def sessions_for(window: MeanReversionWindow) -> dict[str, Any]:
    """Per-session volumes of both legs, cached for the whole request."""
    spec = window.spec
    near, far = spec.pair["near"], spec.pair["far"]

    def build() -> dict[str, Any]:
        return _session_volumes(spec.inputs, spec.pair, window.series["timeframe"]) or {
            near: {},
            far: {},
        }

    if spec.cache is None:
        return build()
    if "sessions" not in spec.cache:
        spec.cache["sessions"] = build()
    return spec.cache["sessions"]


def window_meta(window: MeanReversionWindow) -> dict[str, Any]:
    """Liquidity screen for the current cut-off: the smallest min-leg session volume."""
    sessions = sessions_for(window)
    near, far = window.spec.pair["near"], window.spec.pair["far"]
    near_volumes, far_volumes = sessions.get(near, {}), sessions.get(far, {})
    volumes: list[float] = []
    for row in window.series["rows"]:
        session_date = row.get("session_date", row["date"][:10])
        near_volume, far_volume = near_volumes.get(session_date), far_volumes.get(session_date)
        if near_volume is None or far_volume is None:
            continue
        if float(near_volume) > 0 and float(far_volume) > 0:
            volumes.append(min(float(near_volume), float(far_volume)))
    smallest = min(volumes) if volumes else None
    return {
        "min_leg_volume": smallest,
        "tradable_every_day": bool(smallest is not None and smallest >= MIN_DAILY_VOLUME),
    }


def side(row: dict[str, Any]) -> str:
    return "long_spread" if row["direction"] == "long_near_short_far" else "short_spread"


def stamp(value: str, intraday: bool) -> str:
    return value[:19].replace(" ", "T") if intraday else value[:10]


def trades_for(window: MeanReversionWindow, report: dict[str, Any]) -> list[dict[str, Any]]:
    """Fixed-quantity trade rows; fills are looked up in the cut price rows."""
    spec, execution = window.spec, window.spec.execution
    intraday = get_timeframe(window.series["timeframe"]).intraday
    trades: list[dict[str, Any]] = []
    for number, row in enumerate(report["trades"], start=1):
        entry_date = stamp(row["entry_date"], intraday)
        exit_date = stamp(row["exit_date"], intraday)
        fills = {
            "entry": fill_block(
                entry_date,
                row["entry_spread_fill"],
                row["entry_near_fill"],
                row["entry_far_fill"],
                window.volume_map,
                row.get("entry_costs") or {},
                far_value=row["entry_far_fill"] * window.far_mult,
            ),
            "exit": fill_block(
                exit_date,
                row["exit_spread_fill"],
                row["exit_near_fill"],
                row["exit_far_fill"],
                window.volume_map,
                row.get("exit_costs") or {},
                far_value=row["exit_far_fill"] * window.far_mult,
            ),
        }
        trades.append(
            trade_block(
                TradeSpec(
                    number=number,
                    side=side(row),
                    quantity=int(execution["quantity"]),
                    signal={
                        "kind": "z",
                        "value": row.get("entry_signal_z"),
                        "date": window.signal_dates[entry_date],
                    },
                    entry=fills["entry"],
                    exit=fills["exit"],
                    exit_reason=row["exit_reason"],
                    margin_pair=window.margin_pair,
                    gross_rub=float(row["gross_pnl"]),
                    costs_rub=float(row["modeled_costs"] or 0.0),
                    net_rub=float(row["net_pnl"] or 0.0),
                    funding_rub=float(row.get("funding_rub") or 0.0)
                    if window.funding_swaprates
                    else None,
                ),
                benchmark_dates=spec.benchmark_dates or [],
                benchmark_prices=spec.benchmark_prices or {},
            )
        )
    return trades


def holding_flags(
    days: list[str], daily_rows: list[dict[str, Any]]
) -> dict[str, tuple[dict[str, Any], int, int]]:
    """Daily position state: (row, position, contracts held for clearing) keyed by date."""
    daily_map = {row["date"]: row for row in daily_rows}
    state: dict[str, tuple[dict[str, Any], int, int]] = {}
    carried = 0
    for day in days:
        daily = daily_map.get(day, {})
        if day in daily_map:
            carried = daily.get("position") or 0
        position = daily.get("position", carried) or 0
        clearing = bool(position) or daily.get("action") == "forced_exit_end_of_sample"
        state[day] = (daily, position, int(clearing))
    return state


def series_rows_for(window: MeanReversionWindow, report: dict[str, Any]) -> list[dict[str, Any]]:
    """Full price rows enriched with funding, signal and margin columns."""
    spec = window.spec
    quantity = int(spec.execution["quantity"])
    near, far = spec.pair["near"], spec.pair["far"]
    sessions = sessions_for(window)
    near_sessions, far_sessions = sessions.get(near, {}), sessions.get(far, {})
    near_lot = float(window.series["contracts"][near].get("lot_size") or 1.0)
    state = holding_flags([row["date"] for row in window.series["rows"]], report["daily"])
    rows: list[dict[str, Any]] = []
    for row in window.series["rows"]:
        day = row["date"]
        session_date = row.get("session_date", day[:10])
        daily, position, held = state[day]
        funded = day in window.funding_swaprates
        spread_open = row["near_open"] * window.near_mult - row["far_open"] * window.far_mult
        row_out: dict[str, Any] = {
            "date": day,
            "timestamp": row.get("timestamp", day),
            "session_date": session_date,
            "timeframe": window.series["timeframe"],
            "benchmark_mark_kind": row.get("benchmark_mark_kind"),
            "benchmark_mark_date": row.get("benchmark_mark_date"),
            "near_session_volume": near_sessions.get(session_date),
            "far_session_volume": far_sessions.get(session_date),
            "near": row["near_close"],
            "far": row["far_close"],
            "near_open": row["near_open"],
            "far_open": row["far_open"],
            "spread": row["spread_value"],
            "spread_open": round(spread_open, 8),
            "spread_last_trade": None,
            "spread_rel": rel_pct(row["spread_value"], row["far_close"] * window.far_mult),
            "spread_open_rel": rel_pct(spread_open, row["far_open"] * window.far_mult),
            "spread_last_trade_rel": None,
            "near_volume": row.get("near_volume"),
            "far_volume": row.get("far_volume"),
            "spread_volume": None,
            "spread_trades": None,
            "volume_eligible": positive_pair_volume(row),
            "days_to_near_expiry": None,
            "funding_rub": round(daily.get("funding_rub", 0.0), 8) if day in window.marks else None,
            "funding_swaprate_rub_per_unit": window.funding_swaprates.get(day),
            "funding_lot_size": near_lot if funded else None,
            "funding_contracts": quantity if funded and held else 0,
            "funding_source": "MOEX ISS SWAPRATE; EOD settlement history" if funded else None,
            **(
                window.marks[day].series_fields(window.funding_window.book.marks)
                if day in window.marks
                else {}
            ),
            "signal_z": daily.get("signal_z"),
            "margin_rub": round(window.margin_pair, 2) if position else 0.0,
        }
        rows.append(row_out)
    return rows


def selection_for(window: MeanReversionWindow) -> dict[str, Any]:
    spec = window.spec
    return {
        "universe": PAIR_UNIVERSE,
        "pair": spec.pair["pair_id"],
        "strategy": "mean_reversion",
        "timeframe": window.series["timeframe"],
        "fill": spec.execution["fill"],
        "quantity": int(spec.execution["quantity"]),
        "slippage_ticks": float(spec.execution["slippage_ticks"]),
        "parameters": spec.params,
        "initial_capital_rub": spec.execution["initial_capital_rub"],
    }


def instrument_for(window: MeanReversionWindow) -> dict[str, Any]:
    return {
        "near": window.spec.pair["near"],
        "far": window.spec.pair["far"],
        "family": window.spec.pair["family"],
    }


def notes(window: MeanReversionWindow, trades: list[dict[str, Any]]) -> list[str]:
    """Qualify the proxy price path, funding proxies and any forced end-of-sample exit."""
    intraday = get_timeframe(window.series["timeframe"]).intraday
    evidence = window.evidence
    if evidence is None:
        funding_notes = [
            "На выбранном внутридневном таймфрейме SWAPRATE не включён: дневную ставку нельзя "
            "размножать на внутридневные бары."
            if intraday
            else "Для пары без вечного фьючерса фандинг неприменим."
        ]
    else:
        funding_notes = [*evidence.notes(), CARRY_FILL_NOTE]
    fill_label = window.spec.execution["fill"]
    fill_note = {
        "reference_same_bar": REFERENCE_WARNING,
        "next_open": (
            "Сигнал по закрытию дня; исполнение по следующему открытию дня (прокси открытия candle); "
            "отметки по закрытию."
        ),
    }.get(
        fill_label,
        "Сигнал по закрытию дня; исполнение по закрытию следующего дня (ценовой прокси); отметки по "
        "закрытию.",
    )
    return [
        *funding_notes,
        VOLUME_RULE_NOTE,
        fill_note,
        *([OPEN_POSITION_NOTE] if any(trade["open_at_end"] for trade in trades) else []),
        *SPREAD_NOTES,
    ]
