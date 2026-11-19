"""Local-only research screener over the existing analytical engines."""

from __future__ import annotations

import hashlib
import json
import math
import select
import socket
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .account_risk import (
    DEFAULT_STRESS_VM_BUFFER,
    DEFAULT_UNCERTAINTY_BUFFER,
    assess_vtb_pair,
)
from .analytics import run_mean_reversion_baseline
from .carry import (
    build_carry_strategy,
    build_carry_study,
    fetch_history,
)
from .costs import tariff_profile
from .evaluation_periods import EvaluationSplit, read_settings, save_settings
from .proof import ProofService
from .proof_funding import FundingWindow, ProofFunding
from .screener import SCREEN_PAIRS, _normalized_series
from .w0 import W0Error

EXTRA_PAIRS = (
    {
        "pair_id": "SLVRUBF/S2Z6",
        "near": "SLVRUBF",
        "far": "S2Z6",
        "family": "perpetual_quarterly_same_asset",
    },
    {
        "pair_id": "EURRUBF/EuZ6",
        "near": "EURRUBF",
        "far": "EuZ6",
        "family": "perpetual_quarterly_same_asset",
    },
    {
        "pair_id": "BTCUSDF/BTZ6",
        "near": "BTCUSDF",
        "far": "BTZ6",
        "family": "perpetual_quarterly_same_asset",
    },
    {
        "pair_id": "GLZ6/GLH7",
        "near": "GLZ6",
        "far": "GLH7",
        "family": "dated_calendar_same_asset",
    },
    {
        "pair_id": "CRZ6/CRH7",
        "near": "CRZ6",
        "far": "CRH7",
        "family": "dated_calendar_same_asset",
    },
)
PERPETUAL_HISTORY_STARTS = {
    "CNYRUBF": "2022-04-26", "GLDRUBF": "2023-07-11", "IMOEXF": "2023-11-14",
    "GAZPF": "2024-10-01", "SBERF": "2024-10-01", "RGBIF": "2025-12-23",
    "USDRUBF": "2022-04-26", "SLVRUBF": "2026-03-31", "EURRUBF": "2022-04-26",
    "BTCUSDF": "2026-09-22",
}
PERPETUAL_HISTORY_TILL = "2026-10-27"
HISTORICAL_FAR_BY_NEAR = {
    "CNYRUBF": ("CRH3", "CRH4", "CRH6", "CRM2", "CRM3", "CRM4", "CRM5", "CRM6", "CRU2", "CRU4", "CRU5", "CRU6", "CRZ2", "CRZ3", "CRZ4", "CRZ5"),
    "IMOEXF": ("MMH4", "MMH5", "MMH6", "MMM4", "MMM5", "MMM6", "MMU4", "MMU5", "MMU6", "MMZ3", "MMZ4", "MMZ5"),
    "GAZPF": ("GZH5", "GZH6", "GZM5", "GZM6", "GZU5", "GZU6", "GZZ4", "GZZ5"),
    "SBERF": ("SRH5", "SRH6", "SRM5", "SRM6", "SRU5", "SRU6", "SRZ4", "SRZ5"),
    "RGBIF": ("RBH6", "RBM6", "RBU6"),
    "USDRUBF": ("SiH3", "SiH4", "SiH5", "SiH6", "SiM2", "SiM3", "SiM4", "SiM5", "SiM6", "SiU2", "SiU3", "SiU4", "SiU5", "SiU6", "SiZ2", "SiZ3", "SiZ4", "SiZ5"),
    "SLVRUBF": ("S2M6", "S2U6"),
    "EURRUBF": ("EuH3", "EuH4", "EuH5", "EuH6", "EuM2", "EuM3", "EuM4", "EuM5", "EuM6", "EuU2", "EuU3", "EuU4", "EuU5", "EuU6", "EuZ2", "EuZ3", "EuZ4", "EuZ5"),
}
HISTORICAL_PERPETUAL_PAIRS = tuple(
    {"pair_id": f"{near}/{far}", "near": near, "far": far, "family": "perpetual_quarterly_same_asset"}
    for near, far_values in HISTORICAL_FAR_BY_NEAR.items()
    for far in far_values
)
GLD_HISTORICAL_FAR = (
    "GLU6", "GLM6", "GLH6", "GLZ5", "GLU5", "GLM5", "GLH5", "GLZ4", "GLU4", "GLM4", "GLH4", "GLZ3", "GLU3",
)
GLD_HISTORICAL_PAIRS = tuple(
    {"pair_id": f"GLDRUBF/{far}", "near": "GLDRUBF", "far": far, "family": "perpetual_quarterly_same_asset"}
    for far in GLD_HISTORICAL_FAR
)
PAIRS = (
    {
        "pair_id": "CNYRUBF/CRZ6",
        "near": "CNYRUBF",
        "far": "CRZ6",
        "family": "perpetual_quarterly_same_asset",
    },
    *SCREEN_PAIRS,
    *EXTRA_PAIRS,
    *HISTORICAL_PERPETUAL_PAIRS,
    *GLD_HISTORICAL_PAIRS,
)
STRATEGIES = {
    "mean_reversion": {
        "label": "Возврат спреда к среднему",
        "parameters": {"lookback": 20, "entry_z": 2.0, "exit_z": 0.5},
    },
    "funding_only": {
        "label": "Фондирование перпетуального свопа (carry)",
        "parameters": {"lookback": 20},
    },
}
MIN_DAILY_VOLUME = 100
MAX_QUANTITY = 50
INITIAL_CAPITAL = 30_000.0
HISTORY_WINDOWS = {
    **{
        pair["pair_id"]: (PERPETUAL_HISTORY_STARTS.get(pair["near"], "2026-03-23"), PERPETUAL_HISTORY_TILL)
        for pair in PAIRS
    },
    "CNYRUBF/CRZ6": ("2025-06-17", PERPETUAL_HISTORY_TILL),
    **{pair["pair_id"]: ("2023-07-11", PERPETUAL_HISTORY_TILL) for pair in GLD_HISTORICAL_PAIRS},
}


