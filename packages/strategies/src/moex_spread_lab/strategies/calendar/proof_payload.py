"""Calendar-spread proof payload assembly (compact metrics and full audit ledger)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from moex_spread_lab.accounting.costs import TAKER_RATE_PERCENT
from moex_spread_lab.core.settings import INITIAL_CAPITAL, MIN_DAILY_VOLUME

from .contracts import ASSETS, stress_ok
from .proof_ledger import (
    clean_payload,
    compact_series,
    elapsed_days,
    full_event_dict,
    full_series_row,
    simulate_marked_ledger,
    sizing_intents_trades,
)

_PROOF_NOTES = [
    "Цена спреда = расчёт дальней минус расчёт ближней (биржевой расчёт спреда там, где он публикуется).",
    "Даты с нулевым объёмом самого календарного спреда остаются в журнале как наблюдения, но исключены из индикаторов, гейта объёма и симуляции входа/выхода; позиции на них не переоцеваются.",
    "Исполнение next_open использует цены первых сделок ног за день; исполнение settlement — официальный расчёт.",
    "Отметки — по расчётным ценам с дневной стоимостью пункта; издержки — опубликованный тариф биржи по обеим ногам плюс проскальзывание один раз на сторону.",
    "Для контрактов в USD и CNY стоимость пункта дрейфует вместе с валютой, поэтому день-в-день результат немного отличается от арифметики «вход–выход» (показано как дрейф стоимости пункта FX).",
    "Маржа — запасной коэффициент по сегодняшнему контракту; издержки LQDT — доходность бенчмарка на этой марже, пока позиция открыта.",
    "Спред показан в % от цены дальней ноги; абсолютные пункты остаются в CSV и во второй строке таблицы.",
]


@dataclass(frozen=True, slots=True)
class _ProofEnvelopeCtx:
    window: dict[str, Any]
    account: dict[str, Any]
    asset: str
    strategy: str
    fill_label: str
    options: dict[str, Any]
    threshold: float
    quantity: int
    slippage: float
    terms: dict[str, Any]
    initial_capital: float


def _base_proof_envelope(
    ctx: _ProofEnvelopeCtx,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    window = ctx.window
    selection = {
        "asset": ctx.asset,
        "pair": window["pair"],
        "spread_instrument": window["spread_instrument"],
        "strategy": ctx.strategy,
        "fill": ctx.fill_label,
        "quantity": ctx.quantity,
        "slippage_ticks": ctx.slippage,
        "initial_capital_rub": ctx.initial_capital,
        "parameters": {**ctx.options, "threshold_pp": ctx.threshold},
    }
    win_meta = {
        "from": window["first_date"],
        "to": window["last_date"],
        "near_expiry": window["near_expiry"],
        "near": window["near"],
        "far": window["far"],
        "tradable_every_day": window.get(
            "tradable_every_day", (window.get("min_spread_volume") or 0) >= MIN_DAILY_VOLUME
        ),
        "passes_capital_screen": stress_ok(
            window["max_pair_margin_rub"], ctx.quantity, ctx.initial_capital
        ),
        "max_pair_margin_rub": window["max_pair_margin_rub"],
        "timeframe": window.get("timeframe", "1d"),
        "price_source": window.get("price_source", "native_daily_spread"),
        "observations": len(window["rows"]),
    }
    contract = {
        "point_value_rub": ctx.terms["mult"],
        "tick_size": ctx.terms["tick_size"],
        "reference_contract": ctx.terms["reference_contract"],
        "margin_rate": ctx.terms["margin_rate"],
        "tariff_group": ASSETS[ctx.asset]["group"],
        "taker_rate_percent": TAKER_RATE_PERCENT[ASSETS[ctx.asset]["group"]],
        "broker_fee_rub": 0.0,
    }
    return selection, win_meta, contract


def _payload_shell(
    ctx: _ProofEnvelopeCtx,
    envelopes: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
    series: list[dict[str, Any]],
) -> dict[str, Any]:
    selection, win_meta, contract = envelopes
    return {
        "schema_version": "calendar-proof.v1",
        "initial_capital_rub": ctx.initial_capital,
        "selection": selection,
        "window": win_meta,
        "contract": contract,
        "series": series,
    }


def _compact_event(row: dict[str, Any], costs: dict[str, float], is_open: bool) -> dict[str, Any]:
    return {
        "date": row["date"],
        "spread_fill": row["spread_open_price"] if is_open else row["spread_price"],
        "costs": costs,
    }


def _trade_seeds(ctx: _ProofEnvelopeCtx, full: bool) -> list[dict[str, Any]]:
    """Per-trade rows before the marked ledger replaces gross/net with mark-to-market values."""
    is_open = ctx.fill_label == "next_open"
    trades: list[dict[str, Any]] = []
    for number, trade in enumerate(ctx.account["trade_list"], start=1):
        direction = int(trade["direction"])
        seed = {
            "number": number,
            "side": "short_spread" if direction < 0 else "long_spread",
            "quantity": ctx.quantity,
            "signal": trade["signal"],
            "entry": _compact_event(trade["entry_row"], trade["entry_costs"], is_open)
            if not full
            else full_event_dict(trade["entry_row"], trade["entry_costs"], is_open),
            "exit": _compact_event(trade["exit_row"], trade["exit_costs"], is_open)
            if not full
            else full_event_dict(trade["exit_row"], trade["exit_costs"], is_open),
            "reason": trade["reason"],
            "margin_rub": trade["margin_rub"],
            "gross_rub": float(trade["gross_rub"]),
            "costs_rub": float(trade["costs_rub"]),
            "lqdt_on_margin_rub": float(trade["lqdt_on_margin_rub"]),
        }
        if full:
            seed["legs"] = "sell far, buy near" if direction < 0 else "buy far, sell near"
            seed["days_held"] = elapsed_days(
                str(trade["entry_row"]["date"]), str(trade["exit_row"]["date"])
            )
        trades.append(seed)
    return trades


def _accumulate_trades(
    ctx: _ProofEnvelopeCtx, trades: list[dict[str, Any]], *, compact: bool
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Mark every trade against the daily ledger and return it with the payload summary."""
    ledger, marked, cumulative = simulate_marked_ledger(
        ctx.window["rows"], trades, ctx.quantity, compact=compact
    )
    fx_drift = 0.0
    for trade in trades:
        static = float(trade["gross_rub"])
        trade["gross_rub_static"] = static
        trade["gross_rub"] = round(float(marked[int(trade["number"])]), 2)
        trade["net_rub"] = round(float(trade["gross_rub"]) - float(trade["costs_rub"]), 2)
        trade["excess_rub"] = round(float(trade["net_rub"]) - float(trade["lqdt_on_margin_rub"]), 2)
        fx_drift += float(trade["gross_rub"]) - static
    gross = sum(float(trade["gross_rub"]) for trade in trades)
    net = sum(float(trade["net_rub"]) for trade in trades)
    lqdt_m = float(ctx.account["lqdt_on_pair_margin_while_open_rub"])
    summary: dict[str, Any] = {
        "trades": len(trades),
        "gross_rub": round(gross, 2),
        "costs_rub": ctx.account["costs_rub"],
        "net_rub": round(net, 2),
        "lqdt_on_margin_rub": lqdt_m,
        "excess_over_lqdt_rub": round(net - lqdt_m, 2),
        "winners": sum(1 for trade in trades if float(trade["net_rub"]) > 0),
    }
    if not compact:
        summary["ledger_reconciliation_rub"] = round(net - cumulative, 2)
        summary["fx_point_value_drift_rub"] = round(fx_drift, 2)
    return ledger, summary


