"""Fast C-accelerated metrics-only sizing path (`_fastcore.size_proof_metrics`)."""

from __future__ import annotations

import math
from array import array
from typing import Any

from moex_spread_lab.core.w0 import W0Error

from .sizing_terms import AUTO_SIZING
from .sizing_terms import Terms as _Terms
from .sizing_terms import money as _money
from .sizing_terms import positive as _positive

_STD_COST_KEYS = frozenset({"exchange_clearing", "broker", "slippage"})

type AcceptedTrade = tuple[int, int, float, float, float, float, float, float, float, float, int]

type ScanColumns = tuple[
    list[str],
    array[float],
    array[float],
    array[float],
    array[float],
    array[int],
    array[int],
    array[int],
    dict[str, int],
]


def _get_or_build_scan_columns(
    rows: list[dict[str, Any]],
    terms: _Terms,
    marks: dict[str, float],
    scan_cache: dict[Any, ScanColumns] | None,
    scan_key: Any,
) -> ScanColumns:
    n = len(rows)
    cached = scan_cache.get(scan_key) if scan_cache is not None else None
    if cached is not None and len(cached[0]) == n:
        return cached
    dates = [r["date"] for r in rows]
    prices = array("d", (_positive(marks[d], "отметка LQDT") for d in dates))
    spreads = array("d", (float(r["spread"]) for r in rows))
    if terms.native:
        points = array("d", (terms.point(i) for i in range(n)))
        posted_units = array("d", (terms.posted_margin(i, 1) for i in range(n)))
    else:
        points = array("d", [1.0]) * n
        posted_units = array("d", [terms.margin]) * n
    elig_list = [1 if r.get("volume_eligible", True) else 0 for r in rows]
    elig = array("B", elig_list)
    entry_elig = array(
        "B",
        (
            1 if r.get("entry_eligible", bool(e)) else 0
            for r, e in zip(rows, elig_list, strict=True)
        ),
    )
    exit_elig = array(
        "B",
        (1 if r.get("exit_eligible", bool(e)) else 0 for r, e in zip(rows, elig_list, strict=True)),
    )
    date_to_idx = {d: i for i, d in enumerate(dates)}
    cols: ScanColumns = (
        dates,
        prices,
        spreads,
        points,
        posted_units,
        elig,
        entry_elig,
        exit_elig,
        date_to_idx,
    )
    if scan_cache is not None:
        scan_cache[scan_key] = cols
    return cols


def _build_single_trade_spec(
    trade: dict[str, Any],
    orig_idx: int,
    entry_idx: int,
    exit_idx: int,
    terms: _Terms,
) -> tuple[Any, ...] | None:
    same_day = exit_idx == entry_idx
    open_at_end = bool(trade.get("open_at_end"))
    entry_margin, entry_costs = terms.at_fill(trade["entry"], entry_idx, at_open=terms.open_fill)
    if not set(entry_costs) <= _STD_COST_KEYS:
        return None
    _positive(entry_margin, "ГО пары")
    if any(not math.isfinite(v) or v < 0 for v in entry_costs.values()):
        raise W0Error("Неизвестные издержки не могут стать нулём при расчёте размера.")
    if exit_idx >= 0:
        _, exit_costs = terms.at_fill(
            trade["exit"],
            exit_idx,
            at_open=False if same_day else (terms.open_fill and not open_at_end),
        )
        if not set(exit_costs) <= _STD_COST_KEYS:
            return None
        exit_point = (
            terms.point(exit_idx)
            if same_day
            else terms.fill_point(exit_idx, at_open=terms.open_fill and not open_at_end)
        )
    else:
        exit_costs = {}
        exit_point = 1.0
    return (
        orig_idx,
        entry_idx,
        exit_idx,
        1 if trade["side"] == "long_spread" else -1,
        open_at_end,
        float(trade["entry"]["spread_fill"]),
        float(trade["exit"]["spread_fill"]),
        float(entry_margin),
        float(entry_costs.get("exchange_clearing", 0.0)),
        float(entry_costs.get("broker", 0.0)),
        float(entry_costs.get("slippage", 0.0)),
        float(exit_costs.get("exchange_clearing", 0.0)),
        float(exit_costs.get("broker", 0.0)),
        float(exit_costs.get("slippage", 0.0)),
        float(exit_point),
    )


def _pack_trade_specs(
    trades: list[dict[str, Any]], date_to_idx: dict[str, int], terms: _Terms
) -> tuple[list[dict[str, Any]], list[tuple[Any, ...]]] | None:
    intents = {t["entry"]["date"]: t for t in trades}
    intent_list: list[dict[str, Any]] = []
    trade_specs: list[tuple[Any, ...]] = []
    for entry_day, trade in intents.items():
        entry_idx = date_to_idx.get(entry_day)
        if entry_idx is None:
            continue
        exit_idx = date_to_idx.get(trade["exit"]["date"], -1)
        spec = _build_single_trade_spec(trade, len(intent_list), entry_idx, exit_idx, terms)
        if spec is None:
            return None
        intent_list.append(trade)
        trade_specs.append(spec)
    trade_specs.sort(key=lambda item: item[1])
    return intent_list, trade_specs