class ScreenerError(W0Error):
    """Raised for unavailable data or invalid screener selections."""


@dataclass(frozen=True)
class ScreenerInputs:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

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

    def candles(self, pair_id: str, secid: str) -> Path:
        primary = self.root / "data/w1/candles-screen" / f"{secid}_1d.json"
        original = self.root / "data/w1/candles" / f"{secid}_1d.json"
        return primary if primary.is_file() else original

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


def _leg_volumes(inputs: ScreenerInputs, pair: dict[str, str]) -> dict[str, int] | None:
    """Smallest daily volume of each leg over the days both legs traded; None if candles are missing."""
    try:
        legs = {
            secid: {
                row["begin"][:10]: row["volume"]
                for row in json.loads(
                    inputs.candles(pair["pair_id"], secid).read_text(encoding="utf-8")
                )["rows"]
            }
            for secid in (pair["near"], pair["far"])
        }
    except (OSError, ValueError, KeyError, TypeError):
        return None
    days = set(legs[pair["near"]]) & set(legs[pair["far"]])
    eligible = [day for day in days if all(legs[secid][day] is not None and float(legs[secid][day]) > 0 for secid in (pair["near"], pair["far"]))]
    return {secid: min(legs[secid][day] for day in eligible) for secid in legs} if eligible else None


def _positive_leg_volume_dates(inputs: ScreenerInputs, pair: dict[str, str]) -> set[str]:
    try:
        legs = {
            secid: {
                row["begin"][:10]: row.get("volume")
                for row in json.loads(inputs.candles(pair["pair_id"], secid).read_text(encoding="utf-8")).get("rows", [])
                if row.get("begin")
            }
            for secid in (pair["near"], pair["far"])
        }
    except (OSError, ValueError, KeyError, TypeError):
        return set()
    return {
        day for day in set(legs[pair["near"]]) & set(legs[pair["far"]])
        if all(legs[secid][day] is not None and float(legs[secid][day]) > 0 for secid in (pair["near"], pair["far"]))
    }


def _pair_history_span(inputs: ScreenerInputs, pair: dict[str, str]) -> tuple[str | None, str | None, int, set[str]]:
    try:
        legs = []
        for secid in (pair["near"], pair["far"]):
            payload = json.loads(inputs.candles(pair["pair_id"], secid).read_text(encoding="utf-8"))
            legs.append({str(row.get("begin", ""))[:10] for row in payload.get("rows", []) if row.get("begin")})
        common = legs[0] & legs[1]
        ordered = sorted(common)
        return (ordered[0], ordered[-1], len(ordered), common) if ordered else (None, None, 0, set())
    except (OSError, ValueError, KeyError, TypeError):
        return None, None, 0, set()


