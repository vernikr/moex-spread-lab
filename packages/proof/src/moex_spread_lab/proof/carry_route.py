"""Carry replay: one trusted carry study cut, settlement intents and their accounting.

Runs the built-in `funding_only` plugin or a trusted drop-in carry plugin through
`carry_settlement_intents.v1`; prices and cash flows come only from the carry study.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from moex_spread_lab.accounting.costs import side_cost, tariff_profile
from moex_spread_lab.core.contracts import WindowSnapshot, plugin_is_builtin
from moex_spread_lab.core.settings import MIN_DAILY_VOLUME
from moex_spread_lab.core.universe import PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies.funding_carry.engine import build_carry_strategy
from moex_spread_lab.strategies.funding_carry.intents import carry_settlement_intents
from moex_spread_lab.strategies.funding_carry.proof_funding import ProofFunding
from moex_spread_lab.strategies.funding_carry.study import build_carry_study
from moex_spread_lab.strategies.pair_screener import ScreenerInputs

from .carry_intent_accounting import AccountTerms, replay_carry_intents
from .ledger_blocks import pair_selection
from .market_window import ReplaySpec
from .payload_tools import PROOF_SCHEMA, load_contracts_for_side_cost, point_value

PERPETUAL_OPEN_STATUS = "open_at_data_end_no_executable_exit"


def funding_inputs(
    inputs: ScreenerInputs,
    pair: Mapping[str, str],
    funding: ProofFunding,
    validation_percent: float,
) -> tuple[Any, ...]:
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
class CarryWindowState:
    """Study cut at `through` plus the reference settlement prices for that cut."""

    spec: ReplaySpec
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


def _study_min_volume(rows: list[dict[str, Any]]) -> Any:
    volumes: list[Any] = []
    for row in rows:
        near, far = row.get("near_volume"), row.get("far_volume")
        if near is not None and far is not None and float(near) > 0 and float(far) > 0:
            volumes.append(min(near, far))
    return min(volumes) if volumes else None


def study_cut(spec: ReplaySpec) -> CarryWindowState:
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
        smallest = _study_min_volume(study_rows)
        meta = {
            "min_leg_volume": smallest,
            "tradable_every_day": bool(smallest is not None and smallest >= MIN_DAILY_VOLUME),
        }
        if cache is not None:
            cache[spec.through] = (evidence, by_date, ref_near, ref_far, meta)
    near, far = spec.pair["near"], spec.pair["far"]
    near_mult = point_value(spec.terms[near], fallback=1.0)
    far_mult = point_value(spec.terms[far], fallback=near_mult)
    return CarryWindowState(
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


def carry_plugin_of(spec: ReplaySpec) -> Any:
    return spec.cache.get("carry_plugin") if spec.cache is not None else None


def strategy_name_of(spec: ReplaySpec) -> str:
    plugin = carry_plugin_of(spec)
    return plugin.strategy_id if plugin is not None else "funding_only"


def reference_costs(state: CarryWindowState) -> dict[str, float]:
    """One-unit taker cost at the reference settlement, by cost component."""
    spec = state.spec
    near, far = spec.pair["near"], spec.pair["far"]
    leg_contracts = load_contracts_for_side_cost(spec.inputs.contracts, near, far)
    return side_cost(
        tariff_profile(float(spec.execution["slippage_ticks"])),
        [(leg_contracts[near], state.ref_near), (leg_contracts[far], state.ref_far)],
        1,
        as_of=str(state.study["rows"][-1]["date"]),
    )


def _pluginreport_for(
    state: CarryWindowState, plugin: Any, *, metrics_only: bool
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
    account: AccountTerms | None = (
        None
        if metrics_only
        else (
            execution["initial_capital_rub"],
            float(spec.terms[near].get("initial_margin") or 0)
            + float(spec.terms[far].get("initial_margin") or 0),
            spec.benchmark_prices or {},
        )
    )
    report = replay_carry_intents(
        state.study,
        intents,
        quantity=int(execution["quantity"]),
        side_cost=round(sum(reference_costs(state).values()), 8),
        far_expiry=leg_contracts[far].get("expiry"),
        account=account,
    )
    if metrics_only:
        for row in report["daily"]:
            row.pop("near_volume", None)
            row.pop("far_volume", None)
    return report


def _builtinreport_for(state: CarryWindowState, *, metrics_only: bool) -> dict[str, Any]:
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


def report_for(state: CarryWindowState, *, metrics_only: bool) -> dict[str, Any]:
    plugin = carry_plugin_of(state.spec)
    if plugin is None:
        return _builtinreport_for(state, metrics_only=metrics_only)
    try:
        return _pluginreport_for(state, plugin, metrics_only=metrics_only)
    except W0Error:
        # The adapter rejects windows its invariants cannot certify (for example missing
        # leg volumes). The built-in keeps its historical screening behaviour there, so a
        # degenerate window is reported exactly as before the cutover.
        if plugin_is_builtin(plugin):
            return _builtinreport_for(state, metrics_only=metrics_only)
        raise


def sizing_intent_payload(
    state: CarryWindowState, report: dict[str, Any], strategy_name: str
) -> dict[str, Any]:
    """Price-only payload for the sizing pass: settlement spreads and funding only."""
    spec = state.spec
    quantity = int(spec.execution["quantity"])
    daily_by_date = {row["date"]: row for row in report["daily"]}
    last_date = report["daily"][-1]["date"]
    trades: list[dict[str, Any]] = []
    for row in report["trades"]:
        entry_date = row["entry_execution_date"]
        exit_date = row["exit_execution_date"] or last_date
        trades.append(
            {
                "side": "short_spread",
                "signal": {
                    "kind": "funding",
                    "value": None,
                    "date": row["entry_decision_date"],
                },
                "entry": settlement_intent_fill(state, daily_by_date[entry_date]),
                "exit": settlement_intent_fill(state, daily_by_date[exit_date]),
                "open_at_end": row["status"] == PERPETUAL_OPEN_STATUS,
            }
        )
    series_rows = [
        {
            "date": row["date"],
            "spread": round(
                row["near_settle"] * state.near_mult - row["far_settle"] * state.far_mult, 8
            ),
            "funding_rub": round(row["daily_funding_included_rub"], 2),
            "funding_contracts": quantity if row["position"] else 0,
        }
        for row in report["daily"]
    ]
    return {
        **funding_head(state, strategy_name),
        "contract": {},
        "series": series_rows,
        "trades": trades,
        "provenance": state.evidence.provenance,
    }


def funding_head(state: CarryWindowState, strategy_name: str) -> dict[str, Any]:
    spec = state.spec
    return {
        "schema_version": PROOF_SCHEMA,
        "universe": PAIR_UNIVERSE,
        "selection": funding_selection(state, strategy_name),
        "initial_capital_rub": spec.execution["initial_capital_rub"],
        "instrument": funding_instrument(state),
        "window": {
            **state.window_meta,
            "passes_capital_screen": spec.risk["summary"]["all_scenarios_passed"],
        },
    }


def funding_selection(state: CarryWindowState, strategy_name: str) -> dict[str, Any]:
    spec = state.spec
    return pair_selection(
        spec.pair["pair_id"],
        strategy_name,
        "1d",
        {**spec.execution, "fill": "same_day_settlement"},
        spec.params,
    )


def funding_instrument(state: CarryWindowState) -> dict[str, Any]:
    return {
        "near": state.spec.pair["near"],
        "far": state.spec.pair["far"],
        "family": state.spec.pair["family"],
    }


def settlement_intent_fill(state: CarryWindowState, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": row["date"],
        "spread_fill": round(
            row["near_settle"] * state.near_mult - row["far_settle"] * state.far_mult, 8
        ),
        "near_price": row["near_settle"],
        "far_price": row["far_settle"],
    }
