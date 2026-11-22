"""Funding/carry pair proof engine.

Runs the built-in `funding_only` strategy or a trusted carry plugin over a resolved
carry study and emits the `execution-proof.v1` payload with settlement-priced fills.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from moex_spread_lab.accounting.costs import side_cost, tariff_profile
from moex_spread_lab.core.contracts import WindowSnapshot
from moex_spread_lab.core.universe import MIN_DAILY_VOLUME, PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.funding_carry.carry import build_carry_strategy, build_carry_study
from moex_spread_lab.strategies.funding_carry.intents import carry_settlement_intents

from .carry_intent_accounting import replay_carry_intents
from .payload_tools import (
    load_contracts_for_side_cost,
    point_value,
)


@dataclass(frozen=True, slots=True)
class FundingSpec:
    """One funding/carry replay request bound to prepared inputs."""

    inputs: Any
    pair: dict[str, Any]
    execution: dict[str, Any]
    params: dict[str, Any]
    terms: dict[str, dict[str, Any]]
    risk: dict[str, Any]
    prepared: tuple[Any, ...]
    through: str | None = None
    cache: dict[str, Any] | None = None
    benchmark_dates: list[str] | None = None
    benchmark_prices: dict[str, float] | None = None


def funding_inputs(inputs, pair, funding, validation_percent) -> tuple[Any, ...]:
    """Carry study, leg provenance and the funding window for one pair."""
    pair_id, near, far = pair["pair_id"], pair["near"], pair["far"]
    window = funding.carry_window(pair, validation_percent)
    near_source = funding.source(pair_id, near)
    far_source = funding.source(pair_id, far)
    # Resolve the full research period (including its split) before the study reads any estimate.
    study = build_carry_study(
        near_source,
        far_source,
        inputs.contracts,
        near=near,
        far=far,
        funding_regimes=None if pair_id == "CNYRUBF/CRZ6" else (),
        funding_marks=window.resolve().marks,
        no_clearing_dates=window.book.no_clearing_dates,
    )
    return study, near_source["source"], far_source["source"], window


@dataclass(frozen=True, slots=True)
class FundingWindowState:
    """Study cut at `through` plus the reference settlement prices for that cut."""

    spec: FundingSpec
    study: dict[str, Any]
    evidence: Any
    study_by_date: dict[str, dict[str, Any]]
    ref_near: float
    ref_far: float
    window_meta: dict[str, Any]
    funding_window: Any
    near_mult: float
    far_mult: float
    margin_pair: float


def study_cut(spec: FundingSpec) -> FundingWindowState:
    """Cut the study at `through` and derive the quantities every parameter reuses."""
    study, _, _, window = spec.prepared[:4]
    if spec.through is not None:
        study = {**study, "rows": [row for row in study["rows"] if row["date"] <= spec.through]}
    study_rows = study["rows"]
    cache = spec.cache["funding_through"] if spec.cache is not None else None
    if cache is not None and spec.through in cache:
        evidence, by_date, ref_near, ref_far, meta = cache[spec.through]
    else:
        evidence = window.resolve(spec.through)
        by_date = {row["date"]: row for row in study_rows}
        ref_near = float(study_rows[-1]["near_settle"])
        ref_far = float(study_rows[-1]["far_settle"])
        volumes = [
            min(row.get("near_volume"), row.get("far_volume"))
            for row in study_rows
            if row.get("near_volume") is not None
            and row.get("far_volume") is not None
            and float(row.get("near_volume")) > 0
            and float(row.get("far_volume")) > 0
        ]
        smallest = min(volumes) if volumes else None
        meta = {
            "min_leg_volume": smallest,
            "tradable_every_day": bool(smallest is not None and smallest >= MIN_DAILY_VOLUME),
        }
        if cache is not None:
            cache[spec.through] = (evidence, by_date, ref_near, ref_far, meta)
    near, far = spec.pair["near"], spec.pair["far"]
    near_mult = point_value(spec.terms[near], fallback=1.0)
    far_mult = point_value(spec.terms[far], fallback=near_mult)
    return FundingWindowState(
        spec=spec,
        study=study,
        evidence=evidence,
        study_by_date=by_date,
        ref_near=ref_near,
        ref_far=ref_far,
        window_meta=meta,
        funding_window=window,
        near_mult=near_mult,
        far_mult=far_mult,
        margin_pair=(
            float(spec.terms[near].get("initial_margin") or 0.0)
            + float(spec.terms[far].get("initial_margin") or 0.0)
        )
        * int(spec.execution["quantity"]),
    )


def carry_plugin_of(spec: FundingSpec) -> Any:
    return spec.cache.get("carry_plugin") if spec.cache is not None else None


def strategy_name_of(spec: FundingSpec) -> str:
    plugin = carry_plugin_of(spec)
    return plugin.strategy_id if plugin is not None else "funding_only"


def _reference_fee(state: FundingWindowState) -> float:
    """Round-trip taker cost of one unit of the pair at the reference settlement."""
    spec = state.spec
    near, far = spec.pair["near"], spec.pair["far"]
    leg_contracts = load_contracts_for_side_cost(spec.inputs.contracts, near, far)
    costs = side_cost(
        tariff_profile(float(spec.execution["slippage_ticks"])),
        [(leg_contracts[near], state.ref_near), (leg_contracts[far], state.ref_far)],
        1,
    )
    return round(sum(costs.values()), 8)


def _pluginreport_for(
    state: FundingWindowState, plugin: Any, *, metrics_only: bool
) -> dict[str, Any]:
    """Replay trusted carry-plugin intents through the settlement accounting."""
    spec, execution = state.spec, state.spec.execution
    pair_id, near, far = spec.pair["pair_id"], spec.pair["near"], spec.pair["far"]
    snapshot = WindowSnapshot(
        universe=PAIR_UNIVERSE,
        pair=pair_id,
        near=near,
        far=far,
        timeframe="1d",
        carry_study=state.study,
        contracts=spec.terms,
        benchmark=spec.inputs.benchmark,
        initial_capital_rub=execution["initial_capital_rub"],
        slippage_ticks=float(execution["slippage_ticks"]),
        extra={
            "contracts_path": spec.inputs.contracts,
            "transaction_cost_scenario": "tariff",
        },
    )
    verdict = plugin.check_compatibility(deepcopy(snapshot))
    if not verdict.compatible:
        raise W0Error(verdict.reason or "Несовместимое окно carry.")
    intents = carry_settlement_intents(plugin, snapshot, spec.params)
    leg_contracts = load_contracts_for_side_cost(spec.inputs.contracts, near, far)
    full = (
        {}
        if metrics_only
        else {
            "initial_capital": execution["initial_capital_rub"],
            "margin_per_pair": float(spec.terms[near].get("initial_margin") or 0)
            + float(spec.terms[far].get("initial_margin") or 0),
            "benchmark": spec.benchmark_prices or {},
        }
    )
    report = replay_carry_intents(
        state.study,
        intents,
        quantity=int(execution["quantity"]),
        side_cost=_reference_fee(state),
        far_expiry=leg_contracts[far].get("expiry"),
        **full,
    )
    if metrics_only:
        for row in report["daily"]:
            row.pop("near_volume", None)
            row.pop("far_volume", None)
    return report


def _builtinreport_for(state: FundingWindowState, *, metrics_only: bool) -> dict[str, Any]:
    spec, execution = state.spec, state.spec.execution
    return build_carry_strategy(
        state.study,
        spec.inputs.benchmark,
        spec.inputs.contracts,
        transaction_cost_scenario="tariff",
        warmup_observations=int(spec.params["lookback"]),
        strategy_variant="funding_only",
        initial_capital=execution["initial_capital_rub"],
        quantity=int(execution["quantity"]),
        execution_delay_rows=0,
        cash_in_lqdt=True,
        slippage_ticks=float(execution["slippage_ticks"]),
        metrics_only=metrics_only,
    )


def report_for(state: FundingWindowState, *, metrics_only: bool) -> dict[str, Any]:
    plugin = carry_plugin_of(state.spec)
    if plugin is None:
        return _builtinreport_for(state, metrics_only=metrics_only)
    return _pluginreport_for(state, plugin, metrics_only=metrics_only)
