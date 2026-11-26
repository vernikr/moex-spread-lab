"""Daily mark/cost journals replayed from prepared rows and trades."""

from __future__ import annotations

from typing import Any, cast

from .payload_tools import events


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
