"""Offline pair-screener domain logic, input reader, and catalog builder."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from .account_risk import (
    DEFAULT_STRESS_VM_BUFFER,
    DEFAULT_UNCERTAINTY_BUFFER,
    assess_vtb_pair,
)
from .analytics import run_mean_reversion_baseline
from .carry import build_carry_strategy, build_carry_study, fetch_history
from .costs import tariff_profile
from .data_lake import BarSeries, MarketDataStore
from .evaluation_periods import EvaluationSplit, read_settings
from .screener import _normalized_series
from .timeframes import DEFAULT_TIMEFRAME, get_timeframe
from .universe import (
    HISTORY_WINDOWS,
    INITIAL_CAPITAL,
    MAX_QUANTITY,
    MIN_DAILY_VOLUME,
    PAIRS,
    STRATEGIES,
)
from .w0 import W0Error


class ScreenerError(W0Error):
    """Raised for unavailable data or invalid screener selections."""


@dataclass(frozen=True)
class ScreenerInputs:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    @cached_property
    def bar_store(self) -> MarketDataStore:
        return MarketDataStore(self.root)

    @property
    def benchmark(self) -> Path:
        return self.root / "data/benchmark/lqdt-lineage.json"

    @property
    def contracts(self) -> Path:
        return self.root / "data/w1/current-intake.json"

    @property
    def history(self) -> Path:
        return self.root / "data/analytics/screener-runs.jsonl"

    @property
    def screen_history(self) -> Path:
        return self.root / "data/analytics/screener-screens.jsonl"

    @property
    def search_history(self) -> Path:
        return self.root / "data/analytics/screener-searches.jsonl"

    def candles(self, pair_id: str, secid: str, timeframe: str = DEFAULT_TIMEFRAME) -> Path:
        spec = get_timeframe(timeframe)
        if spec.id == "1h":
            return self.bar_store.select_h1(secid)
        screened = self.root / "data/w1/candles-screen" / f"{secid}_{spec.file_suffix}.json"
        intake = self.root / "data/w1/candles" / f"{secid}_{spec.file_suffix}.json"
        if screened.is_file():
            return screened
        return intake

    def bars(
        self,
        pair_id: str,
        secid: str,
        timeframe: str = DEFAULT_TIMEFRAME,
        *,
        columns: tuple[str, ...] | None = None,
    ) -> BarSeries:
        return self.bar_store.read(self.candles(pair_id, secid, timeframe), columns=columns)

    def history_cache(self, pair_id: str) -> Path:
        folder = (
            "carry-history-cache"
            if pair_id == "CNYRUBF/CRZ6"
            else "carry-screen-history-cache"
        )
        return self.root / "data" / folder


def _pair(pair_id: Any) -> dict[str, str]:
    for pair in PAIRS:
        if pair["pair_id"] == pair_id:
            return pair
    raise ScreenerError("пара не входит в допущенный исследовательский реестр")


def _offline_only(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError(
        "local screener does not download data; run the documented intake first"
    )


_CANDLE_SUMMARY_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


def _candle_summary(series: BarSeries):
    cached = _CANDLE_SUMMARY_CACHE.get(series.cache_key)
    if cached is not None:
        return cached
    timestamps: set[str] = set()
    volumes: dict[str, float | None] = {}
    invalid_days: set[str] = set()
    for row in series.rows:
        begin = row.get("begin")
        if begin and row.get("close") is not None:
            timestamps.add(str(begin))
        if not isinstance(begin, str) or len(begin) < 10:
            continue
        day = begin[:10]
        value = row.get("volume")
        if value is None or isinstance(value, bool):
            invalid_days.add(day)
            continue
        number = float(value)
        if not math.isfinite(number) or number < 0:
            invalid_days.add(day)
            continue
        volumes[day] = float(volumes.get(day) or 0.0) + number
    for day in invalid_days:
        volumes[day] = None
    result = {"timestamps": frozenset(timestamps), "volumes": volumes}
    if len(_CANDLE_SUMMARY_CACHE) > 2048:
        _CANDLE_SUMMARY_CACHE.clear()
    _CANDLE_SUMMARY_CACHE[series.cache_key] = result
    return result


def _session_volumes(
    inputs: ScreenerInputs, pair: dict[str, str], timeframe: str = DEFAULT_TIMEFRAME
):
    legs = {}
    for secid in (pair["near"], pair["far"]):
        try:
            summary = _candle_summary(
                inputs.bars(
                    pair["pair_id"], secid, timeframe, columns=("begin", "close", "volume")
                )
            )
        except W0Error:
            return None
        legs[secid] = dict(summary["volumes"])
    return legs


def _leg_volumes(
    inputs: ScreenerInputs, pair: dict[str, str], timeframe: str = DEFAULT_TIMEFRAME
) -> dict[str, float] | None:
    legs = _session_volumes(inputs, pair, timeframe)
    if legs is None:
        return None
    days = set(legs[pair["near"]]) & set(legs[pair["far"]])
    eligible = [
        day
        for day in days
        if all(
            legs[secid].get(day) is not None and float(legs[secid][day]) > 0
            for secid in (pair["near"], pair["far"])
        )
    ]
    return (
        {secid: min(float(legs[secid][day]) for day in eligible) for secid in legs}
        if eligible
        else None
    )


def _positive_leg_volume_dates(
    inputs: ScreenerInputs, pair: dict[str, str], timeframe: str = DEFAULT_TIMEFRAME
) -> set[str]:
    legs = _session_volumes(inputs, pair, timeframe)
    if legs is None:
        return set()
    return {
        day
        for day in set(legs[pair["near"]]) & set(legs[pair["far"]])
        if all(
            legs[secid].get(day) is not None and float(legs[secid][day]) > 0
            for secid in (pair["near"], pair["far"])
        )
    }


def _pair_history_span(
    inputs: ScreenerInputs, pair: dict[str, str], timeframe: str = DEFAULT_TIMEFRAME
) -> tuple[str | None, str | None, int, set[str]]:
    legs = []
    for secid in (pair["near"], pair["far"]):
        try:
            summary = _candle_summary(
                inputs.bars(
                    pair["pair_id"], secid, timeframe, columns=("begin", "close", "volume")
                )
            )
        except W0Error:
            return None, None, 0, set()
        legs.append(summary["timestamps"])
    common = legs[0] & legs[1]
    if not common:
        return None, None, 0, set()
    first = min(common)
    last = max(common)
    return (
        first[:10],
        last[:10],
        len(common),
        {timestamp[:10] for timestamp in common},
    )


def _history_source(
    inputs: ScreenerInputs,
    pair_id: str,
    secid: str,
    *,
    fetch_fn: Callable[..., dict[str, Any]] = fetch_history,
) -> dict[str, Any]:
    start, end = HISTORY_WINDOWS[pair_id]
    return fetch_fn(
        secid,
        start,
        end,
        cache_dir=inputs.history_cache(pair_id),
        delay_seconds=0,
        opener=_offline_only,
    )


def _make_proof_funding(
    inputs: ScreenerInputs,
    *,
    fetch_fn: Callable[..., dict[str, Any]] = fetch_history,
):
    from .proof_funding import ProofFunding

    return ProofFunding(
        inputs,
        history_fn=lambda inp, pid, secid: _history_source(
            inp, pid, secid, fetch_fn=fetch_fn
        ),
        positive_volume_dates_fn=_positive_leg_volume_dates,
    )


def catalog(
    inputs: ScreenerInputs,
    *,
    registry=PAIRS,
    funding=None,
    initial_capital=INITIAL_CAPITAL,
    timeframe: str = DEFAULT_TIMEFRAME,
    fetch_fn: Callable[..., dict[str, Any]] = fetch_history,
) -> dict[str, Any]:
    spec = get_timeframe(timeframe)
    funding = funding or _make_proof_funding(inputs, fetch_fn=fetch_fn)
    try:
        instruments = {
            row["secid"]: row
            for row in json.loads(inputs.contracts.read_text(encoding="utf-8"))[
                "instruments"
            ]
            if isinstance(row, dict) and row.get("secid")
        }
    except (OSError, ValueError, KeyError, TypeError):
        instruments = {}
    historical_terms = inputs.root / "data/w1/historical-contracts.json"
    if historical_terms.is_file():
        try:
            rows = json.loads(historical_terms.read_text(encoding="utf-8")).get(
                "instruments", []
            )
            instruments.update(
                {row["secid"]: row for row in rows if isinstance(row, dict) and row.get("secid")}
            )
        except (OSError, ValueError, TypeError):
            pass
    pairs = []
    for pair in registry:
        pair_id = pair["pair_id"]
        near, far = pair["near"], pair["far"]
        try:
            near_sum = _candle_summary(
                inputs.bars(pair_id, near, timeframe, columns=("begin", "close", "volume"))
            )
            far_sum = _candle_summary(
                inputs.bars(pair_id, far, timeframe, columns=("begin", "close", "volume"))
            )
        except W0Error:
            near_sum = far_sum = None
        if near_sum is not None and far_sum is not None:
            common_ts = near_sum["timestamps"] & far_sum["timestamps"]
            if common_ts:
                history_from, history_to, history_observations = (
                    min(common_ts)[:10],
                    max(common_ts)[:10],
                    len(common_ts),
                )
            else:
                history_from, history_to, history_observations = None, None, 0
            near_vols, far_vols = near_sum["volumes"], far_sum["volumes"]
            common_vol_days = set(near_vols) & set(far_vols)
            pos_days = {
                day
                for day in common_vol_days
                if near_vols.get(day) is not None
                and float(near_vols[day]) > 0
                and far_vols.get(day) is not None
                and float(far_vols[day]) > 0
            }
            volumes = (
                {
                    near: min(float(near_vols[d]) for d in pos_days),
                    far: min(float(far_vols[d]) for d in pos_days),
                }
                if pos_days
                else None
            )
        else:
            history_from, history_to, history_observations = None, None, 0
            pos_days = set()
            volumes = None
        price_files = (
            inputs.candles(pair_id, near, timeframe),
            inputs.candles(pair_id, far, timeframe),
            inputs.benchmark,
            inputs.contracts,
        )
        start, end = HISTORY_WINDOWS[pair_id]
        funding_files = (
            inputs.history_cache(pair_id) / f"{near}-{start}-{end}-0.json",
            inputs.history_cache(pair_id) / f"{far}-{start}-{end}-0.json",
            inputs.benchmark,
            inputs.contracts,
        )
        tradable = volumes is not None and min(volumes.values()) >= MIN_DAILY_VOLUME
        days = (
            pos_days
            if pair["family"] == "perpetual_quarterly_same_asset"
            and spec.supports_funding_strategy
            else set()
        )
        if spec.supports_funding_strategy:
            proof_available, reasons = funding.statuses(
                pair, days, price_files, funding_files
            )
        else:
            missing_price = next(
                (path for path in price_files if not inputs.bar_store.source_available(path)),
                None,
            )
            proof_available = {
                "mean_reversion": missing_price is None and history_observations >= 2,
                "funding_only": False,
            }
            reasons = {}
            if missing_price is not None:
                reasons["mean_reversion"] = f"Нет локальных данных: {missing_price.name}."
            elif history_observations < 2:
                reasons["mean_reversion"] = "Меньше двух общих ценовых наблюдений."
        if not spec.supports_pair_proof:
            proof_available["mean_reversion"] = False
            reasons["mean_reversion"] = (
                f"{spec.label}: ценовой pair-proof не реализован для этого интервала."
            )
        if not spec.supports_funding_strategy:
            proof_available["funding_only"] = False
            reasons["funding_only"] = (
                f"{spec.label}: carry требует дневных расчётных ставок."
            )
        available = {
            "mean_reversion": tradable and proof_available["mean_reversion"],
            "funding_only": tradable and proof_available["funding_only"],
        }
        near_terms, far_terms = instruments.get(near, {}), instruments.get(far, {})
        margins = (near_terms.get("initial_margin"), far_terms.get("initial_margin"))
        margin_pct = (
            round(sum(float(value) for value in margins) / initial_capital * 100, 2)
            if all(isinstance(value, (int, float)) for value in margins)
            else None
        )
        margin = (
            sum(margins)
            if all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in margins)
            else None
        )
        pairs.append(
            {
                "id": pair_id,
                "timeframe": timeframe,
                "near": near,
                "far": far,
                "family": pair["family"],
                "from": history_from,
                "to": history_to,
                "observations": history_observations,
                "expiry": far_terms.get("expiry"),
                "current_margin_pct": margin_pct,
                "max_pair_margin_rub": margin,
                "passes_capital_screen": (
                    3 * margin + DEFAULT_STRESS_VM_BUFFER + DEFAULT_UNCERTAINTY_BUFFER
                    <= initial_capital
                )
                if margin is not None
                else None,
                "tradable_every_day": tradable if volumes is not None else None,
                "min_daily_volume_rule": MIN_DAILY_VOLUME,
                "min_leg_daily_volume": min(volumes.values()) if volumes else None,
                "strategies": available,
                "proof_strategies": proof_available,
                "data_reasons": reasons,
                "status": "excluded_thin_leg"
                if volumes is not None and not tradable
                else "local_inputs_available"
                if any(available.values())
                else "proof_ready_thin_leg"
                if any(proof_available.values())
                else "intake_required",
            }
        )
    return {
        "timeframe": timeframe,
        "pairs": pairs,
        "strategies": STRATEGIES,
        "execution_defaults": {"slippage_ticks": 1.0, "quantity": 1},
        "min_daily_volume": MIN_DAILY_VOLUME,
        "price_warning": "Исторические candle/расчётные значения — аналитические ценовые прокси, не исполнения.",
    }


def _parameters(selection: dict[str, Any], strategy: str) -> dict[str, float | int]:
    defaults = STRATEGIES[strategy]["parameters"]
    provided = selection.get("parameters", {})
    if not isinstance(provided, dict) or set(provided) - set(defaults):
        raise ScreenerError("неизвестный параметр стратегии")
    values = {**defaults, **provided}
    lookback = values["lookback"]
    if (
        isinstance(lookback, bool)
        or not isinstance(lookback, int)
        or not 5 <= lookback <= 80
    ):
        raise ScreenerError("lookback должен быть целым числом от 5 до 80 наблюдений")
    if strategy == "mean_reversion":
        for field in ("entry_z", "exit_z"):
            value = values[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ScreenerError(f"{field} должен быть конечным числом")
        if (
            not 0.5 <= values["entry_z"] <= 5
            or not 0 <= values["exit_z"] < values["entry_z"]
        ):
            raise ScreenerError(
                "entry должен быть в диапазоне 0.5–5, а exit — неотрицательным и ниже entry"
            )
    return values


def execution_settings(selection: dict[str, Any]) -> dict[str, float | int]:
    slippage, quantity = selection.get("slippage_ticks", 1.0), selection.get("quantity", 1)
    if (
        isinstance(slippage, bool)
        or not isinstance(slippage, (int, float))
        or not 0 <= slippage <= 10
    ):
        raise ScreenerError("slippage должен быть 0–10 тиков на ногу и сторону")
    if (
        isinstance(quantity, bool)
        or not isinstance(quantity, int)
        or not 1 <= quantity <= MAX_QUANTITY
    ):
        raise ScreenerError(
            f"контрактов на ногу должно быть целым числом от 1 до {MAX_QUANTITY}"
        )
    return {"slippage_ticks": float(slippage), "quantity": quantity}


def pair_price_inputs(
    inputs: ScreenerInputs,
    pair: dict[str, str],
    funding: Any,
    timeframe: str = DEFAULT_TIMEFRAME,
):
    pair_id, near, far = pair["pair_id"], pair["near"], pair["far"]
    spec = get_timeframe(timeframe)
    book = (
        funding.timeline(pair)
        if pair["family"] == "perpetual_quarterly_same_asset"
        and spec.supports_funding_strategy
        else None
    )
    series = _normalized_series(
        inputs.bars(
            pair_id,
            near,
            timeframe,
            columns=("begin", "open", "close", "volume", "price_proxy_kind"),
        ),
        inputs.bars(
            pair_id,
            far,
            timeframe,
            columns=("begin", "open", "close", "volume", "price_proxy_kind"),
        ),
        inputs.benchmark,
        inputs.contracts,
        near=near,
        far=far,
        prior_benchmark_dates=book.no_clearing_dates if book else (),
        timeframe=timeframe,
    )
    return series, book


def build_result(
    inputs: ScreenerInputs,
    selection: dict[str, Any],
    *,
    through: str | None = None,
    validation_share: float | None = None,
    fetch_fn: Callable[..., dict[str, Any]] = fetch_history,
) -> dict[str, Any]:
    from .proof_funding import FundingWindow

    if not isinstance(selection, dict):
        raise ScreenerError("выбор должен быть объектом")
    pair = _pair(selection.get("pair"))
    pair_id, near, far = pair["pair_id"], pair["near"], pair["far"]
    strategy = selection.get("strategy")
    if not isinstance(strategy, str) or strategy not in STRATEGIES:
        raise ScreenerError("неизвестная стратегия")
    execution = execution_settings(selection)
    params = _parameters(selection, strategy)
    settings = read_settings(inputs.root)
    timeframe = settings["timeframe"]
    spec = get_timeframe(timeframe)
    if strategy == "funding_only" and not spec.supports_funding_strategy:
        raise ScreenerError(f"{spec.label}: carry требует дневных расчётных ставок.")
    funding = _make_proof_funding(inputs, fetch_fn=fetch_fn)
    entry = catalog(
        inputs,
        registry=(pair,),
        funding=funding,
        initial_capital=settings["initial_capital_rub"],
        timeframe=timeframe,
        fetch_fn=fetch_fn,
    )["pairs"][0]
    if not entry["strategies"][strategy]:
        raise ScreenerError(
            entry["data_reasons"].get(strategy, "Не пройдено правило ликвидности пары.")
        )
    share = (
        (
            read_settings(inputs.root)["validation_percent"]
            if validation_share is None
            else validation_share
        )
        if through is None
        else 0
    )
    evidence = None

    if strategy == "mean_reversion":
        series, book = pair_price_inputs(inputs, pair, funding, timeframe=timeframe)
        if through is not None:
            series = {
                **series,
                "rows": [
                    r
                    for r in series["rows"]
                    if (
                        r.get("session_date", r["date"][:10])
                        if len(through) == 10 and spec.intraday
                        else r["date"]
                    )
                    <= through
                ],
            }
        risk = assess_vtb_pair(inputs.contracts, near, far, quantity=execution["quantity"])
        if not risk["summary"]["all_scenarios_passed"]:
            raise ScreenerError("пара не проходит стресс-экран рабочего капитала")
        funding_swaprates: dict[str, float] = {}
        funding_source = None
        if book is not None:
            funding_source = funding.source(pair_id, near)
            requested_dates = [row["date"] for row in series["rows"]]
            evidence = FundingWindow(book, requested_dates, share).resolve(through)
            funding_swaprates = evidence.rates
        report = run_mean_reversion_baseline(
            series,
            lookback=params["lookback"],
            entry_z=params["entry_z"],
            exit_z=params["exit_z"],
            quantity=execution["quantity"],
            analysis_role="diagnostic_only",
            cost_profile=tariff_profile(execution["slippage_ticks"]),
            fill="open",
            cash_in_lqdt=True,
            funding_swaprates=funding_swaprates,
        )
        value_key = "strategy_value"
        summary = report["summary"]
        trades = summary["trade_count"]
        open_positions = 0
        costs = summary["modeled_costs"]
        provenance = dict(report["input_provenance"])
        if funding_source is not None:
            provenance["near_funding_history_sha256"] = funding_source["source"][
                "raw_response_sha256"
            ]
        price_role = (
            "ISS: дневной candle, заполнение по открытию, отметки по закрытию"
            if not spec.intraday
            else f"ISS: свеча {spec.label}, заполнение по открытию, отметки по закрытию"
        )
        notes = [
            *(
                [
                    "Фандинг включён: биржевой SWAPRATE или явно отмеченная расчётная ставка; сумма = −position × SWAPRATE × лот × quantity."
                ]
                if funding_source is not None
                else [
                    "На выбранном внутридневном таймфрейме SWAPRATE не включён: дневную ставку нельзя размножать на внутридневные бары."
                    if spec.intraday
                    else "Для пары без вечного фьючерса фандинг неприменим."
                ]
            ),
            *(
                [
                    "Итоговый EOD SWAPRATE отражает клиринговый расчёт MOEX, включая действовавшие лимиты; минутный D/K1/K2 не пересчитывается. Лот берётся из текущего intake снапшота, не из эффективной исторической спецификации. Это не брокерская выписка."
                ]
                if funding_source is not None
                else []
            ),
            "Сигнал по закрытию дня, исполнение по следующему открытию дня (прокси реакции в пределах минут–часов)."
            if not spec.intraday
            else f"Сигнал по закрытию бара {spec.label}; исполнение по открытию следующего бара (прокси).",
            "Открытая на конец выборки позиция отмечается как принудительный прокси-выход, а не как исполнимая сделка.",
        ]
    else:
        window = funding.carry_window(pair, share)
        evidence = window.resolve(through)
        near_source = funding.source(pair_id, near)
        far_source = funding.source(pair_id, far)
        study = build_carry_study(
            near_source,
            far_source,
            inputs.contracts,
            near=near,
            far=far,
            funding_regimes=None if pair_id == "CNYRUBF/CRZ6" else (),
            funding_marks=evidence.marks,
            no_clearing_dates=window.book.no_clearing_dates,
            through=through,
        )
        report = build_carry_strategy(
            study,
            inputs.benchmark,
            inputs.contracts,
            transaction_cost_scenario="tariff",
            warmup_observations=params["lookback"],
            strategy_variant="funding_only",
            quantity=execution["quantity"],
            execution_delay_rows=0,
            cash_in_lqdt=True,
            slippage_ticks=execution["slippage_ticks"],
        )
        risk = assess_vtb_pair(inputs.contracts, near, far, quantity=execution["quantity"])
        if not risk["summary"]["all_scenarios_passed"]:
            raise ScreenerError("пара не проходит стресс-экран рабочего капитала")
        value_key = "strategy_value_proxy"
        summary = report["summary"]
        trades = summary["closed_trade_count"]
        open_positions = summary["open_trade_count"]
        costs = summary["transaction_costs"]
        provenance = {
            "near_history_hashes": near_source["source"]["raw_response_sha256"],
            "far_history_hashes": far_source["source"]["raw_response_sha256"],
            "benchmark_sha256": hashlib.sha256(
                inputs.benchmark.read_bytes()
            ).hexdigest(),
            "contracts_sha256": hashlib.sha256(
                inputs.contracts.read_bytes()
            ).hexdigest(),
        }
        price_role = "ISS: расчётная цена на конец дня (прокси)"
        notes = [
            "Сигнал фондирования использует фондирование до предыдущего клиринга; прокси исполнения — расчёт в тот же день; выход в тот же день теряет фондирование этого дня.",
            *(
                ["Non-CNY funding-rule caps are not reconstructed."]
                if pair_id != "CNYRUBF/CRZ6"
                else []
            ),
        ]
    if evidence:
        provenance.update(evidence.provenance)
        notes.extend(evidence.notes())
    daily = [
        {
            "date": row["date"],
            "V": row[value_key],
            "B": row["benchmark_value"],
            "X": row["X"],
            "D": row["D"],
        }
        for row in report["daily"]
    ]
    first, last = daily[0], daily[-1]
    initial = float(settings["initial_capital_rub"])
    result = {
        "selection": {
            "pair": pair_id,
            "strategy": strategy,
            "parameters": params,
            "execution": execution,
        },
        "status": "exploratory_proxy_not_live",
        "price_role": price_role,
        "period": {
            "from": first["date"],
            "to": last["date"],
            "observations": len(daily),
        },
        "metrics": {
            "strategy_return_pct": round((last["V"] / initial - 1) * 100, 3),
            "lqdt_return_pct": round((last["B"] / initial - 1) * 100, 3),
            "excess_rub": last["X"],
            "excess_pct_points": round(last["X"] / initial * 100, 3),
            "max_excess_drawdown_rub": round(max(row["D"] for row in daily), 2),
            "worst_lqdt_shortfall_rub": round(min(row["X"] for row in daily), 2),
            "closed_trades": trades,
            "open_positions": open_positions,
            "modeled_costs_rub": costs,
            "current_pair_margin_rub": risk["pair"]["pair_margin_no_offset_rub"],
            "stress_remaining_capital_rub": risk["summary"][
                "most_severe_remaining_capital_rub"
            ],
        },
        "daily": daily,
        "notes": [
            "Сопоставимый ценовой путь LQDT — не сверка с выпиской по счёту.",
            "Текущие снапшоты контрактов/маржи — запасные допущения, а не датированные условия брокера.",
            "Издержки: опубликованный тариф MOEX taker, нулевая брокерская комиссия VTB до 2026-12-31, slippage как параметр.",
            "Свободные средства держатся в LQDT, маржинальные средства ничего не зарабатывают (A-11); бенчмарк держит весь капитал в LQDT.",
            "Отбор по капиталу использует фиксированные рабочие буферы и текущие снапшоты маржи, а не потери, известные заранее.",
            *notes,
        ],
        "provenance": provenance,
    }
    identity = {
        "selection": result["selection"],
        "daily": daily,
        "source_hashes": {
            key: value
            for key, value in provenance.items()
            if "sha256" in key or "hashes" in key
        },
    }
    if evidence and evidence.provenance:
        identity["funding_evidence"] = evidence.provenance
    if through is None:
        split = EvaluationSplit.from_dates((r["date"] for r in daily), share)
        opening = (
            daily[len(split.training) - 1]
            if split.validation
            else {"V": initial, "B": initial}
        )
        strategy_change = last["V"] - opening["V"]
        benchmark_change = last["B"] - opening["B"]
        result["metrics_scope"] = "full_history"
        result["evaluation"] = {
            **split.metadata(),
            "result": {
                "strategy_change_rub": round(strategy_change, 2),
                "lqdt_change_rub": round(benchmark_change, 2),
                "excess_rub": round(strategy_change - benchmark_change, 2),
            },
        }
        identity["evaluation"] = result["evaluation"]
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    result["run_id"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return result
