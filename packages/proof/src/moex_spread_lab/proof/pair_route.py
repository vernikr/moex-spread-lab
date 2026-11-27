"""Request-scoped pair replay: one canonical intent-to-accounting pipeline.

`prepare_pair` owns the native preflight (screen, capital stress, inputs) for the built-in
pair strategies and trusted carry plugins. A price strategy is replayed by its registered
`StrategyPlugin` through `pair_price_intents.v1`, priced and accounted independently of the
plugin, and rendered with the built-in provenance (`trust_tier="builtin"`).
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from typing import Any, cast

from moex_spread_lab.accounting.account_risk import assess_vtb_pair
from moex_spread_lab.accounting.position_sizing import AUTO_SIZING, automatic
from moex_spread_lab.core.contracts import (
    StrategyIntentRun,
    WindowSnapshot,
    plugin_is_builtin,
)
from moex_spread_lab.core.settings import STRICT_SCREENS, ResearchSettings
from moex_spread_lab.core.timeframes import get_timeframe
from moex_spread_lab.core.universe import PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.strategies import PRICE_FILLS, pair_fill
from moex_spread_lab.strategies.funding_carry.proof_funding import ProofFunding
from moex_spread_lab.strategies.pair_mean_reversion.engine import AnalyticsError
from moex_spread_lab.strategies.pair_screener import (
    STRATEGIES,
    _pair,
    _parameters,
    execution_settings,
)
from moex_spread_lab.strategies.pair_screener import (
    catalog as _pair_catalog,
)

from .carry_payload import run_carry
from .fixed_plugin_payload import fixed_pair_accounting
from .ledger_blocks import chart_rows, metrics_payload, sizing_unit
from .market_window import ReplaySpec, cached, chart_signal_z, resolve_window
from .pair_payload import full_payload
from .pair_plugin_accounting import prepare_daily_pair_unit as _prepare_unit
from .payload_tools import benchmark_marks, load_instruments, sha256_bytes


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


def _plugin_parameters(plugin: Any, request: Any, pair: Any, settings: Any) -> dict[str, Any]:
    """Validate trusted-plugin parameters against the schema it declares."""
    from copy import deepcopy

    from moex_spread_lab.core.contracts import InstrumentContext
    from moex_spread_lab.strategies.base import validate_parameter_map

    context = InstrumentContext(
        universe=PAIR_UNIVERSE, pair=pair["pair_id"], timeframe=settings.timeframe
    )
    schema = plugin.parameter_schema(context)
    clean = validate_parameter_map(request.get("parameters", {}), schema)
    params = validate_parameter_map(plugin.validate_parameters(deepcopy(clean), context), schema)
    if params != clean or any(not param.contains(params[param.name]) for param in schema):
        raise W0Error("Параметры carry-плагина не соответствуют объявленной схеме.")
    return params


def _validate_pair_screen(
    inputs: Any, pair: Any, funding: Any, screen_strategy: Any, settings: ResearchSettings
) -> None:
    tf = get_timeframe(settings.timeframe)
    if not tf.supports_pair_proof:
        raise W0Error(f"{tf.label}: pair-proof не поддерживается.")
    catalog = _pair_catalog(
        inputs,
        registry=(pair,),
        funding=funding,
        initial_capital=settings.initial_capital_rub,
        timeframe=settings.timeframe,
    )
    entry = cast(dict[str, Any], catalog["pairs"][0])
    if not cast(dict[str, Any], entry["proof_strategies"]).get(screen_strategy):
        raise W0Error(cast(str, cast(dict[str, Any], entry["data_reasons"])[screen_strategy]))
    if not (settings.validation_percent or settings.allow_failed_volume) and not cast(
        dict[str, Any], entry["strategies"]
    ).get(screen_strategy):
        raise W0Error("Не пройдено правило ликвидности пары.")


def _validate_pair_request(
    request: Any, strategy: Any, pair: Any, carry_plugin: Any, sizing_intents: Any
) -> None:
    req = cast(dict[str, Any], request)
    if (
        req.get("strategy") != strategy
        or req.get("pair") != cast(dict[str, Any], pair)["pair_id"]
        or req.get("universe", PAIR_UNIVERSE) != PAIR_UNIVERSE
        or (carry_plugin is not None and automatic(request) != sizing_intents)
    ):
        raise W0Error("Запрос не соответствует подготовленному окну стратегии.")


def _validate_carry_result(result: Any, carry_plugin: Any, metrics_only: bool) -> None:
    # Built-in engines keep their native payload shape; drop-ins carry the marker even
    # when they subclass a built-in plugin and inherit its trust tier.
    if carry_plugin is None or plugin_is_builtin(carry_plugin):
        return
    res = cast(dict[str, Any], result)
    res["accounting_policy"] = "carry_settlement.v1"
    if (
        not metrics_only
        and abs(cast(float, cast(dict[str, Any], res["summary"])["ledger_reconciliation_rub"]))
        > 0.02
    ):
        raise W0Error("Учёт carry-плагина не согласован с журналом сделок.")


def _replay_once(spec: ReplaySpec, price_strategy: bool, metrics_only: bool) -> dict[str, Any]:
    """Dispatch a prepared spec to its canonical replay pipeline."""
    if price_strategy:
        return builtin_pair_proof(spec, metrics_only=metrics_only)
    return run_carry(spec, metrics_only=metrics_only)


def prepare_pair(
    service: Any,
    selection: dict[str, Any],
    settings: ResearchSettings,
    sizing_intents: Any,
    carry_plugin: Any,
) -> Callable[..., dict[str, Any]]:
    """Prepare a request-scoped pair replay over directly imported canonical hooks."""
    pair = _pair(selection.get("pair"))
    near, far = pair["near"], pair["far"]
    strategy = selection.get("strategy")
    if not isinstance(strategy, str) or (strategy not in STRATEGIES and carry_plugin is None):
        raise W0Error("неизвестная стратегия")
    screen_strategy = "funding_only" if carry_plugin is not None else strategy
    inputs = service.screener_inputs
    funding = ProofFunding(inputs)
    _validate_pair_screen(inputs, pair, funding, screen_strategy, settings)

    @cache
    def risk_for(quantity: Any) -> Any:
        risk = assess_vtb_pair(
            inputs.contracts,
            near,
            far,
            quantity=quantity,
            initial_capital=settings.initial_capital_rub,
        )
        if not settings.allow_failed_capital and not risk["summary"]["all_scenarios_passed"]:
            raise W0Error("пара не проходит стресс-экран рабочего капитала")
        return risk

    risk_for(execution_settings(selection)["quantity"])
    instruments = load_instruments(inputs.contracts)
    terms = {secid: instruments.get(secid, {}) for secid in (near, far)}
    dates, prices = benchmark_marks(inputs.benchmark)
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
        "benchmark_sha256": sha256_bytes(inputs.benchmark),
        "contracts_sha256": sha256_bytes(inputs.contracts),
        "historical_contracts_sha256": sha256_bytes(
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
            **execution_settings(request),
            "sizing_intents": sizing_intents,
            "fill": pair_fill(request, strategy, default_price_fill=settings.price_fill),
            "initial_capital_rub": settings.initial_capital_rub,
        }
        _validate_pair_request(request, strategy, pair, carry_plugin, sizing_intents)
        params = (
            _parameters(request, strategy)
            if carry_plugin is None
            else _plugin_parameters(carry_plugin, request, pair, settings)
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
        result = _replay_once(spec, price_strategy, metrics_only)
        _validate_carry_result(result, carry_plugin, metrics_only)
        return result

    return replay