def _resolve_scalar_bounds(
    scalar_summary: tuple[str, str] | None, date_to_idx: dict[str, int], n_rows: int
) -> tuple[bool, int, int]:
    if scalar_summary is None:
        return False, 0, n_rows - 1
    s_i = date_to_idx.get(scalar_summary[0])
    e_i = date_to_idx.get(scalar_summary[1])
    if s_i is not None and e_i is not None and s_i <= e_i:
        return True, s_i, e_i
    return False, 0, n_rows - 1


def _hydrate_accepted_trades(
    intent_list: list[dict[str, Any]],
    acc_tuples: list[AcceptedTrade],
    marks: dict[str, float],
) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    for orig_idx, q, margin_rub, gross_rub, ec0, ec1, ec2, xc0, xc1, xc2, has_xc in acc_tuples:
        trade = intent_list[orig_idx]
        trade["number"] = len(accepted) + 1
        trade["quantity"] = q
        trade["margin_rub"] = margin_rub
        trade["gross_rub"] = gross_rub
        trade["entry"]["costs"] = {"exchange_clearing": ec0, "broker": ec1, "slippage": ec2}
        trade["exit"]["costs"] = (
            {"exchange_clearing": xc0, "broker": xc1, "slippage": xc2} if has_xc else {}
        )
        trade["costs_rub"] = _money(ec0 + ec1 + ec2 + (xc0 + xc1 + xc2 if has_xc else 0.0))
        trade["net_rub"] = _money(gross_rub - trade["costs_rub"])
        trade["lqdt_on_margin_rub"] = _money(
            margin_rub * (marks[trade["exit"]["date"]] / marks[trade["entry"]["date"]] - 1)
        )
        accepted.append(trade)
    return accepted


def _size_proof_fast_c(
    fc: Any,
    proof: dict[str, Any],
    terms: _Terms,
    marks: dict[str, float],
    run_cfg: tuple[Any, float, str],
    *,
    scan_cache_pair: tuple[dict[Any, ScanColumns] | None, Any] = (None, None),
    scalar_summary: tuple[str, str] | None = None,
) -> dict[str, Any] | None:
    rows = proof["series"]
    if not rows:
        return None
    settings, initial, strategy_name = run_cfg
    scan_cache, scan_key = scan_cache_pair
    (
        dates,
        prices,
        spreads,
        points,
        posted_units,
        elig,
        entry_elig,
        exit_elig,
        date_to_idx,
    ) = _get_or_build_scan_columns(rows, terms, marks, scan_cache, scan_key)
    packed = _pack_trade_specs(proof["trades"], date_to_idx, terms)
    if packed is None:
        return None
    intent_list, trade_specs = packed
    has_funding = any(
        r.get("funding_rub") is not None or r.get("funding_contracts", 0) for r in rows
    )
    use_scalar, start_idx, end_idx = _resolve_scalar_bounds(scalar_summary, date_to_idx, len(rows))
    is_carry = (
        strategy_name == "funding_only" or proof.get("accounting_policy") == "carry_settlement.v1"
    )
    ledger_or_summary, acc_tuples = fc.size_proof_metrics(
        dates,
        prices,
        spreads,
        points,
        posted_units,
        elig,
        entry_elig,
        exit_elig,
        rows,
        trade_specs,
        float(initial),
        not settings.allow_failed_capital,
        terms.native_order,
        is_carry,
        has_funding,
        use_scalar,
        start_idx,
        end_idx,
    )
    if ledger_or_summary == "overlap":
        raise W0Error("Перекрывающиеся сигналы не поддерживаются одним счётом окна.")
    if use_scalar:
        p0, end_price, end_wealth, end_cum_net, any_window_shortfall = ledger_or_summary
        exposed = any(e_i <= end_idx and (x_i < 0 or x_i >= start_idx) for e_i, x_i in acc_tuples)
        proof["selection"]["sizing"] = AUTO_SIZING
        proof["initial_capital_rub"] = initial
        proof["sizing"] = {"initial_capital_rub": initial}
        proof["_scalar_train_summary"] = (
            initial,
            p0,
            end_price,
            end_wealth,
            end_cum_net,
            bool(any_window_shortfall),
            exposed,
        )
        return proof

    proof["selection"]["sizing"] = AUTO_SIZING
    proof["initial_capital_rub"] = initial
    proof["trades"] = _hydrate_accepted_trades(intent_list, acc_tuples, marks)
    proof["ledger"] = ledger_or_summary
    proof["sizing"] = {"initial_capital_rub": initial}
    return proof


# Public internal-package names; preserve the original private entry points.
STD_COST_KEYS = _STD_COST_KEYS
size_proof_fast_c = _size_proof_fast_c