def catalog(inputs: ScreenerInputs, *, registry=PAIRS, funding=None, initial_capital=INITIAL_CAPITAL) -> dict[str, Any]:
    funding = funding or ProofFunding(inputs)
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
            rows = json.loads(historical_terms.read_text(encoding="utf-8")).get("instruments", [])
            instruments.update({row["secid"]: row for row in rows if isinstance(row, dict) and row.get("secid")})
        except (OSError, ValueError, TypeError):
            pass
    pairs = []
    for pair in registry:
        pair_id = pair["pair_id"]
        history_from, history_to, history_observations, _ = _pair_history_span(inputs, pair)
        near, far = pair["near"], pair["far"]
        price_files = (
            inputs.candles(pair_id, near),
            inputs.candles(pair_id, far),
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
        volumes = _leg_volumes(inputs, pair)
        tradable = volumes is not None and min(volumes.values()) >= MIN_DAILY_VOLUME
        days = _positive_leg_volume_dates(inputs, pair) if pair["family"] == "perpetual_quarterly_same_asset" else set()
        proof_available, reasons = funding.statuses(pair, days, price_files, funding_files)
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
        margin = sum(margins) if all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in margins) else None
        pairs.append(
            {
                "id": pair_id,
                "near": near,
                "far": far,
                "family": pair["family"],
                "from": history_from,
                "to": history_to,
                "observations": history_observations,
                "expiry": far_terms.get("expiry"),
                "current_margin_pct": margin_pct,
                "max_pair_margin_rub": margin,
                "passes_capital_screen": (3 * margin + DEFAULT_STRESS_VM_BUFFER + DEFAULT_UNCERTAINTY_BUFFER <= initial_capital) if margin is not None else None,
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
    if isinstance(slippage, bool) or not isinstance(slippage, (int, float)) or not 0 <= slippage <= 10:
        raise ScreenerError("slippage должен быть 0–10 тиков на ногу и сторону")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_QUANTITY:
        raise ScreenerError(f"контрактов на ногу должно быть целым числом от 1 до {MAX_QUANTITY}")
    return {"slippage_ticks": float(slippage), "quantity": quantity}


def _history_source(inputs: ScreenerInputs, pair_id: str, secid: str) -> dict[str, Any]:
    start, end = HISTORY_WINDOWS[pair_id]
    return fetch_history(
        secid,
        start,
        end,
        cache_dir=inputs.history_cache(pair_id),
        delay_seconds=0,
        opener=_offline_only,
    )


def pair_price_inputs(inputs: ScreenerInputs, pair: dict[str, str], funding: ProofFunding):
    pair_id, near, far = pair["pair_id"], pair["near"], pair["far"]
    book = funding.timeline(pair) if pair["family"] == "perpetual_quarterly_same_asset" else None
    series = _normalized_series(
        inputs.candles(pair_id, near), inputs.candles(pair_id, far),
        inputs.benchmark, inputs.contracts, near=near, far=far,
        prior_benchmark_dates=book.no_clearing_dates if book else (),
    )
    return series, book


def build_result(
    inputs: ScreenerInputs,
    selection: dict[str, Any],
    *,
    through: str | None = None,
    validation_share: float | None = None,
) -> dict[str, Any]:
    if not isinstance(selection, dict):
        raise ScreenerError("выбор должен быть объектом")
    pair = _pair(selection.get("pair"))
    pair_id, near, far = pair["pair_id"], pair["near"], pair["far"]
    strategy = selection.get("strategy")
    if not isinstance(strategy, str) or strategy not in STRATEGIES:
        raise ScreenerError("неизвестная стратегия")
    execution = execution_settings(selection)
    params = _parameters(selection, strategy)
    funding = ProofFunding(inputs)
    entry = catalog(inputs, registry=(pair,), funding=funding)["pairs"][0]
    if not entry["strategies"][strategy]:
        raise ScreenerError(entry["data_reasons"].get(strategy, "Не пройдено правило ликвидности пары."))
    share = (read_settings(inputs.root)["validation_percent"] if validation_share is None else validation_share) if through is None else 0
    evidence = None

    if strategy == "mean_reversion":
        series, book = pair_price_inputs(inputs, pair, funding)
        if through is not None:
            series = {**series, "rows": [r for r in series["rows"] if r["date"] <= through]}
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
            provenance["near_funding_history_sha256"] = funding_source["source"]["raw_response_sha256"]
        price_role = "ISS: дневной candle, заполнение по открытию, отметки по закрытию"
        notes = [
            *( ["Фандинг включён: биржевой SWAPRATE или явно отмеченная расчётная ставка; сумма = −position × SWAPRATE × лот × quantity."] if funding_source is not None else ["Для пары без вечного фьючерса фандинг неприменим."]),
            *( ["Итоговый EOD SWAPRATE отражает клиринговый расчёт MOEX, включая действовавшие лимиты; минутный D/K1/K2 не пересчитывается. Лот берётся из текущего intake снапшота, не из эффективной исторической спецификации. Это не брокерская выписка."] if funding_source is not None else []),
            "Сигнал по закрытию дня, исполнение по следующему открытию дня (прокси реакции в пределах минут–часов).",
            "Открытая на конец выборки позиция отмечается как принудительный прокси-выход, а не как исполнимая сделка.",
        ]
    else:
        window = funding.carry_window(pair, share)
        evidence = window.resolve(through)
        near_source = funding.source(pair_id, near)
        far_source = funding.source(pair_id, far)
        study = build_carry_study(
            near_source, far_source, inputs.contracts, near=near, far=far,
            funding_regimes=None if pair_id == "CNYRUBF/CRZ6" else (),
            funding_marks=evidence.marks, no_clearing_dates=window.book.no_clearing_dates, through=through,
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
        {"date": row["date"], "V": row[value_key], "B": row["benchmark_value"], "X": row["X"], "D": row["D"]}
        for row in report["daily"]
    ]
    first, last = daily[0], daily[-1]
    initial = INITIAL_CAPITAL
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
        opening = daily[len(split.training) - 1] if split.validation else {"V": initial, "B": initial}
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


class ScreenerStore:
    def __init__(self, inputs: ScreenerInputs):
        self.inputs = inputs
        self.lock = threading.Lock()
        self.proof_search_lock = threading.Lock()
        self.proof_profile_lock = threading.Lock()
        self.proof_windows_lock = threading.Lock()
        self._calendar: Any = None
        self._proof: ProofService | None = None

    @property
    def proof(self) -> ProofService:
        if self._proof is None:
            self._proof = ProofService(self.inputs.root)
        return self._proof

    @property
    def calendar(self) -> Any:
        if self._calendar is None:
            from .calendar_spreads import CalendarData

            self._calendar = CalendarData(self.inputs.root)
        return self._calendar

    @staticmethod
    def _recent(path: Path) -> list[dict[str, Any]]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in lines[-20:] if line]

    def list_runs(self) -> list[dict[str, Any]]:
        return self._recent(self.inputs.history)

    def list_searches(self) -> list[dict[str, Any]]:
        return self._recent(self.inputs.search_history)

    def _save(self, path: Path, result: dict[str, Any]) -> None:
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if any(run["run_id"] == result["run_id"] for run in self._recent(path)):
                return
            record = {**result, "recorded_at": datetime.now(UTC).isoformat()}
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                )

    def save(self, result: dict[str, Any]) -> None:
        self._save(self.inputs.history, result)

    def save_screen(self, result: dict[str, Any]) -> None:
        compact = {
            key: result[key] for key in ("run_id", "schema_version", "selection")
        }
        compact["pairs"] = [
            {key: row[key] for key in ("id", "status", "metrics", "evaluation") if key in row}
            for row in result["pairs"]
        ]
        self._save(self.inputs.screen_history, compact)

    def save_search(self, result: dict[str, Any]) -> None:
        self._save(self.inputs.search_history, result)

    def save_proof_search(self, result: dict[str, Any]) -> None:
        self._save(self.inputs.history.parent / "proof-searches.jsonl", result)


