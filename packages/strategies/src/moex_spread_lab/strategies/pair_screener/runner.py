"""Offline pair-screener strategy validation and branch runners."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from moex_spread_lab.accounting.account_risk import assess_vtb_pair
from moex_spread_lab.accounting.costs import tariff_profile
from moex_spread_lab.core.settings import read_settings
from moex_spread_lab.core.timeframes import TimeframeSpec, get_timeframe
from moex_spread_lab.core.universe import MAX_QUANTITY, STRATEGIES

from ..funding_carry.carry import build_carry_strategy, build_carry_study, fetch_history
from ..funding_carry.proof_funding import FundingWindow
from ..pair_mean_reversion.analytics import run_mean_reversion_baseline
from .catalog import catalog
from .inputs import ScreenerError, ScreenerInputs, _make_proof_funding, _pair, pair_price_inputs
from .result_builder import (
    StrategyRunOutcome,
    assemble_screener_result,
    mean_reversion_notes,
)


def _parameters(selection: dict[str, Any], strategy: str) -> dict[str, float | int]:
    defaults: dict[str, Any] = dict(STRATEGIES[strategy]["parameters"])
    provided = selection.get("parameters", {})
    if not isinstance(provided, dict) or set(provided) - set(defaults):
        raise ScreenerError("неизвестный параметр стратегии")
    values: dict[str, Any] = {**defaults, **provided}
    lookback = values["lookback"]
    if isinstance(lookback, bool) or not isinstance(lookback, int) or not 5 <= lookback <= 80:
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
        entry_z, exit_z = float(values["entry_z"]), float(values["exit_z"])
        if not 0.5 <= entry_z <= 5 or not 0 <= exit_z < entry_z:
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
        raise ScreenerError(f"контрактов на ногу должно быть целым числом от 1 до {MAX_QUANTITY}")
    return {"slippage_ticks": float(slippage), "quantity": quantity}


@dataclass
class _BranchReq:
    inputs: ScreenerInputs
    pair: dict[str, str]
    spec: TimeframeSpec
    timeframe: str
    funding: Any
    params: dict[str, float | int]
    execution: dict[str, float | int]
    through: str | None
    share: float


def _run_mean_reversion_branch(req: _BranchReq) -> StrategyRunOutcome:
    pair_id, near, far = req.pair["pair_id"], req.pair["near"], req.pair["far"]
    series, book = pair_price_inputs(req.inputs, req.pair, req.funding, timeframe=req.timeframe)
    if req.through is not None:
        series = {
            **series,
            "rows": [
                r
                for r in series["rows"]
                if (
                    r.get("session_date", r["date"][:10])
                    if len(req.through) == 10 and req.spec.intraday
                    else r["date"]
                )
                <= req.through
            ],
        }
    qty = int(req.execution["quantity"])
    risk = assess_vtb_pair(req.inputs.contracts, near, far, quantity=qty)
    if not risk["summary"]["all_scenarios_passed"]:
        raise ScreenerError("пара не проходит стресс-экран рабочего капитала")
    funding_swaprates: dict[str, float] = {}
    funding_source = None
    evidence = None
    if book is not None:
        funding_source = req.funding.source(pair_id, near)
        requested_dates = [row["date"] for row in series["rows"]]
        evidence = FundingWindow(book, requested_dates, req.share).resolve(req.through)
        funding_swaprates = evidence.rates
    report = run_mean_reversion_baseline(
        series,
        lookback=int(req.params["lookback"]),
        entry_z=float(req.params["entry_z"]),
        exit_z=float(req.params["exit_z"]),
        quantity=qty,
        analysis_role="diagnostic_only",
        cost_profile=tariff_profile(float(req.execution["slippage_ticks"])),
        fill="open",
        cash_in_lqdt=True,
        funding_swaprates=funding_swaprates,
    )
    summary = report["summary"]
    provenance = dict(report["input_provenance"])
    if funding_source is not None:
        provenance["near_funding_history_sha256"] = funding_source["source"]["raw_response_sha256"]
    price_role = (
        "ISS: дневной candle, заполнение по открытию, отметки по закрытию"
        if not req.spec.intraday
        else f"ISS: свеча {req.spec.label}, заполнение по открытию, отметки по закрытию"
    )
    return StrategyRunOutcome(
        report=report,
        risk=risk,
        value_key="strategy_value",
        trades=int(summary["trade_count"]),
        open_positions=0,
        costs=summary["modeled_costs"],
        provenance=provenance,
        price_role=price_role,
        notes=mean_reversion_notes(req.spec, funding_source is not None),
        evidence=evidence,
    )


def _run_funding_only_branch(req: _BranchReq) -> StrategyRunOutcome:
    pair_id, near, far = req.pair["pair_id"], req.pair["near"], req.pair["far"]
    window = req.funding.carry_window(req.pair, req.share)
    evidence = window.resolve(req.through)
    near_source = req.funding.source(pair_id, near)
    far_source = req.funding.source(pair_id, far)
    study = build_carry_study(
        near_source,
        far_source,
        req.inputs.contracts,
        near=near,
        far=far,
        funding_regimes=None if pair_id == "CNYRUBF/CRZ6" else (),
        funding_marks=evidence.marks,
        no_clearing_dates=window.book.no_clearing_dates,
        through=req.through,
    )
    qty = int(req.execution["quantity"])
    report = build_carry_strategy(
        study,
        req.inputs.benchmark,
        req.inputs.contracts,
        transaction_cost_scenario="tariff",
        warmup_observations=int(req.params["lookback"]),
        strategy_variant="funding_only",
        quantity=qty,
        execution_delay_rows=0,
        cash_in_lqdt=True,
        slippage_ticks=float(req.execution["slippage_ticks"]),
    )
    risk = assess_vtb_pair(req.inputs.contracts, near, far, quantity=qty)
    if not risk["summary"]["all_scenarios_passed"]:
        raise ScreenerError("пара не проходит стресс-экран рабочего капитала")
    summary = report["summary"]
    provenance = {
        "near_history_hashes": near_source["source"]["raw_response_sha256"],
        "far_history_hashes": far_source["source"]["raw_response_sha256"],
        "benchmark_sha256": hashlib.sha256(req.inputs.benchmark.read_bytes()).hexdigest(),
        "contracts_sha256": hashlib.sha256(req.inputs.contracts.read_bytes()).hexdigest(),
    }
    notes = [
        "Сигнал фондирования использует фондирование до предыдущего клиринга; прокси исполнения — расчёт в тот же день; выход в тот же день теряет фондирование этого дня.",
        *(
            ["Non-CNY funding-rule caps are not reconstructed."]
            if pair_id != "CNYRUBF/CRZ6"
            else []
        ),
    ]
    return StrategyRunOutcome(
        report=report,
        risk=risk,
        value_key="strategy_value_proxy",
        trades=int(summary["closed_trade_count"]),
        open_positions=int(summary["open_trade_count"]),
        costs=summary["transaction_costs"],
        provenance=provenance,
        price_role="ISS: расчётная цена на конец дня (прокси)",
        notes=notes,
        evidence=evidence,
    )


def build_result(
    inputs: ScreenerInputs,
    selection: dict[str, Any],
    *,
    through: str | None = None,
    validation_share: float | None = None,
    fetch_fn: Callable[..., dict[str, Any]] = fetch_history,
) -> dict[str, Any]:
    if not isinstance(selection, dict):
        raise ScreenerError("выбор должен быть объектом")
    pair = _pair(selection.get("pair"))
    strategy = selection.get("strategy")
    if not isinstance(strategy, str) or strategy not in STRATEGIES:
        raise ScreenerError("неизвестная стратегия")
    execution = execution_settings(selection)
    params = _parameters(selection, strategy)
    settings = read_settings(inputs.root)
    timeframe = str(settings["timeframe"])
    spec = get_timeframe(timeframe)
    if strategy == "funding_only" and not spec.supports_funding_strategy:
        raise ScreenerError(f"{spec.label}: carry требует дневных расчётных ставок.")
    funding = _make_proof_funding(inputs, fetch_fn=fetch_fn)
    initial = float(settings["initial_capital_rub"])
    entry = catalog(
        inputs,
        registry=(pair,),
        funding=funding,
        initial_capital=initial,
        timeframe=timeframe,
        fetch_fn=fetch_fn,
    )["pairs"][0]
    if not entry["strategies"][strategy]:
        raise ScreenerError(
            entry["data_reasons"].get(strategy, "Не пройдено правило ликвидности пары.")
        )
    share = (
        (
            float(read_settings(inputs.root)["validation_percent"])
            if validation_share is None
            else float(validation_share)
        )
        if through is None
        else 0.0
    )
    req = _BranchReq(
        inputs=inputs,
        pair=pair,
        spec=spec,
        timeframe=timeframe,
        funding=funding,
        params=params,
        execution=execution,
        through=through,
        share=share,
    )
    outcome = (
        _run_mean_reversion_branch(req)
        if strategy == "mean_reversion"
        else _run_funding_only_branch(req)
    )
    return assemble_screener_result(
        {
            "pair": pair["pair_id"],
            "strategy": strategy,
            "parameters": params,
            "execution": execution,
        },
        initial,
        through,
        share,
        outcome,
    )
