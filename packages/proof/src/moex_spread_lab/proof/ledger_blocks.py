"""Daily mark/cost journals replayed from prepared rows and trades."""

from __future__ import annotations

from collections.abc import Sequence
from math import fsum
from typing import Any, cast

from moex_spread_lab.core.universe import PAIR_UNIVERSE

from .market_window import PairMarketWindow, sessions_for, window_meta
from .payload_tools import PROOF_SCHEMA, events, positive_pair_volume, rel_pct


def _mark_step(
    position: dict[str, Any], near: float, far: float, mults: tuple[float, float], quantity: int
) -> float:
    """Signed mark move of the open position from its stored prices to `near`/`far`."""
    near_mult, far_mult = mults
    return (
        position["sign"]
        * quantity
        * ((near - position["near"]) * near_mult - (far - position["far"]) * far_mult)
    )


def _exit_step(
    position: dict[str, Any],
    exiting: dict[str, Any] | None,
    row: dict[str, Any],
    mults: tuple[float, float],
    quantity: int,
) -> tuple[float, float, dict[str, Any] | None]:
    """Mark move and costs of a held position on one day; closing trades clear it."""
    end_near, end_far = (
        (exiting["exit"]["near_price"], exiting["exit"]["far_price"])
        if exiting is not None
        else (row["near"], row["far"])
    )
    step_cost = (
        sum(cast(dict[str, float], exiting["exit"]["costs"] or {}).values())
        if exiting is not None
        else 0.0
    )
    held = None if exiting is not None else {**position, "near": end_near, "far": end_far}
    return _mark_step(position, end_near, end_far, mults, quantity), step_cost, held


def _entry_step(
    entering: dict[str, Any], row: dict[str, Any], mults: tuple[float, float], quantity: int
) -> tuple[float, float, dict[str, Any]]:
    """Mark move and costs of a position opened on this row, plus the new position."""
    sign = 1 if entering["side"] == "long_spread" else -1
    entry = entering["entry"]
    step_mark = (
        sign
        * quantity
        * (
            (row["near"] - entry["near_price"]) * mults[0]
            - (row["far"] - entry["far_price"]) * mults[1]
        )
    )
    return (
        step_mark,
        sum(cast(dict[str, float], entry["costs"] or {}).values()),
        {
            "sign": sign,
            "quantity": int(quantity),
            "near": row["near"],
            "far": row["far"],
        },
    )


def _ledger_row(
    row: dict[str, Any],
    position: dict[str, Any] | None,
    step_mark: float,
    step_cost: float,
    cumulative: float,
    labels: dict[str, str],
) -> dict[str, Any]:
    day = row["date"]
    return {
        "date": day,
        "timestamp": row.get("timestamp", day),
        "session_date": row.get("session_date", day[:10]),
        "benchmark_mark_date": row.get("benchmark_mark_date"),
        "position": position["sign"] if position else 0,
        "quantity": position["quantity"] if position else 0,
        "mark_pnl_rub": round(step_mark, 2),
        "funding_rub": row.get("funding_rub"),
        "costs_rub": round(step_cost, 2),
        "lqdt_on_margin_rub": None,
        "cumulative_net_rub": round(cumulative, 2),
        "event": labels.get(day),
    }


def price_ledger(
    series_rows: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    near_mult: float,
    far_mult: float,
    quantity: int,
) -> list[dict[str, Any]]:
    """Daily mark/cost journal replayed from price rows and trades.

    Rows without eligible volume keep their journal entry but never revalue, open
    or close a position.
    """
    by_entry = {trade["entry"]["date"]: trade for trade in trades}
    by_exit = {trade["exit"]["date"]: trade for trade in trades}
    labels = events(trades)
    mults = (near_mult, far_mult)
    position: dict[str, Any] | None = None
    cumulative = 0.0
    ledger: list[dict[str, Any]] = []
    for row in series_rows:
        day = row["date"]
        eligible = row.get("volume_eligible", True)
        step_mark = 0.0
        step_cost = 0.0
        if eligible and position is not None:
            exiting = by_exit.get(day)
            moved, cost, position = _exit_step(position, exiting, row, mults, quantity)
            step_mark += moved
            step_cost += cost
        entering = by_entry.get(day) if eligible else None
        if entering is not None:
            moved, cost, position = _entry_step(entering, row, mults, quantity)
            step_mark += moved
            step_cost += cost
        step_mark += float(row.get("funding_rub") or 0.0)
        cumulative += step_mark - step_cost
        ledger.append(_ledger_row(row, position, step_mark, step_cost, cumulative, labels))
    return ledger


