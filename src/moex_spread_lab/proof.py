"""Unified execution-proof payload for the pair screener and native calendar spreads.

Every number produced here must reconcile: trades, the daily ledger and the summary are
built from the same marks and costs. Prices are ISS settlement/candle proxies, not
executable fills. The screen is offline-only; it never downloads data.
"""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_right
from collections.abc import Callable
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any

from .account_risk import assess_vtb_pair
from .analytics import run_mean_reversion_baseline
from .carry import build_carry_strategy, build_carry_study
from .costs import CONTRACT_GROUP, TAKER_RATE_PERCENT, side_cost, tariff_profile
from .evaluation_periods import STRICT_SCREENS, ResearchSettings
from .position_sizing import AUTO_SIZING, automatic, size_proof
from .proof_funding import FundingWindow, ProofFunding
from .proof_signals import (
    PRICE_FILLS,
    REFERENCE_WARNING,
    attach_entry_levels,
    pair_fill,
)
from .screener import _normalized_series
from .w0 import W0Error

PROOF_SCHEMA = "execution-proof.v1"
PAIR_UNIVERSE = "pair_screen"
CALENDAR_UNIVERSE = "calendar_native"
FORCED_EXIT_REASONS = frozenset({"end_of_sample", "end_of_window", "open_at_data_end_no_executable_exit"})