def make_handler(store: ScreenerStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The browser superseded this run with a newer selection.

        def _json(self, status: int, value: Any) -> None:
            self._send(
                status,
                json.dumps(value, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _local_request(self) -> bool:
            host = self.headers.get("Host", "")
            port = self.server.server_port
            return host in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def do_GET(self) -> None:
            if not self._local_request():
                self._json(403, {"error": "local host only"})
            elif (path := urlsplit(self.path).path) in {"/", "/proof"}:
                self._send(
                    200,
                    Path(__file__).with_name("proof_ui.html").read_bytes(),
                    "text/html; charset=utf-8",
                )
            elif path == "/vendor/lightweight-charts.standalone.production.js":
                vendor = Path(__file__).with_name("vendor") / "lightweight-charts.standalone.production.js"
                try:
                    self._send(200, vendor.read_bytes(), "application/javascript; charset=utf-8")
                except OSError:
                    self._json(404, {"error": "not found"})
            elif path == "/api/settings":
                try:
                    self._json(200, read_settings(store.inputs.root))
                except (ValueError, OSError) as exc:
                    self._json(400, {"error": str(exc)})
            elif path == "/api/proof/catalog":
                try:
                    self._json(200, store.proof.catalog())
                except (W0Error, OSError) as exc:
                    self._json(400, {"error": str(exc)})
            elif path == "/api/catalog":
                self._json(200, catalog(store.inputs))
            elif path == "/api/runs":
                self._json(200, store.list_runs())
            elif path == "/api/searches":
                self._json(200, store.list_searches())
            else:
                self._json(404, {"error": "not found"})

        def _check_connected(self) -> None:
            # Abort expensive group replay after its browser fetch has been cancelled.
            if select.select([self.connection], [], [], 0)[0] and not self.connection.recv(1, socket.MSG_PEEK):
                raise ConnectionAbortedError("Клиент отменил подбор.")

        def do_POST(self) -> None:
            if not self._local_request() or self.headers.get("Origin") not in (
                None,
                f"http://127.0.0.1:{self.server.server_port}",
                f"http://localhost:{self.server.server_port}",
            ):
                self._json(403, {"error": "local origin only"})
                return
            path = urlsplit(self.path).path
            if path not in {"/api/settings", "/api/run", "/api/screen", "/api/optimize", "/api/proof/run", "/api/proof/optimize", "/api/proof/plateau", "/api/proof/windows"}:
                self._json(404, {"error": "not found"})
                return
            if (
                self.headers.get("Content-Type", "").split(";", 1)[0]
                != "application/json"
            ):
                self._json(415, {"error": "JSON body required"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                limit = 192 * 1024 if path in {"/api/proof/optimize", "/api/proof/plateau", "/api/proof/windows"} else 8192
                if not 0 < size <= limit:
                    raise ScreenerError(f"размер запроса должен быть 1–{limit} байт")
                selection = json.loads(self.rfile.read(size))
                if path == "/api/settings":
                    with store.lock:
                        result = save_settings(store.inputs.root, selection)
                elif path in {"/api/proof/run", "/api/proof/optimize", "/api/proof/plateau", "/api/proof/windows"}:
                    if not isinstance(selection, dict):
                        raise ScreenerError("выбор должен быть объектом")
                    search = path == "/api/proof/optimize"
                    from .proof_search import research_run

                    diagnostics = selection.pop("include_plateau", False) is True
                    if path in {"/api/proof/windows", "/api/proof/plateau"}:
                        from .proof_batch import checked_plateau
                        from .proof_windows import window_summaries

                        if path == "/api/proof/windows":
                            lock, read = store.proof_windows_lock, window_summaries
                        else:
                            lock, read = store.proof_profile_lock, checked_plateau
                        while not lock.acquire(timeout=0.15):
                            self._check_connected()
                        try:
                            self._check_connected()
                            result = read(store.proof, selection, check_cancelled=self._check_connected)
                        finally:
                            lock.release()
                    elif search:
                        if not store.proof_search_lock.acquire(blocking=False):
                            self._json(409, {"error": "Предыдущий подбор ещё выполняется. Повторите через несколько секунд."})
                            return
                        try:
                            result = research_run(store.proof, selection, optimize=True, check_cancelled=self._check_connected)
                            if result.get("run_id"):
                                store.save_proof_search(result)
                        finally:
                            store.proof_search_lock.release()
                    else:
                        result = research_run(store.proof, selection, optimize=False, include_plateau=diagnostics)
                elif path == "/api/run":
                    result = build_result(store.inputs, selection)
                    store.save(result)
                else:
                    from .screener_search import optimize, screen_table

                    if path == "/api/screen":
                        result = screen_table(store.inputs, selection)
                        store.save_screen(result)
                    else:
                        result = optimize(store.inputs, selection)
                        store.save_search(result)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                return
            except (ValueError, OSError) as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, result)

        def log_message(self, _format: str, *_args: Any) -> None:
            pass

    return Handler


def serve(inputs: ScreenerInputs, port: int = 8765) -> None:
    if not 0 <= port <= 65535:
        raise ScreenerError("порт должен быть в диапазоне 0–65535")
    server = ThreadingHTTPServer(
        ("127.0.0.1", port), make_handler(ScreenerStore(inputs))
    )
    print(f"Local research screener: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