def funding_ledger(
    daily_rows: list[dict[str, Any]],
    trades: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Daily journal for settlement-priced (funding/carry) strategies."""
    by_entry = {trade["entry"]["date"]: trade for trade in trades}
    by_exit = {trade["exit"]["date"]: trade for trade in trades}
    labels = events(trades)
    cumulative = 0.0
    held_quantity = 0
    ledger: list[dict[str, Any]] = []
    for row in daily_rows:
        day = row["date"]
        step_mark = float(row["gross_pnl_increment"])
        step_cost = 0.0
        exiting = by_exit.get(day)
        entering = by_entry.get(day)
        if exiting is not None and not exiting["open_at_end"]:
            step_cost += sum(cast(dict[str, float], exiting["exit"]["costs"] or {}).values())
            held_quantity = 0
        if entering is not None:
            step_cost += sum(cast(dict[str, float], entering["entry"]["costs"] or {}).values())
            held_quantity = int(entering.get("quantity", 1))
            if exiting is entering and not entering["open_at_end"]:
                held_quantity = 0
        cumulative += step_mark - step_cost
        is_open = bool(row["position"])
        ledger.append(
            {
                "date": day,
                "position": -1 if is_open else 0,
                "quantity": held_quantity if is_open else 0,
                "mark_pnl_rub": round(step_mark, 2),
                "funding_rub": round(row["daily_funding_included_rub"], 2),
                "costs_rub": round(step_cost, 2),
                "lqdt_on_margin_rub": None,
                "cumulative_net_rub": round(cumulative, 2),
                "event": labels.get(day),
            }
        )
    return ledger


def pair_selection(
    pair_id: str, strategy: str, timeframe: str, execution: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    return {
        "universe": PAIR_UNIVERSE,
        "pair": pair_id,
        "strategy": strategy,
        "timeframe": timeframe,
        "fill": execution["fill"],
        "quantity": int(execution["quantity"]),
        "slippage_ticks": float(execution["slippage_ticks"]),
        "parameters": params,
        "initial_capital_rub": execution["initial_capital_rub"],
    }


_FILL_KEYS = ("date", "spread_fill", "near_price", "far_price")


def chart_rows(window: PairMarketWindow, signal_z: dict[str, float | None]) -> list[dict[str, Any]]:
    """Position-neutral chart rows; accounting columns are merged in from the ledger."""
    spec = window.spec
    near, far = spec.pair["near"], spec.pair["far"]
    sessions = sessions_for(window)
    near_sessions, far_sessions = sessions.get(near, {}), sessions.get(far, {})
    near_lot = float(window.series["contracts"][near].get("lot_size") or 1.0)
    rows: list[dict[str, Any]] = []
    for row in window.series["rows"]:
        day = row["date"]
        session_date = row.get("session_date", day[:10])
        funded = day in window.funding_swaprates
        spread_open = row["near_open"] * window.near_mult - row["far_open"] * window.far_mult
        rows.append(
            {
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
                "funding_rub": 0.0 if day in window.marks else None,
                "funding_swaprate_rub_per_unit": window.funding_swaprates.get(day),
                "funding_lot_size": near_lot if funded else None,
                "funding_contracts": 0,
                "funding_source": "MOEX ISS SWAPRATE; EOD settlement history" if funded else None,
                **(
                    window.marks[day].series_fields(window.funding_window.book.marks)
                    if day in window.marks
                    else {}
                ),
                "signal_z": signal_z.get(day),
                "margin_rub": 0.0,
            }
        )
    return rows


def selection_for(window: PairMarketWindow) -> dict[str, Any]:
    spec = window.spec
    return pair_selection(
        spec.pair["pair_id"],
        "mean_reversion",
        window.series["timeframe"],
        spec.execution,
        spec.params,
    )


def _head(window: PairMarketWindow) -> dict[str, Any]:
    """Selection/instrument/window block shared by the metrics and sizing shapes."""
    pair = window.spec.pair
    return {
        "schema_version": PROOF_SCHEMA,
        "universe": PAIR_UNIVERSE,
        "selection": selection_for(window),
        "initial_capital_rub": window.spec.execution["initial_capital_rub"],
        "instrument": {"near": pair["near"], "far": pair["far"], "family": pair["family"]},
        "window": window_meta(window),
    }


def sizing_unit(
    window: PairMarketWindow, unit: dict[str, Any], provenance: dict[str, Any]
) -> dict[str, Any]:
    """Price-only payload handed to `position_sizing.size_proof` (no cost or capital data)."""
    journal = {row["date"]: row for row in unit["series"]}
    series = [
        {
            "date": row["date"],
            "benchmark_mark_date": row.get("benchmark_mark_date"),
            "spread": row["spread_value"],
            "volume_eligible": positive_pair_volume(row),
            "funding_rub": journal.get(row["date"], {}).get("funding_rub"),
            "funding_contracts": journal.get(row["date"], {}).get("funding_contracts", 0),
        }
        for row in window.series["rows"]
    ]
    trades = [
        {
            "side": trade["side"],
            "signal": {
                "kind": trade["signal"]["kind"],
                "value": trade["signal"]["value"],
                "date": trade["signal"]["date"],
            },
            "entry": {key: trade["entry"][key] for key in _FILL_KEYS},
            "exit": {key: trade["exit"][key] for key in _FILL_KEYS},
            "exit_reason": trade["exit_reason"],
            "open_at_end": trade["open_at_end"],
        }
        for trade in unit["trades"]
    ]
    return {
        **_head(window),
        "contract": {},
        "series": series,
        "trades": trades,
        "provenance": provenance,
    }


def metrics_payload(
    window: PairMarketWindow,
    trades: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """Tuning-search shape: trades and ledger only, no chart series or capital curve."""
    return {**_head(window), "trades": trades, "ledger": ledger, "provenance": provenance}


def apply_raw_trade_totals(
    summary: dict[str, Any],
    trades: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    raw_totals: Sequence[float],
) -> None:
    """Update summary only; do not mutate the ledger, trade rows or account curve."""
    gross, costs, net = raw_totals
    for name, value in (("gross", gross), ("costs", costs), ("net", net)):
        key = f"{name}_rub"
        rounded = round(value, 2)
        displayed_sum = round(fsum(float(t[key]) for t in trades), 2)
        delta = round(displayed_sum - rounded, 2)
        summary[key] = rounded
        delta_key = f"displayed_trade_{name}_rounding_delta_rub"
        if delta:
            summary[delta_key] = delta
        else:
            summary.pop(delta_key, None)
    hurdle = fsum(float(t["lqdt_on_margin_rub"]) for t in trades)
    summary["excess_over_lqdt_rub"] = round(net - hurdle, 2)
    cumulative = ledger[-1]["cumulative_net_rub"] if ledger else 0.0
    summary["ledger_reconciliation_rub"] = round(round(net, 2) - cumulative, 2)