def _clean(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


def _sha256_bytes(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _benchmark_marks(path: Path) -> tuple[list[str], dict[str, float]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = [
        (row["date"], float(row["price"]))
        for row in payload.get("rows", [])
        if row.get("date") and row.get("price") is not None
    ]
    rows.sort(key=lambda item: item[0])
    return [day for day, _ in rows], dict(rows)


def _mark(dates: list[str], prices: dict[str, float], day: str) -> float:
    index = max(0, bisect_right(dates, day) - 1)
    return prices[dates[index]]


def _cost_totals(trades: list[dict[str, Any]]) -> dict[str, float]:
    totals = {"exchange_clearing": 0.0, "broker": 0.0, "slippage": 0.0}
    for trade in trades:
        for side in ("entry", "exit"):
            for key, value in (trade[side].get("costs") or {}).items():
                if key in totals:
                    totals[key] += float(value)
    return {key: round(value, 2) for key, value in totals.items()}


def _events(trades: list[dict[str, Any]]) -> dict[str, str]:
    events: dict[str, list[str]] = {}
    for trade in trades:
        events.setdefault(trade["entry"]["date"], []).append(f"ENTRY #{trade['number']}")
        events.setdefault(trade["exit"]["date"], []).append(f"EXIT #{trade['number']}")
    return {day: " + ".join(labels) for day, labels in events.items()}


def _result(net_rub: float) -> str:
    if net_rub > 0:
        return "win"
    if net_rub < 0:
        return "loss"
    return "flat"


def _run_id(identity: dict[str, Any]) -> str:
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _rel_pct(value: Any, base: Any) -> float | None:
    """Spread as %% of the far leg. Pure same-row ratio — strictly causal, no look-ahead."""
    try:
        numerator = float(value)
        denominator = float(base)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numerator) or not math.isfinite(denominator) or denominator == 0.0:
        return None
    return round(numerator / denominator * 100, 6)


def _load_instruments(contracts_path: Path) -> dict[str, dict[str, Any]]:
    try:
        rows = json.loads(contracts_path.read_text(encoding="utf-8"))["instruments"]
        historical_path = contracts_path.parent / "historical-contracts.json"
        if historical_path.is_file():
            historical_rows = json.loads(historical_path.read_text(encoding="utf-8")).get("instruments", [])
            rows.extend(historical_rows)
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return {row["secid"]: row for row in rows if isinstance(row, dict) and row.get("secid")}


def _point_value(terms: dict[str, Any], fallback: float = 1.0) -> float:
    try:
        tick_size = float(terms["tick_size"])
        if tick_size == 0:
            return fallback
        return float(terms["tick_value"]) / tick_size
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return fallback


def _legs_label(side: str) -> str:
    return "buy near / sell far" if side == "long_spread" else "sell near / buy far"


def _days_held(entry_date: str, exit_date: str) -> int | None:
    try:
        return (date.fromisoformat(exit_date[:10]) - date.fromisoformat(entry_date[:10])).days
    except ValueError:
        return None


class ProofService:
    """Single entry point for /api/proof/*; dispatches on the `universe` field."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    # ------------------------------------------------------------------ catalog

    def catalog(self) -> dict[str, Any]:
        from . import screener_app
        from .calendar_spreads import CalendarData

        settings = ResearchSettings.load(self.root)
        try:
            calendar = CalendarData(self.root).catalog(initial_capital=settings.initial_capital_rub)
        except (W0Error, OSError) as exc:
            calendar = {
                "till": None,
                "min_daily_volume": screener_app.MIN_DAILY_VOLUME,
                "strategies": [],
                "fills": [],
                "assets": [],
                "error": str(exc),
            }
        pair = screener_app.catalog(screener_app.ScreenerInputs(self.root), initial_capital=settings.initial_capital_rub)
        return {
            **calendar,
            "initial_capital_rub": settings.initial_capital_rub,
            "universes": [CALENDAR_UNIVERSE, PAIR_UNIVERSE],
            "pair_screen": pair,
        }

    # ------------------------------------------------------------------- proof

    def proof(self, selection: dict[str, Any]) -> dict[str, Any]:
        return self.prepare(selection)(selection)

    def prepare(self, selection: dict[str, Any], *, calendar=None, settings: ResearchSettings = STRICT_SCREENS) -> Callable[..., dict[str, Any]]:
        if not isinstance(selection, dict):
            raise W0Error("выбор должен быть объектом")
        if selection.get("sizing") not in (None, AUTO_SIZING):
            raise W0Error("Неизвестный режим расчёта размера позиции.")
        if not automatic(selection):
            return self._prepare_fixed(selection, calendar=calendar, settings=settings)

        def intent_request(request):
            return {**request, "quantity": 1, "slippage_ticks": settings.slippage_ticks}

        replay = self._prepare_fixed(intent_request(selection), calendar=calendar, settings=settings, sizing_intents=True)
        dates, prices = _benchmark_marks(self.root / "data/benchmark/lqdt-lineage.json")
        contracts = _load_instruments(self.root / "data/w1/current-intake.json")

        def sized_replay(request, through=None):
            unit = replay(intent_request(request), through=through)
            if unit.get("schema_version") == "calendar-proof.v1":
                unit = self._calendar_proof(request, unit)
            if not dates or dates[0] > unit["series"][0]["date"]:
                raise W0Error("Нет отметки LQDT на начало истории.")
            marks = {r["date"]: _mark(dates, prices, r["date"]) for r in unit["series"]}
            return size_proof(unit, marks, settings, contracts)

        return sized_replay

    def _prepare_fixed(self, selection: dict[str, Any], *, calendar=None, settings: ResearchSettings = STRICT_SCREENS, sizing_intents=False) -> Callable[..., dict[str, Any]]:
        """Request-scoped replay of one instrument/strategy with different numeric settings.

        Load the selected inputs once, not the entire registry per trial. Outside this
        request a new session sees refreshed local files. `through` cuts the actual
        simulator input (including carry's tariff reference), not just its output.
        """
        if not isinstance(selection, dict):
            raise W0Error("выбор должен быть объектом")
        universe = selection.get("universe", CALENDAR_UNIVERSE)
        if universe == PAIR_UNIVERSE:
            return self._prepare_pair(selection, settings, sizing_intents=sizing_intents)
        if universe != CALENDAR_UNIVERSE:
            raise W0Error("неизвестная вселенная")
        from .calendar_spreads import CalendarData

        calendar = calendar if calendar is not None else CalendarData(self.root)

        def replay(request, through=None):
            payload = calendar.proof(request, through=through, initial_capital=settings.initial_capital_rub)
            # The search only needs trades, daily marks and admissions from partial replays.
            return payload if through is not None else self._calendar_proof(request, payload)

        return replay

    # ------------------------------------------------------- calendar adapter

    def _calendar_proof(self, selection: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload)
        payload["universe"] = CALENDAR_UNIVERSE
        payload["schema_version"] = PROOF_SCHEMA
        if isinstance(payload.get("selection"), dict):
            payload["selection"]["universe"] = CALENDAR_UNIVERSE
        trades = payload.get("trades", [])
        for trade in trades:
            direction = 1 if trade["side"] == "long_spread" else -1
            trade["gross_pts"] = round((trade["exit"]["spread_fill"] - trade["entry"]["spread_fill"]) * direction, 3)
            trade["result"] = _result(trade["net_rub"])
            trade["open_at_end"] = trade["reason"] in FORCED_EXIT_REASONS
        events = _events(trades)
        for row in payload.get("ledger", []):
            row["funding_rub"] = None
            row["lqdt_on_margin_rub"] = None
            row["event"] = events.get(row["date"])
        for row in payload.get("series", []):
            row.setdefault("funding_rub", None)
            row.setdefault("signal_z", None)
        summary = payload["summary"]
        summary["cost_totals"] = _cost_totals(trades)
        summary["closed_trades"] = sum(not trade["open_at_end"] for trade in trades)
        summary["open_positions"] = sum(trade["open_at_end"] for trade in trades)
        summary.setdefault("max_excess_drawdown_rub", None)
        payload["provenance"] = payload.get("provenance") or {}
        selection_out = payload.get("selection") if isinstance(payload.get("selection"), dict) else {}
        payload["provenance"] = {**self._calendar_provenance(selection_out), **payload["provenance"]}
        payload["instrument"] = self._calendar_instrument(selection_out, payload)
        attach_entry_levels(payload)
        if payload["execution_reference"]:
            payload["notes"].append(REFERENCE_WARNING)
        fill = (selection.get("fill") or "next_open").replace("_", " ")
        payload["price_role"] = (
            f"Нативный календарный спред; исполнение {fill}, отметки по официальному расчёту"
        )
        payload["run_id"] = _run_id(
            {
                "selection": payload["selection"],
                "daily": payload.get("series"),
                "source_hashes": payload["provenance"],
            }
        )
        return payload

    # ----------------------------------------------------------- internals

    def _calendar_provenance(self, selection: dict[str, Any]) -> dict[str, str]:
        provenance: dict[str, str] = {}
        cache = self.root / "data/spread-history-cache"
        legs = str(selection.get("pair") or "").split("/")
        names = {
            "near_history": legs[0] if legs and legs[0] else None,
            "far_history": legs[1] if len(legs) > 1 and legs[1] else None,
            "spread_history": selection.get("spread_instrument"),
        }
        for label, secid in names.items():
            if not secid:
                continue
            matches = sorted(cache.glob(f"{secid}-*.json")) if cache.is_dir() else []
            if matches:
                digest = _sha256_bytes(matches[-1])
                if digest:
                    provenance[f"{label}_sha256"] = digest
        for label, relative in (
            ("contracts", "data/w1/current-intake.json"),
            ("benchmark", "data/benchmark/lqdt-lineage.json"),
            ("ruonia", "data/benchmark-cache/CBR_RUONIA.json"),
        ):
            digest = _sha256_bytes(self.root / relative)
            if digest:
                provenance[f"{label}_sha256"] = digest
        return provenance

    def _calendar_instrument(self, selection: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        window = payload.get("window") or {}
        contract = payload.get("contract") or {}
        legs = str(selection.get("pair") or "").split("/")
        near = window.get("near") or (legs[0] if legs and legs[0] else None)
        far = window.get("far") or (legs[1] if len(legs) > 1 and legs[1] else None)
        instruments = _load_instruments(self.root / "data/w1/current-intake.json")
        point_value = contract.get("point_value_rub")
        return {
            "near": near,
            "far": far,
            "family": "calendar_spread",
            "near_expiry": (instruments.get(near or "") or {}).get("expiry") or window.get("near_expiry"),
            "far_expiry": (instruments.get(far or "") or {}).get("expiry"),
            "near_point_value_rub": point_value,
            "far_point_value_rub": point_value,
            "spread_instrument": selection.get("spread_instrument"),
        }

    # ----------------------------------------------------------- pair adapter

    def _prepare_pair(self, selection: dict[str, Any], settings: ResearchSettings, *, sizing_intents=False) -> Callable[..., dict[str, Any]]:
        from . import screener_app

        pair = screener_app._pair(selection.get("pair"))
        near, far = pair["near"], pair["far"]
        strategy = selection.get("strategy")
        if not isinstance(strategy, str) or strategy not in screener_app.STRATEGIES:
            raise W0Error("неизвестная стратегия")
        inputs = screener_app.ScreenerInputs(self.root)
        funding = ProofFunding(inputs)
        entry = screener_app.catalog(inputs, registry=(pair,), funding=funding, initial_capital=settings.initial_capital_rub)["pairs"][0]
        if not entry["proof_strategies"].get(strategy):
            raise W0Error(entry["data_reasons"][strategy])
        # With a holdout, evaluate liquidity on the actual training replay, not the full catalog flag.
        if not (settings.validation_percent or settings.allow_failed_volume) and not entry["strategies"].get(strategy):
            raise W0Error("Не пройдено правило ликвидности пары.")

        @cache
        def risk_for(quantity):
            risk = assess_vtb_pair(inputs.contracts, near, far, quantity=quantity, initial_capital=settings.initial_capital_rub)
            if not settings.allow_failed_capital and not risk["summary"]["all_scenarios_passed"]:
                raise W0Error("пара не проходит стресс-экран рабочего капитала")
            return risk

        risk_for(screener_app.execution_settings(selection)["quantity"])
        instruments = _load_instruments(inputs.contracts)
        terms = {secid: instruments.get(secid, {}) for secid in (near, far)}
        dates, prices = _benchmark_marks(inputs.benchmark)
        if strategy == "mean_reversion":
            prepared, engine = self._price_inputs(inputs, pair, funding, settings.validation_percent), self._mean_reversion_proof
        else:
            prepared, engine = self._funding_inputs(inputs, pair, funding, settings.validation_percent), self._funding_proof

        # Reuse immutable input hashes throughout this request's Optuna trials.
        source_hashes = {
            "benchmark_sha256": _sha256_bytes(inputs.benchmark),
            "contracts_sha256": _sha256_bytes(inputs.contracts),
            "historical_contracts_sha256": _sha256_bytes(inputs.contracts.parent / "historical-contracts.json"),
        }
        prepared = (*prepared, source_hashes)

        def replay(request, through=None):
            execution = {**screener_app.execution_settings(request), "sizing_intents": sizing_intents,
                         "fill": pair_fill(request, strategy), "initial_capital_rub": settings.initial_capital_rub}
            params = screener_app._parameters(request, strategy)
            return engine(inputs, pair, execution, params, terms, dates, prices,
                          risk_for(execution["quantity"]), prepared, through)

        return replay

    # ------------------------------------------------------ mean reversion

    def _price_inputs(self, inputs, pair, funding, validation_percent):
        pair_id, near, far = pair["pair_id"], pair["near"], pair["far"]
        book = funding.timeline(pair) if pair["family"] == "perpetual_quarterly_same_asset" else None
        series = _normalized_series(
            inputs.candles(pair_id, near),
            inputs.candles(pair_id, far),
            inputs.benchmark,
            inputs.contracts,
            near=near,
            far=far,
            prior_benchmark_dates=book.no_clearing_dates if book else (),
        )
        analysis_rows = [
            row for row in series["rows"]
            if row.get("near_volume") is not None and row.get("far_volume") is not None
            and float(row["near_volume"]) > 0 and float(row["far_volume"]) > 0
        ]
        if len(analysis_rows) < 2:
            raise W0Error("fewer than two positive-volume common observations")
        if book is None:
            return series, analysis_rows, None, {}
        window = FundingWindow(book, (r["date"] for r in analysis_rows), validation_percent)
        hashes = funding.source(pair_id, near)["source"]["raw_response_sha256"]
        return series, analysis_rows, window, {"near_funding_history_sha256": hashes}

    def _mean_reversion_proof(
        self, inputs, pair, execution, params, terms, benchmark_dates, benchmark_prices, risk, prepared, through,
    ) -> dict[str, Any]:
        near, far = pair["near"], pair["far"]
        quantity, slippage = int(execution["quantity"]), float(execution["slippage_ticks"])
        fill_label = execution["fill"]
        delay, fill_quote = PRICE_FILLS[fill_label]
        series, analysis_rows, window, funding_provenance, source_hashes = prepared
        evidence = window.resolve(through) if window else None
        marks = evidence.marks if evidence else {}
        funding_swaprates = evidence.rates if evidence else {}
        if through is not None:
            series = {**series, "rows": [r for r in series["rows"] if r["date"] <= through]}
            analysis_rows = [r for r in analysis_rows if r["date"] <= through]
        analysis_series = {**series, "rows": analysis_rows}
        near_contract = series["contracts"][near]
        far_contract = series["contracts"][far]
        near_mult = float(near_contract["tick_value"]) / float(near_contract["tick_size"])
        far_mult = float(far_contract["tick_value"]) / float(far_contract["tick_size"])
        margin_pair = (float(near_contract["initial_margin"]) + float(far_contract["initial_margin"])) * quantity
        report = run_mean_reversion_baseline(
            analysis_series,
            initial_capital=execution["initial_capital_rub"],
            lookback=int(params["lookback"]),
            entry_z=float(params["entry_z"]),
            exit_z=float(params["exit_z"]),
            quantity=quantity,
            analysis_role="diagnostic_only",
            cost_profile=tariff_profile(slippage),
            fill=fill_quote,
            decision_delay_bars=delay,
            cash_in_lqdt=True,
            funding_swaprates=funding_swaprates,
            sizing_intents=execution.get("sizing_intents", False),
        )
        daily_map = {row["date"]: row for row in report["daily"]}
        volume_map = {row["date"]: row for row in series["rows"]}

        trades: list[dict[str, Any]] = []
        signal_dates = {r["date"]: analysis_rows[max(0, i - delay)]["date"] for i, r in enumerate(analysis_rows)}
        for number, row in enumerate(report["trades"], start=1):
            side = "long_spread" if row["direction"] == "long_near_short_far" else "short_spread"
            entry_date, exit_date = row["entry_date"][:10], row["exit_date"][:10]
            entry_costs = row.get("entry_costs") or {}
            exit_costs = row.get("exit_costs") or {}
            entry = self._fill_block(entry_date, row["entry_spread_fill"], row["entry_near_fill"], row["entry_far_fill"], volume_map, entry_costs, far_value=row["entry_far_fill"] * far_mult)
            exit_ = self._fill_block(exit_date, row["exit_spread_fill"], row["exit_near_fill"], row["exit_far_fill"], volume_map, exit_costs, far_value=row["exit_far_fill"] * far_mult)
            trades.append(
                self._trade_block(
                    number=number,
                    side=side,
                    quantity=quantity,
                    signal={"kind": "z", "value": row.get("entry_signal_z"), "date": signal_dates[entry_date]},
                    entry=entry,
                    exit=exit_,
                    exit_reason=row["exit_reason"],
                    margin_pair=margin_pair,
                    gross_rub=float(row["gross_pnl"]),
                    costs_rub=float(row["modeled_costs"] or 0.0),
                    net_rub=float(row["net_pnl"] or 0.0),
                    benchmark_dates=benchmark_dates,
                    funding_rub=float(row.get("funding_rub") or 0.0) if funding_swaprates else None,
                    benchmark_prices=benchmark_prices,
                )
            )

        series_rows = []
        position_on_last_analyzed_day = 0
        for row in series["rows"]:
            day = row["date"]
            daily = daily_map.get(day, {})
            if day in daily_map:
                position_on_last_analyzed_day = daily.get("position") or 0
            position = daily.get("position", position_on_last_analyzed_day) or 0
            held_for_clearing = bool(position) or daily.get("action") == "forced_exit_end_of_sample"
            series_rows.append(
                {
                    "date": day,
                    **({"benchmark_mark_kind": "prior_daily_close", "benchmark_mark_date": row["benchmark_mark_date"]} if row.get("benchmark_mark_date") else {}),
                    "near": row["near_close"],
                    "far": row["far_close"],
                    "near_open": row["near_open"],
                    "far_open": row["far_open"],
                    "spread": row["spread_value"],
                    "spread_open": round(row["near_open"] * near_mult - row["far_open"] * far_mult, 8),
                    "spread_last_trade": None,
                    "spread_rel": _rel_pct(row["spread_value"], row["far_close"] * far_mult),
                    "spread_open_rel": _rel_pct(row["near_open"] * near_mult - row["far_open"] * far_mult, row["far_open"] * far_mult),
                    "spread_last_trade_rel": None,
                    "near_volume": row.get("near_volume"),
                    "far_volume": row.get("far_volume"),
                    "spread_volume": None,
                    "spread_trades": None,
                    "volume_eligible": row.get("near_volume") is not None and row.get("far_volume") is not None
                    and float(row["near_volume"]) > 0 and float(row["far_volume"]) > 0,
                    "days_to_near_expiry": None,
                    "funding_rub": round(daily.get("funding_rub", 0.0), 8) if day in marks else None,
                    "funding_swaprate_rub_per_unit": funding_swaprates.get(day),
                    "funding_lot_size": float(near_contract.get("lot_size") or 1.0) if day in funding_swaprates else None,
                    "funding_contracts": quantity if day in funding_swaprates and held_for_clearing else 0,
                    "funding_source": "MOEX ISS SWAPRATE; EOD settlement history" if day in funding_swaprates else None,
                    **(marks[day].series_fields(window.book.marks) if day in marks else {}),
                    "signal_z": daily.get("signal_z"),
                    "margin_rub": round(margin_pair, 2) if position else 0.0,
                }
            )

        ledger = self._price_ledger(series_rows, trades, near_mult, far_mult, quantity)
        return self._pair_payload(
            pair=pair,
            strategy="mean_reversion",
            fill_label=fill_label,
            quantity=quantity,
            slippage=slippage,
            params=params,
            terms=terms,
            series_rows=series_rows,
            trades=trades,
            ledger=ledger,
            daily_rows=report["daily"],
            margin_pair=margin_pair,
            provenance={
                **series["provenance"],
                **source_hashes,
                **funding_provenance,
                **(evidence.provenance if evidence else {}),
            },
            notes=[
                *(evidence.notes() if evidence else ["Для пары без вечного фьючерса фандинг неприменим."]),
                *(["Фандинг удерживаемой позиции = −position × SWAPRATE × лот × число контрактов. Источник ставки — итог клиринга MOEX ISS, кроме явно отмеченных оценок. Вход/выход по open — прокси времени исполнения; лот — из текущего снапшота. Это не пересчёт минутных D/K1/K2 и не брокерская выписка."] if evidence else []),
                "Дни, когда объём любой ноги равен нулю или отсутствует, остаются в журнале, но исключены из индикаторов, гейта объёма и симуляции сделок; позиции на этих датах не переоцениваются.",
                (REFERENCE_WARNING if fill_label == "reference_same_bar" else
                 "Сигнал по закрытию дня; исполнение по следующему открытию дня (прокси открытия candle); отметки по закрытию."
                 if fill_label == "next_open" else
                 "Сигнал по закрытию дня; исполнение по закрытию следующего дня (ценовой прокси); отметки по закрытию."),
                *(
                    ["Открытая на конец выборки позиция отмечается как принудительный прокси-выход, а не как исполнимая сделка."]
                    if any(trade["open_at_end"] for trade in trades)
                    else []
                ),
                "Спред пары — близкая минус дальняя, нормированный в рублях, а не собственный инструмент; его объём не наблюдаем.",
                "Спред показан в % от стоимости дальней ноги; абсолютные отметки в рублях — в CSV и во второй строке таблицы.",
            ],
            risk=risk,
        )

    # ------------------------------------------------------------ funding carry

    def _funding_inputs(self, inputs, pair, funding, validation_percent):
        pair_id, near, far = pair["pair_id"], pair["near"], pair["far"]
        window = funding.carry_window(pair, validation_percent)
        near_source = funding.source(pair_id, near)
        far_source = funding.source(pair_id, far)
        # Resolve the full research period (including its split) before the study reads any estimate.
        study = build_carry_study(
            near_source, far_source, inputs.contracts, near=near, far=far,
            funding_regimes=None if pair_id == "CNYRUBF/CRZ6" else (),
            funding_marks=window.resolve().marks, no_clearing_dates=window.book.no_clearing_dates,
        )
        return study, near_source["source"], far_source["source"], window

    def _funding_proof(
        self, inputs, pair, execution, params, terms, benchmark_dates, benchmark_prices, risk, prepared, through,
    ) -> dict[str, Any]:
        pair_id, near, far = pair["pair_id"], pair["near"], pair["far"]
        quantity, slippage = int(execution["quantity"]), float(execution["slippage_ticks"])
        study, near_source, far_source, window, source_hashes = prepared
        evidence = window.resolve(through)
        if through is not None:
            study = {**study, "rows": [row for row in study["rows"] if row["date"] <= through]}
        report = build_carry_strategy(
            study,
            inputs.benchmark,
            inputs.contracts,
            transaction_cost_scenario="tariff",
            warmup_observations=int(params["lookback"]),
            strategy_variant="funding_only",
            initial_capital=execution["initial_capital_rub"],
            quantity=quantity,
            execution_delay_rows=0,
            cash_in_lqdt=True,
            slippage_ticks=slippage,
        )
        near_mult = _point_value(terms[near], fallback=1.0)
        far_mult = _point_value(terms[far], fallback=near_mult)
        margin_pair = (float(terms[near].get("initial_margin") or 0.0) + float(terms[far].get("initial_margin") or 0.0)) * quantity

        study_rows = study["rows"]
        ref_near = float(study_rows[-1]["near_settle"])
        ref_far = float(study_rows[-1]["far_settle"])
        tariff = tariff_profile(slippage)
        leg_contracts = _load_contracts_for_side_cost(inputs.contracts, near, far)
        reference_side = side_cost(tariff, [(leg_contracts[near], ref_near), (leg_contracts[far], ref_far)], 1)
        entry_costs = {key: round(value * quantity, 2) for key, value in reference_side.items()}
        exit_costs = dict(entry_costs)

        signal_reasons = {(sig["date"], sig["action"]): sig.get("reason") for sig in report.get("signals", [])}
        daily_rows = report["daily"]
        daily_by_date = {row["date"]: row for row in daily_rows}

        trades: list[dict[str, Any]] = []
        last_date = daily_rows[-1]["date"]
        for number, row in enumerate(report["trades"], start=1):
            open_at_end = row["status"] == "open_at_data_end_no_executable_exit"
            entry_date = row["entry_execution_date"]
            exit_date = row["exit_execution_date"] or last_date
            entry_row = daily_by_date[entry_date]
            exit_row = daily_by_date[exit_date]
            entry_fill = {
                "spread_fill": round(entry_row["near_settle"] * near_mult - entry_row["far_settle"] * far_mult, 8),
                "near_price": entry_row["near_settle"],
                "far_price": entry_row["far_settle"],
            }
            exit_fill = {
                "spread_fill": round(exit_row["near_settle"] * near_mult - exit_row["far_settle"] * far_mult, 8),
                "near_price": exit_row["near_settle"],
                "far_price": exit_row["far_settle"],
            }
            side = "short_spread"
            entry = self._fill_block(
                entry_date, entry_fill["spread_fill"], entry_fill["near_price"], entry_fill["far_price"],
                daily_by_date, entry_costs, far_value=entry_fill["far_price"] * far_mult,
            )
            exit_ = self._fill_block(
                exit_date, exit_fill["spread_fill"], exit_fill["near_price"], exit_fill["far_price"],
                daily_by_date, {} if open_at_end else exit_costs, far_value=exit_fill["far_price"] * far_mult,
            )
            if open_at_end:
                cost_rub = float(row["transaction_costs"])
            else:
                cost_rub = float(row["transaction_costs"])
            trades.append(
                self._trade_block(
                    number=number,
                    side=side,
                    quantity=quantity,
                    signal={
                        "kind": "funding",
                        "value": None,
                        "date": row["entry_decision_date"],
                        "reason": signal_reasons.get((row["entry_decision_date"], "entry")),
                    },
                    entry=entry,
                    exit=exit_,
                    exit_reason=(
                        row.get("reason")
                        or signal_reasons.get((row.get("exit_decision_date") or "", "exit"))
                        or row["status"]
                    ),
                    margin_pair=margin_pair,
                    gross_rub=float(row["gross_pnl"]),
                    costs_rub=cost_rub,
                    net_rub=float(row["net_pnl"]),
                    benchmark_dates=benchmark_dates,
                    benchmark_prices=benchmark_prices,
                    open_at_end=open_at_end,
                )
            )

        study_by_date = {row["date"]: row for row in study_rows}
        near_lot = float(terms[near].get("lot_size") or 1.0)
        series_rows = []
        for row in daily_rows:
            position = 1 if row["position"] else 0
            study_row = study_by_date.get(row["date"], {})
            series_rows.append(
                {
                    "date": row["date"],
                    "near": row["near_settle"],
                    "far": row["far_settle"],
                    "near_open": None,
                    "far_open": None,
                    "spread": round(row["near_settle"] * near_mult - row["far_settle"] * far_mult, 8),
                    "spread_open": None,
                    "spread_last_trade": None,
                    "spread_rel": _rel_pct(row["near_settle"] * near_mult - row["far_settle"] * far_mult, row["far_settle"] * far_mult),
                    "spread_open_rel": None,
                    "spread_last_trade_rel": None,
                    "near_volume": row.get("near_volume"),
                    "far_volume": row.get("far_volume"),
                    "spread_volume": None,
                    "spread_trades": None,
                    "days_to_near_expiry": None,
                    "funding_rub": round(row["daily_funding_included_rub"], 2),
                    "funding_swaprate_rub_per_unit": study_row.get("near_swaprate"),
                    "funding_lot_size": near_lot,
                    "funding_contracts": quantity if position else 0,
                    "funding_source": "MOEX ISS SWAPRATE; EOD settlement history",
                    **evidence.marks[row["date"]].series_fields(window.book.marks),
                    "signal_z": None,
                    "margin_rub": round(margin_pair, 2) if position else 0.0,
                }
            )

        ledger = self._funding_ledger(report["daily"], trades, quantity)
        return self._pair_payload(
            pair=pair,
            strategy="funding_only",
            fill_label="same_day_settlement",
            quantity=quantity,
            slippage=slippage,
            params=params,
            terms=terms,
            series_rows=series_rows,
            trades=trades,
            ledger=ledger,
            daily_rows=report["daily"],
            margin_pair=margin_pair,
            provenance={
                "near_history_sha256": near_source["raw_response_sha256"],
                "far_history_sha256": far_source["raw_response_sha256"],
                **source_hashes,
                **evidence.provenance,
            },
            notes=[
                *evidence.notes(),
                "Сигнал фондирования использует фондирование до предыдущего клиринга; прокси исполнения — расчёт в тот же день; выход в тот же день теряет фондирование этого дня.",
                "В таблице SWAPRATE показан отдельно от денежного итога: начисление/списание = SWAPRATE × лот × число удерживаемых контрактов; положительный итог здесь означает получение шортом ближнего перпетуала. Базовый источник — итог клиринга ISS; оценки помечены отдельно. Это не брокерская выписка.",
                *(["Для GLDRUBF MOEX публикует K1=0,05% и K2=0,15%; источник исходных ставок — ISS. Внутридневное отклонение D и параметры режима из исторических данных не реконструируются."] if near == "GLDRUBF" else []),
                *(
                    ["Потолки правил фондирования для не-CNY инструментов не восстанавливаются."]
                    if pair_id != "CNYRUBF/CRZ6"
                    else []
                ),
                "Спред пары — близкая минус дальняя, нормированный в рублях, а не собственный инструмент; его объём не наблюдаем.",
                ("Комиссии и проскальзывание пересчитываются по прокси-ценам каждого входа и выхода; проскальзывание — из общих настроек."
                 if execution.get("sizing_intents") else
                 "Комиссии — опубликованный тариф MOEX taker, смоделированный на текущем референсе (расчёт по последней строке), а не на каждом ценовом значении исполнения."),
                "Спред показан в % от стоимости дальней ноги; абсолютные отметки в рублях — в CSV и во второй строке таблицы.",
                *(
                    ["Открытая на конец выборки позиция отмечается как принудительный прокси-выход, а не как исполнимая сделка."]
                    if any(trade["open_at_end"] for trade in trades)
                    else []
                ),
            ],
            risk=risk,
        )

    # ------------------------------------------------------------- internals

    def _fill_block(
        self,
        day: str,
        spread_fill: float,
        near_price: float,
        far_price: float,
        rows_by_date: dict[str, dict[str, Any]],
        costs: dict[str, float],
        far_value: float | None = None,
    ) -> dict[str, Any]:
        row = rows_by_date.get(day, {})
        return {
            "date": day,
            "spread_fill": spread_fill,
            "spread_fill_rel": _rel_pct(spread_fill, far_value if far_value is not None else far_price),
            "near_price": near_price,
            "far_price": far_price,
            "near_volume": row.get("near_volume") if "near_volume" in row else row.get("volume"),
            "far_volume": row.get("far_volume"),
            "spread_volume": row.get("spread_volume"),
            "margin_rub": None,
            "costs": {"exchange_clearing": 0.0, "broker": 0.0, "slippage": 0.0, **(costs or {})},
        }

    def _trade_block(
        self,
        *,
        number: int,
        side: str,
        quantity: int,
        signal: dict[str, Any],
        entry: dict[str, Any],
        exit: dict[str, Any],
        exit_reason: str,
        margin_pair: float,
        gross_rub: float,
        costs_rub: float,
        net_rub: float,
        benchmark_dates: list[str],
        benchmark_prices: dict[str, float],
        open_at_end: bool | None = None,
        funding_rub: float | None = None,
    ) -> dict[str, Any]:
        open_at_end = open_at_end if open_at_end is not None else exit_reason in FORCED_EXIT_REASONS
        entry_mark = _mark(benchmark_dates, benchmark_prices, entry["date"])
        exit_mark = _mark(benchmark_dates, benchmark_prices, exit["date"])
        hurdle = margin_pair * (exit_mark / entry_mark - 1)
        return {
            "number": number,
            "side": side,
            "legs": _legs_label(side),
            "direction_detail": "long_near_short_far" if side == "long_spread" else "short_near_long_far",
            "quantity": quantity,
            "signal": signal,
            "entry": entry,
            "exit": exit,
            "days_held": _days_held(entry["date"], exit["date"]),
            "exit_reason": exit_reason,
            "open_at_end": open_at_end,
            "gross_pts": None,
            "gross_rub": round(gross_rub, 2),
            "funding_rub": round(funding_rub, 2) if funding_rub is not None else None,
            "costs_rub": round(costs_rub, 2),
            "net_rub": round(net_rub, 2),
            "margin_rub": round(margin_pair, 2),
            "lqdt_on_margin_rub": round(hurdle, 2),
            "excess_rub": round(net_rub - hurdle, 2),
            "result": _result(net_rub),
        }

    def _price_ledger(
        self,
        series_rows: list[dict[str, Any]],
        trades: list[dict[str, Any]],
        near_mult: float,
        far_mult: float,
        quantity: int,
    ) -> list[dict[str, Any]]:
        by_entry = {trade["entry"]["date"]: trade for trade in trades}
        by_exit = {trade["exit"]["date"]: trade for trade in trades}
        events = _events(trades)
        position: dict[str, Any] | None = None
        cumulative = 0.0
        ledger = []
        for row in series_rows:
            day = row["date"]
            mark = 0.0
            cost = 0.0
            volume_eligible = row.get("volume_eligible", True)
            if volume_eligible and position is not None:
                exiting = by_exit.get(day)
                if exiting is not None:
                    end_near, end_far = exiting["exit"]["near_price"], exiting["exit"]["far_price"]
                    move = position["sign"] * quantity * (
                        (end_near - position["near"]) * near_mult - (end_far - position["far"]) * far_mult
                    )
                    cost += sum((exiting["exit"]["costs"] or {}).values())
                    position = None
                else:
                    end_near, end_far = row["near"], row["far"]
                    move = position["sign"] * quantity * (
                        (end_near - position["near"]) * near_mult - (end_far - position["far"]) * far_mult
                    )
                    position["near"], position["far"] = end_near, end_far
                mark += move
            entering = by_entry.get(day) if volume_eligible else None
            if entering is not None:
                sign = 1 if entering["side"] == "long_spread" else -1
                entry = entering["entry"]
                cost += sum((entry["costs"] or {}).values())
                mark += sign * quantity * (
                    (row["near"] - entry["near_price"]) * near_mult - (row["far"] - entry["far_price"]) * far_mult
                )
                position = {"sign": sign, "near": row["near"], "far": row["far"], "number": entering["number"]}
            funding = float(row.get("funding_rub") or 0.0)
            mark += funding
            cumulative += mark - cost
            ledger.append(
                {
                    "date": day,
                    "position": position["sign"] if position else 0,
                    "mark_pnl_rub": round(mark, 2),
                    "funding_rub": row.get("funding_rub"),
                    "costs_rub": round(cost, 2),
                    "lqdt_on_margin_rub": None,
                    "cumulative_net_rub": round(cumulative, 2),
                    "event": events.get(day),
                }
            )
        return ledger

    def _funding_ledger(
        self,
        daily_rows: list[dict[str, Any]],
        trades: list[dict[str, Any]],
        quantity: int,
    ) -> list[dict[str, Any]]:
        by_entry = {trade["entry"]["date"]: trade for trade in trades}
        by_exit = {trade["exit"]["date"]: trade for trade in trades}
        events = _events(trades)
        cumulative = 0.0
        ledger = []
        for row in daily_rows:
            day = row["date"]
            mark = float(row["gross_pnl_increment"])
            cost = 0.0
            entering = by_entry.get(day)
            if entering is not None:
                cost += sum((entering["entry"]["costs"] or {}).values())
            exiting = by_exit.get(day)
            if exiting is not None and not exiting["open_at_end"]:
                cost += sum((exiting["exit"]["costs"] or {}).values())
            cumulative += mark - cost
            ledger.append(
                {
                    "date": day,
                    "position": -1 if row["position"] else 0,
                    "mark_pnl_rub": round(mark, 2),
                    "funding_rub": round(row["daily_funding_included_rub"], 2),
                    "costs_rub": round(cost, 2),
                    "lqdt_on_margin_rub": None,
                    "cumulative_net_rub": round(cumulative, 2),
                    "event": events.get(day),
                }
            )
        return ledger

    def _pair_payload(
        self,
        *,
        pair: dict[str, str],
        strategy: str,
        fill_label: str,
        quantity: int,
        slippage: float,
        params: dict[str, Any],
        terms: dict[str, dict[str, Any]],
        series_rows: list[dict[str, Any]],
        trades: list[dict[str, Any]],
        ledger: list[dict[str, Any]],
        daily_rows: list[dict[str, Any]],
        margin_pair: float,
        provenance: dict[str, Any],
        notes: list[str],
        risk: dict[str, Any],
    ) -> dict[str, Any]:
        from .screener_app import MIN_DAILY_VOLUME

        pair_id, near, far = pair["pair_id"], pair["near"], pair["far"]
        closed = [trade for trade in trades if not trade["open_at_end"]]
        gross = sum(trade["gross_rub"] for trade in trades)
        costs = sum(trade["costs_rub"] for trade in trades)
        net = sum(trade["net_rub"] for trade in trades)
        hurdle = sum(trade["lqdt_on_margin_rub"] for trade in trades)
        cumulative = ledger[-1]["cumulative_net_rub"] if ledger else 0.0
        volumes = [
            min(row["near_volume"], row["far_volume"])
            for row in series_rows
            if row.get("near_volume") is not None and row.get("far_volume") is not None
            and float(row["near_volume"]) > 0 and float(row["far_volume"]) > 0
        ]
        min_leg_volume = min(volumes) if volumes else None
        trade_volume_rule = (
            all(
                min(trade[side].get("near_volume") or 0, trade[side].get("far_volume") or 0) > 0
                for trade in trades for side in ("entry", "exit")
            )
            if trades
            else True
        )
        near_group = CONTRACT_GROUP.get(near)
        far_group = CONTRACT_GROUP.get(far)
        common_group = near_group if near_group == far_group else "mixed"
        selection_out = {
            "universe": PAIR_UNIVERSE,
            "pair": pair_id,
            "strategy": strategy,
            "fill": fill_label,
            "quantity": quantity,
            "slippage_ticks": slippage,
            "parameters": params,
            "initial_capital_rub": risk["stress_inputs"]["initial_capital_rub"],
        }
        payload = {
            "schema_version": PROOF_SCHEMA,
            "universe": PAIR_UNIVERSE,
            "status": "exploratory_proxy_not_live",
            "price_role": (
                ("ISS: справочное закрытие сигнального бара, не исполнимая доходность"
                 if fill_label == "reference_same_bar" else
                 "ISS: дневной candle, заполнение по открытию, отметки по закрытию"
                 if fill_label == "next_open" else "ISS: дневной candle, следующее закрытие (прокси)")
                if strategy == "mean_reversion"
                else "ISS: расчётная цена на конец дня (прокси)"
            ),
            "selection": selection_out,
            "initial_capital_rub": selection_out["initial_capital_rub"],
            "instrument": {
                "near": near,
                "far": far,
                "family": pair["family"],
                "near_expiry": terms[near].get("expiry"),
                "far_expiry": terms[far].get("expiry"),
                "near_point_value_rub": _point_value(terms[near]) if terms[near] else None,
                "far_point_value_rub": _point_value(terms[far]) if terms[far] else None,
                "far_contract_historical_evidence": terms[far].get("historical_evidence"),
                "far_contract_specification_basis": terms[far].get("historical_specification_basis"),
                "spread_instrument": None,
            },
            "window": {
                "from": series_rows[0]["date"] if series_rows else None,
                "to": series_rows[-1]["date"] if series_rows else None,
                "observations": len(series_rows),
                "near": near,
                "far": far,
                "min_spread_volume": None,
                "min_leg_volume": min_leg_volume,
                "tradable_every_day": bool(min_leg_volume is not None and min_leg_volume >= MIN_DAILY_VOLUME),
                "min_daily_volume_rule": MIN_DAILY_VOLUME,
                "excluded_volume_observations": sum(not row.get("volume_eligible", True) for row in series_rows),
                "passes_capital_screen": risk["summary"]["all_scenarios_passed"],
                "max_pair_margin_rub": risk["pair"]["pair_margin_no_offset_rub"],
            },
            "contract": {
                "point_value_rub": _point_value(terms[near]) if terms[near] else None,
                "tick_size": terms[near].get("tick_size") if terms[near] else None,
                "tariff_group": common_group,
                "taker_rate_percent": TAKER_RATE_PERCENT.get(common_group) if common_group != "mixed" else None,
                "broker_fee_rub": 0.0,
            },
            "series": series_rows,
            "trades": trades,
            "ledger": ledger,
            "summary": {
                "trades": len(trades),
                "closed_trades": len(closed),
                "open_positions": len(trades) - len(closed),
                "winners": sum(trade["net_rub"] > 0 for trade in closed),
                "gross_rub": round(gross, 2),
                "costs_rub": round(costs, 2),
                "net_rub": round(net, 2),
                "cost_totals": _cost_totals(trades),
                "lqdt_on_margin_rub": round(hurdle, 2),
                "excess_over_lqdt_rub": round(net - hurdle, 2),
                "max_excess_drawdown_rub": round(max((float(row.get("D") or 0.0) for row in daily_rows), default=0.0), 2),
                "ledger_reconciliation_rub": round(net - cumulative, 2),
                "fx_point_value_drift_rub": None,
            },
            "notes": [
                "Сопоставимый ценовой путь LQDT — не сверка с выпиской по счёту.",
                "Текущие снапшоты контрактов/маржи — запасные допущения, а не датированные условия брокера.",
                *([f"Исторические параметры дальней ноги используют fallback: {terms[far].get('historical_specification_basis')}."] if terms[far].get("historical_specification_basis") else []),
                "Свободные средства держатся в LQDT, маржинальные средства ничего не зарабатывают (A-11); бенчмарк держит весь капитал в LQDT.",
                "Цены — candle/расчётные прокси, не исполнимые заполнения.",
                "Издержки: опубликованный тариф MOEX taker, нулевая брокерская комиссия VTB до 2026-12-31, проскальзывание как параметр.",
                "Отбор по капиталу использует фиксированные рабочие буферы и текущие снапшоты маржи, а не потери, известные заранее.",
                *notes,
                *([f"Проверка правила объёма в дни сделок: {'пройдена' if trade_volume_rule else 'нарушена'}."] if trades else []),
            ],
            "provenance": provenance,
        }
        attach_entry_levels(payload)
        payload = _clean(payload)
        payload["run_id"] = _run_id(
            {
                "selection": payload["selection"],
                "daily": payload["series"],
                **({"funding_evidence": {k:v for k,v in provenance.items() if k.startswith("funding_")}} if "funding_policy" in provenance else {}),
                "source_hashes": {
                    key: value
                    for key, value in provenance.items()
                    if isinstance(value, str) and "sha256" in key
                },
            }
        )
        return payload


def _load_contracts_for_side_cost(contracts_path: Path, near: str, far: str) -> dict[str, dict[str, Any]]:
    payload = json.loads(contracts_path.read_text(encoding="utf-8"))
    rows = list(payload.get("instruments", []))
    historical_path = contracts_path.parent / "historical-contracts.json"
    if historical_path.is_file():
        historical = json.loads(historical_path.read_text(encoding="utf-8"))
        rows.extend(historical.get("instruments", []))
    contracts: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("secid") in (near, far):
            contracts[row["secid"]] = row
    return contracts
