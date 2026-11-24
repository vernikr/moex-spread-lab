"""Chart series, notes and payload shapes of a pair price proof.

Everything here is a pure mapping over a `PairMarketWindow` plus already accounted
trades: the chart/series template never needs a strategy replay of its own.
"""

from __future__ import annotations

from typing import Any

from moex_spread_lab.core.timeframes import get_timeframe
from moex_spread_lab.core.universe import PAIR_UNIVERSE
from moex_spread_lab.strategies import REFERENCE_WARNING

from .market_window import PairMarketWindow, sessions_for, window_meta
from .pair_payload import PairPayloadSpec, pair_payload
from .payload_tools import PROOF_SCHEMA, positive_pair_volume, rel_pct

CARRY_FILL_NOTE = (
    "Фандинг удерживаемой позиции = −position × SWAPRATE × лот × число контрактов. Источник ставки — "
    "итог клиринга MOEX ISS, кроме явно отмеченных оценок. Вход/выход по open — прокси времени "
    "исполнения; лот — из текущего снапшота. Это не пересчёт минутных D/K1/K2 и не брокерская выписка."
)
VOLUME_RULE_NOTE = (
    "Дни, когда объём любой ноги равен нулю или отсутствует, остаются в журнале, но исключены из "
    "индикаторов, гейта объёма и симуляции сделок; позиции на этих датах не переоцениваются."
)
OPEN_POSITION_NOTE = (
    "Открытая на конец выборки позиция отмечается как принудительный прокси-выход, а не как "
    "исполнимая сделка."
)
SPREAD_NOTES = (
    "Спред пары — близкая минус дальняя, нормированный в рублях, а не собственный инструмент; его "
    "объём не наблюдаем.",
    "Спред показан в % от стоимости дальней ноги; абсолютные отметки в рублях — в CSV и во второй "
    "строке таблицы.",
)
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


def notes(window: PairMarketWindow, trades: list[dict[str, Any]]) -> list[str]:
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
    fill_note = {
        "reference_same_bar": REFERENCE_WARNING,
        "next_open": (
            "Сигнал по закрытию дня; исполнение по следующему открытию дня (прокси открытия candle); "
            "отметки по закрытию."
        ),
    }.get(
        window.spec.execution["fill"],
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


def full_payload(
    window: PairMarketWindow,
    accounted: tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], float],
    raw_totals: tuple[float, float, float],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """Full `execution-proof.v1` payload with chart series, notes and the built-in run id."""
    rows, trades, ledger, drawdown = accounted
    spec = window.spec
    return pair_payload(
        PairPayloadSpec(
            pair=spec.pair,
            strategy="mean_reversion",
            fill_label=spec.execution["fill"],
            quantity=int(spec.execution["quantity"]),
            slippage=float(spec.execution["slippage_ticks"]),
            params=spec.params,
            terms=spec.terms,
            series_rows=rows,
            trades=trades,
            ledger=ledger,
            daily_rows=[{"D": drawdown}],
            margin_pair=window.margin_pair,
            provenance=provenance,
            notes=notes(window, trades),
            risk=spec.risk,
            raw_trade_totals=raw_totals,
        )
    )