def _metrics_only_payload(
    ctx: _ProofEnvelopeCtx,
    envelopes: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
    sizing_intents: bool,
    series_cache: dict[Any, Any] | None,
    series_key: Any,
) -> dict[str, Any]:
    payload = _payload_shell(
        ctx, envelopes, compact_series(ctx.window["rows"], series_cache, series_key)
    )
    if sizing_intents:
        payload.update(
            {
                "trades": sizing_intents_trades(ctx.account, ctx.fill_label == "next_open"),
                "ledger": [],
                "summary": {},
            }
        )
        return payload
    trades = _trade_seeds(ctx, full=False)
    ledger, summary = _accumulate_trades(ctx, trades, compact=True)
    payload.update({"trades": trades, "ledger": ledger, "summary": summary})
    return payload


def build_proof_payload(
    window: dict[str, Any],
    account: dict[str, Any],
    meta: dict[str, Any],
    **flags: Any,
) -> dict[str, Any]:
    ctx = _ProofEnvelopeCtx(
        window=window,
        account=account,
        asset=meta["asset"],
        strategy=meta["strategy"],
        fill_label=meta["fill_label"],
        options=meta["options"],
        threshold=meta["threshold"],
        quantity=meta["quantity"],
        slippage=meta["slippage"],
        terms=meta["terms"],
        initial_capital=meta.get("initial_capital", INITIAL_CAPITAL),
    )
    envelopes = _base_proof_envelope(ctx)
    if flags.get("metrics_only", False):
        return _metrics_only_payload(
            ctx,
            envelopes,
            bool(flags.get("sizing_intents", False)),
            flags.get("series_cache"),
            flags.get("series_key"),
        )
    rows = window["rows"]
    tf = window.get("timeframe", "1d")
    payload = _payload_shell(ctx, envelopes, [full_series_row(r, tf, ctx.quantity) for r in rows])
    trades = _trade_seeds(ctx, full=True)
    ledger, summary = _accumulate_trades(ctx, trades, compact=False)
    payload["window"].update(
        {
            "min_spread_volume": window.get("min_spread_volume"),
            "min_daily_volume_rule": window.get("liquidity_gate", MIN_DAILY_VOLUME),
            "excluded_volume_observations": sum(
                not r.get("volume_eligible", (r.get("min_volume") or 0) > 0) for r in rows
            ),
            "has_native_spread_volume": window.get("has_native_spread_volume", True),
        }
    )
    payload.update({"trades": trades, "ledger": ledger, "summary": summary, "notes": _PROOF_NOTES})
    return clean_payload(payload)
