"""Request-scoped pair replay: one canonical intent-to-accounting pipeline.

`prepare_pair` owns the native preflight (screen, capital stress, inputs) for the built-in
pair strategies and trusted carry plugins. A price strategy is replayed by its registered
`StrategyPlugin` through `pair_price_intents.v1`, priced and accounted independently of the
plugin, and rendered with the built-in provenance (`trust_tier="builtin"`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING, automatic
from moex_spread_lab.core.contracts import StrategyIntentRun, WindowSnapshot
from moex_spread_lab.core.settings import STRICT_SCREENS, ResearchSettings
from moex_spread_lab.core.universe import PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies import PRICE_FILLS
from moex_spread_lab.strategies.pair_mean_reversion.analytics import AnalyticsError

from .fixed_plugin_payload import fixed_pair_accounting
from .market_window import ReplaySpec, cached, chart_signal_z, resolve_window
from .pair_plugin_accounting import prepare_daily_pair_unit as _prepare_unit
from .pair_template import chart_rows, full_payload, metrics_payload, sizing_unit


def check_sizing_request(selection: dict[str, Any]) -> None:
    """Reject sizing modes a request-scoped replay cannot honour."""
    if selection.get("sizing") not in (None, AUTO_SIZING):
        raise W0Error("Неизвестный режим расчёта размера позиции.")
    if automatic(selection) and selection.get("quantity") is not None:
        raise W0Error("При автоматическом размере количество не задаётся вручную.")


def prepare_native(
    service: Any,
    selection: dict[str, Any],
    *,
    calendar: Any = None,
    settings: ResearchSettings = STRICT_SCREENS,
) -> Callable[..., dict[str, Any]]:
    """Prepare a built-in replay without the plugin-route request translation."""
    check_sizing_request(selection)
    if not automatic(selection):
        return service._prepare_fixed(selection, calendar=calendar, settings=settings)
    from .strategy_catalog import get_strategy_registry

    registry = cast(Any, get_strategy_registry())
    return service._prepare_automatic(
        selection, calendar, settings, registry, selection.get("strategy")
    )


def _native_gates(spec: ReplaySpec, rows: int, margin_pair: float) -> None:
    """Built-in screening order: too short a window, then the fixed-quantity capital gate."""
    if rows < 2:
        raise AnalyticsError("fewer than two pair timestamps with benchmark marks")
    capital = float(spec.execution["initial_capital_rub"])
    if not spec.execution.get("sizing_intents") and margin_pair > capital:
        raise AnalyticsError(
            f"ГО для выбранного размера ({margin_pair:.2f} ₽) превышает "
            f"стартовый капитал ({capital:.2f} ₽). Допуск по стресс-тесту не пополняет счёт."
        )


def _snapshot(spec: ReplaySpec) -> tuple[WindowSnapshot, dict[str, float]]:
    """Uncut plugin window and benchmark marks; the accounting applies `through` itself."""
    series = spec.prepared[0]
    snapshot = WindowSnapshot(
        universe=PAIR_UNIVERSE,
        pair=spec.pair["pair_id"],
        timeframe=series["timeframe"],
        near=spec.pair["near"],
        far=spec.pair["far"],
        rows=series["rows"],
        contracts=series["contracts"],
    )
    return snapshot, {row["date"]: row["benchmark_price"] for row in series["rows"]}


class _NoIntents:
    """Position-neutral intent source: renders the shared chart/market payload template."""

    strategy_id = "mean_reversion"
    supported_fills = tuple(PRICE_FILLS)

    def validate_parameters(self, params: dict[str, Any], _context: Any) -> dict[str, Any]:
        return params

    def evaluate_intents(self, _window: Any, params: dict[str, Any], fill: str) -> Any:
        return StrategyIntentRun(self.strategy_id, fill, params, ())


_TEMPLATE_SOURCE = _NoIntents()


def pair_template_payload(spec: ReplaySpec) -> dict[str, Any]:
    """Market-window payload with no decisions, for adapters that account their own intents."""
    return builtin_pair_proof(spec, template=True)


def builtin_pair_proof(
    spec: ReplaySpec, *, metrics_only: bool = False, template: bool = False
) -> dict[str, Any]:
    """Replay a built-in pair price strategy through its plugin intents and shared accounting."""
    cache = cast(dict[str, Any], spec.cache)
    window = resolve_window(spec)
    _native_gates(spec, len(window.analysis_rows), window.margin_pair)
    execution, provenance = spec.execution, spec.prepared[3]
    snapshot, marks = cached(cache, "snapshot", lambda: _snapshot(spec))
    unit, _ = _prepare_unit(
        cast(Any, _TEMPLATE_SOURCE if template else cache["plugin"]),
        snapshot,
        dict(spec.params),
        execution["fill"],
        window.funding_window,
        marks,
        cache["settings"],
        through=spec.through,
        enforce_compatibility=False,
        isolate=False,
        memo=cache.setdefault("unit_rows", {}),
    )
    if metrics_only and execution.get("sizing_intents"):
        return sizing_unit(window, unit, provenance)
    neutral = cached(cache["series"], spec.through, lambda: chart_rows(window, {}))
    if metrics_only or template:
        template_rows = neutral
    else:
        z = chart_signal_z(window, int(spec.params["lookback"]), PRICE_FILLS[execution["fill"]][0])
        template_rows = [{**row, "signal_z": z.get(row["date"])} for row in neutral]
    rows, trades, ledger, drawdown, raw_totals = fixed_pair_accounting(
        template_rows,
        unit,
        (window.series, spec.pair, provenance),
        execution,
        (spec.benchmark_dates or [], spec.benchmark_prices or {}),
        execution["initial_capital_rub"],
        builtin=True,
    )
    if metrics_only:
        return metrics_payload(window, trades, ledger, provenance)
    return full_payload(
        window,
        (rows, trades, ledger, drawdown),
        raw_totals,
        {
            **window.series["provenance"],
            **(spec.prepared[4] or {}),
            **provenance,
            **(window.evidence.provenance if window.evidence else {}),
            **unit["provenance"],
        },
    )


def _replay_once(
    facade: Any, spec: ReplaySpec, price_strategy: bool, metrics_only: bool
) -> dict[str, Any]:
    """Dispatch through the facade hooks so tests can patch either replay."""
    if price_strategy:
        return facade.run_mean_reversion(spec, metrics_only=metrics_only)
    return facade.run_funding(spec, metrics_only=metrics_only)


def prepare_pair(
    service: Any,
    selection: dict[str, Any],
    settings: ResearchSettings,
    sizing_intents: Any,
    carry_plugin: Any,
    facade: Any,
) -> Callable[..., dict[str, Any]]:
    """Prepare a request-scoped pair replay using the facade's injectable hooks."""
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
    price_strategy = strategy == "mean_reversion"
    prepared = (
        service._price_inputs(
            inputs, pair, funding, settings.validation_percent, settings.timeframe
        )
        if price_strategy
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
        "z": {},
        "series": {},
        "funding_through": {},
        "carry_plugin": carry_plugin,
        "settings": settings,
    }
    if price_strategy:
        from .strategy_catalog import get_strategy_registry

        pair_cache["plugin"] = cast(Any, get_strategy_registry()).get(strategy)

    def replay(
        request: Any,
        through: Any = None,
        *,
        metrics_only: bool = False,
        scalar_summary: Any = None,
        template: bool = False,
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
        spec = ReplaySpec(
            inputs=inputs,
            pair=pair,
            execution=execution,
            params=params,
            terms=terms,
            risk=risk_for(execution["quantity"]),
            prepared=prepared,
            through=through,
            cache=pair_cache,
            benchmark_dates=dates,
            benchmark_prices=prices,
        )
        if template:
            return pair_template_payload(spec)
        result = _replay_once(facade, spec, price_strategy, metrics_only)
        facade._validate_carry_result(result, carry_plugin, metrics_only)
        return result

    return replay
