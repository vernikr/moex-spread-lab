"""Prepare a request-scoped pair replay using the facade's injectable hooks."""

from collections.abc import Callable
from typing import Any

from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.core.w0 import W0Error


def prepare_pair(
    service: Any,
    selection: dict[str, Any],
    settings: ResearchSettings,
    sizing_intents: Any,
    carry_plugin: Any,
    facade: Any,
) -> Callable[..., dict[str, Any]]:
    pair = facade._pair(selection.get("pair"))
    near, far = pair["near"], pair["far"]
    strategy = selection.get("strategy")
    if not isinstance(strategy, str) or (
        strategy not in facade.STRATEGIES and carry_plugin is None
    ):
        raise W0Error("неизвестная стратегия")
    screen_strategy = "funding_only" if carry_plugin is not None else strategy
    inputs = service.screener_inputs
    funding = facade.ProofFunding(inputs)
    facade._validate_pair_screen(inputs, pair, funding, screen_strategy, settings)

    @facade.cache
    def risk_for(quantity: Any) -> Any:
        risk = facade.assess_vtb_pair(
            inputs.contracts,
            near,
            far,
            quantity=quantity,
            initial_capital=settings.initial_capital_rub,
        )
        if not settings.allow_failed_capital and not risk["summary"]["all_scenarios_passed"]:
            raise W0Error("пара не проходит стресс-экран рабочего капитала")
        return risk

    risk_for(facade.execution_settings(selection)["quantity"])
    instruments = facade._load_instruments(inputs.contracts)
    terms = {secid: instruments.get(secid, {}) for secid in (near, far)}
    dates, prices = facade._benchmark_marks(inputs.benchmark)
    mean_reversion = strategy == "mean_reversion"
    prepared = (
        service._price_inputs(
            inputs, pair, funding, settings.validation_percent, settings.timeframe
        )
        if mean_reversion
        else service._funding_inputs(inputs, pair, funding, settings.validation_percent)
    )

    # Reuse immutable input hashes throughout this request's Optuna trials.
    source_hashes = {
        "benchmark_sha256": facade._sha256_bytes(inputs.benchmark),
        "contracts_sha256": facade._sha256_bytes(inputs.contracts),
        "historical_contracts_sha256": facade._sha256_bytes(
            inputs.contracts.parent / "historical-contracts.json"
        ),
    }
    prepared = (*prepared, source_hashes)
    pair_cache: dict[str, Any] = {
        "through": {},
        "signal_dates": {},
        "z": {},
        "series": {},
        "window_meta": {},
        "funding_through": {},
        "carry_plugin": carry_plugin,
    }

    def replay(
        request: Any, through: Any = None, *, metrics_only: bool = False, scalar_summary: Any = None
    ) -> dict[str, Any]:
        execution = {
            **facade.execution_settings(request),
            "sizing_intents": sizing_intents,
            "fill": facade.pair_fill(request, strategy, default_price_fill=settings.price_fill),
            "initial_capital_rub": settings.initial_capital_rub,
        }
        facade._validate_pair_request(request, strategy, pair, carry_plugin, sizing_intents)
        params = (
            facade._parameters(request, strategy)
            if carry_plugin is None
            else facade._plugin_parameters(carry_plugin, request, pair, settings)
        )
        common = {
            "inputs": inputs,
            "pair": pair,
            "execution": execution,
            "params": params,
            "terms": terms,
            "risk": risk_for(execution["quantity"]),
            "prepared": prepared,
            "through": through,
            "cache": pair_cache,
            "benchmark_dates": dates,
            "benchmark_prices": prices,
        }
        result = (
            facade.run_mean_reversion(facade.MeanReversionSpec(**common), metrics_only=metrics_only)
            if mean_reversion
            else facade.run_funding(facade.FundingSpec(**common), metrics_only=metrics_only)
        )
        facade._validate_carry_result(result, carry_plugin, metrics_only)
        return result

    return replay
