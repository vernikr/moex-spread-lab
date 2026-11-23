"""Service facade for `/api/proof/*`.

`ProofService` stays the single entry point used by the web routes and the search
kernels. It keeps the cached study inputs, dispatches on `universe`/`sizing`, and
delegates every calculation to the focused modules:

- `calendar_adapter` — native calendar payload -> `execution-proof.v1`
- `mean_reversion_engine` / `funding_engine` — pair strategies
- `pair_plugins` — plugin-driven pair route with its own accounting
- `pair_payload`, `payload_tools`, `ledger_blocks` — shared payload building blocks

Every number produced here must reconcile: trades, the daily ledger and the summary
are built from the same marks and costs. Prices are ISS settlement/candle proxies, not
executable fills. The screen is offline-only; it never downloads data.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

from moex_spread_lab.accounting.account_risk import assess_vtb_pair
from moex_spread_lab.accounting.position_sizing import AUTO_SIZING, automatic, size_proof
from moex_spread_lab.core.evaluation_periods import STRICT_SCREENS, ResearchSettings
from moex_spread_lab.core.timeframes import get_timeframe
from moex_spread_lab.core.universe import (
    CALENDAR_UNIVERSE,
    PAIR_UNIVERSE,
    STRATEGIES,
)
from moex_spread_lab.core.w0 import W0Error, _HashedJSONCache, _run_id
from moex_spread_lab.strategies import pair_fill
from moex_spread_lab.strategies.funding_carry.proof_funding import FundingWindow, ProofFunding
from moex_spread_lab.strategies.pair_screener.pair_screener import (
    ScreenerInputs,
    _pair,
    _parameters,
    execution_settings,
    pair_price_inputs,
)
from moex_spread_lab.strategies.pair_screener.pair_screener import catalog as _pair_catalog

from .calendar_adapter import calendar_proof
from .funding_engine import FundingSpec, funding_inputs
from .funding_payloads import run_funding
from .mean_reversion_payloads import run_mean_reversion
from .mean_reversion_spec import MeanReversionSpec
from .pair_plugins import PairPluginRoute
from .payload_tools import (
    benchmark_marks,
    clean,
    events,
    load_contracts_for_side_cost,
    load_instruments,
    mark,
    positive_pair_volume,
    result_label,
    sha256_bytes,
)

PROOF_SCHEMA = "execution-proof.v1"
FORCED_EXIT_REASONS = frozenset(
    {"end_of_sample", "end_of_window", "open_at_data_end_no_executable_exit"}
)
_BENCHMARK_MARKS_CACHE = _HashedJSONCache(32)
_INSTRUMENTS_CACHE = _HashedJSONCache(32)
_SHA256_CACHE = _HashedJSONCache(512)

# Backwards-compatible private aliases used by the proof tests and older callers.
_benchmark_marks = benchmark_marks
_load_instruments = load_instruments
_sha256_bytes = sha256_bytes
_mark = mark
_load_contracts_for_side_cost = load_contracts_for_side_cost
_result = result_label
_clean = clean
_events = events


def reference_eval() -> bool:
    """Full reference path (series, summary, run_id) vs the metrics-only fast path.

    Defaults to the metrics-only training fast path ("0"); set MOEX_PROOF_REFERENCE_EVAL=1
    to force the full reference path during training probes.
    """
    import os

    return os.environ.get("MOEX_PROOF_REFERENCE_EVAL", "0") == "1"


def _plugin_parameters(plugin, request, pair, settings) -> dict[str, Any]:
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


class ProofService:
    """Single entry point for /api/proof/*; dispatches on the `universe` field."""

    def __init__(self, root: Path, calendar=None, *, pool_factory=None):
        self.root = Path(root).resolve()
        self._calendar = calendar
        self._inputs = None
        self._pool = None
        self._pool_factory = pool_factory

    @property
    def calendar_data(self):
        if self._calendar is None:
            from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData

            self._calendar = CalendarData(self.root)
        return self._calendar

    @property
    def screener_inputs(self):
        if self._inputs is None:
            self._inputs = ScreenerInputs(self.root)
        return self._inputs

    def close(self) -> None:
        """Shut down the cohort worker pool if this service created one."""
        if self._pool is not None:
            self._pool.shutdown(wait_for=True)
            self._pool = None

    def worker_pool(self):
        if self._pool is None:
            if self._pool_factory is None:
                raise W0Error(
                    "CohortWorkerPool factory is not configured; call proof_pool.get_worker_pool(service)."
                )
            self._pool = self._pool_factory(self)
        return self._pool

    def catalog(self, override: ResearchSettings | None = None) -> dict[str, Any]:
        from .catalog_payload import catalog_payload

        settings = override if override is not None else ResearchSettings.load(self.root)
        return catalog_payload(self, settings)

    def proof(self, selection: dict[str, Any]) -> dict[str, Any]:
        saved = ResearchSettings.load(self.root)
        strict = ResearchSettings(
            validation_percent=saved.validation_percent,
            allow_failed_volume=False,
            allow_failed_capital=False,
            slippage_ticks=saved.slippage_ticks,
            initial_capital_rub=saved.initial_capital_rub,
            timeframe=saved.timeframe,
            price_fill=saved.price_fill,
        )
        return self.prepare(selection, calendar=self.calendar_data, settings=strict)(selection)

    def prepare(
        self,
        selection: dict[str, Any],
        *,
        calendar=None,
        settings: ResearchSettings = STRICT_SCREENS,
    ) -> Callable[..., dict[str, Any]]:
        if not isinstance(selection, dict):
            raise W0Error("выбор должен быть объектом")
        from moex_spread_lab.strategies.base import daily_pair_proof_plugin_ids

        from .strategy_catalog import get_strategy_registry

        registry = get_strategy_registry()
        strategy = selection.get("strategy")
        if isinstance(strategy, str) and strategy in daily_pair_proof_plugin_ids(registry):
            if selection.get("sizing") not in (None, AUTO_SIZING):
                raise W0Error("Неизвестный режим расчёта размера позиции.")
            if automatic(selection) and selection.get("quantity") is not None:
                raise W0Error("При автоматическом размере количество не задаётся вручную.")
            return self._prepare_daily_pair_plugin(selection, settings, registry.get(strategy))
        if selection.get("sizing") not in (None, AUTO_SIZING):
            raise W0Error("Неизвестный режим расчёта размера позиции.")
        if not automatic(selection):
            return self._prepare_fixed(selection, calendar=calendar, settings=settings)

        def intent_request(request):
            strategy = request.get("strategy", "zscore_mean_reversion")
            selected_plugin = (
                registry.get(strategy)
                if isinstance(strategy, str) and strategy in registry
                else None
            )
            default_fill = (
                "same_day_settlement"
                if selected_plugin is not None
                and selected_plugin.supported_fills == ("same_day_settlement",)
                else settings.price_fill
            )
            return {
                **request,
                "quantity": 1,
                "slippage_ticks": settings.slippage_ticks,
                **({} if "fill" in request else {"fill": default_fill}),
            }

        replay = self._prepare_fixed(
            intent_request(selection), calendar=calendar, settings=settings, sizing_intents=True
        )
        dates, prices = _benchmark_marks(self.root / "data/benchmark/lqdt-lineage.json")
        contracts = _load_instruments(self.root / "data/w1/current-intake.json")
        marks_cache: dict[str | None, dict[str, float]] = {}
        scan_cols_cache: dict[str | None, Any] = {}

        def sized_replay(
            request,
            through=None,
            *,
            metrics_only: bool = False,
            scalar_summary: tuple[str, str] | None = None,
        ):
            from moex_spread_lab.strategies.base import carry_proof_plugin_ids

            if strategy in carry_proof_plugin_ids(registry) and (
                not isinstance(request, dict)
                or not automatic(request)
                or request.get("quantity") is not None
            ):
                raise W0Error(
                    "Запрос не соответствует подготовленному автоматическому размеру carry."
                )
            unit = replay(intent_request(request), through=through, metrics_only=metrics_only)
            if unit.get("schema_version") == "calendar-proof.v1":
                if through is None:
                    unit = calendar_proof(self.root, request, unit)
                else:
                    # Partial (training) replays skip the full _calendar_proof
                    # enrichment (run_id, notes, summary) to save per-trial work,
                    # but the structural markers size_proof/_Terms need must still
                    # be present. `universe` routes _Terms to the native branch;
                    # `run_id` is the unit identity fed into proof run_id (unused
                    # by _measure, only needed when metrics_only is False).
                    unit.setdefault("universe", CALENDAR_UNIVERSE)
                    if not metrics_only:
                        unit.setdefault(
                            "run_id",
                            _run_id(
                                {
                                    "selection": unit.get("selection", {}),
                                    "daily": unit.get("series", []),
                                    "source_hashes": {
                                        k: v
                                        for k, v in (unit.get("provenance") or {}).items()
                                        if isinstance(v, str) and "sha256" in k
                                    },
                                }
                            ),
                        )
            marks = marks_cache.get(through)
            if marks is None:
                if not dates or dates[0] > unit["series"][0]["date"]:
                    raise W0Error("Нет отметки LQDT на начало истории.")
                marks = {
                    r["date"]: _mark(dates, prices, r.get("benchmark_mark_date") or r["date"][:10])
                    for r in unit["series"]
                }
                marks_cache[through] = marks
            return size_proof(
                unit,
                marks,
                settings,
                contracts,
                metrics_only=metrics_only,
                copy_unit=False,
                _scan_cache=scan_cols_cache if metrics_only else None,
                _scan_key=through,
                scalar_summary=scalar_summary if metrics_only else None,
            )

        return sized_replay

    def _prepare_fixed(
        self,
        selection: dict[str, Any],
        *,
        calendar=None,
        settings: ResearchSettings = STRICT_SCREENS,
        sizing_intents=False,
    ) -> Callable[..., dict[str, Any]]:
        """Request-scoped replay of one instrument/strategy with different numeric settings.

        Load the selected inputs once, not the entire registry per trial. Outside this
        request a new session sees refreshed local files. `through` cuts the actual
        simulator input (including carry's tariff reference), not just its output.
        """
        if not isinstance(selection, dict):
            raise W0Error("выбор должен быть объектом")
        universe = selection.get("universe", CALENDAR_UNIVERSE)
        if universe == PAIR_UNIVERSE:
            from moex_spread_lab.strategies.base import carry_proof_plugin_ids

            from .strategy_catalog import get_strategy_registry

            registry = get_strategy_registry()
            strategy = selection.get("strategy")
            plugin = (
                registry.get(strategy)
                if isinstance(strategy, str) and strategy in carry_proof_plugin_ids(registry)
                else None
            )
            return self._prepare_pair(
                selection, settings, sizing_intents=sizing_intents, carry_plugin=plugin
            )
        if universe != CALENDAR_UNIVERSE:
            raise W0Error("неизвестная вселенная")
        spec = get_timeframe(settings.timeframe)
        if not spec.supports_calendar:
            raise W0Error(
                f"{spec.label}: календарный proof не поддерживается на выбранном интервале."
            )
        calendar = calendar if calendar is not None else self.calendar_data
        cal_cache: dict[str, Any] = {}

        def replay(request, through=None, *, metrics_only: bool = False, scalar_summary=None):
            effective = {
                **request,
                "timeframe": settings.timeframe,
                **({} if "fill" in request else {"fill": settings.price_fill}),
            }
            payload = calendar.proof(
                effective,
                through=through,
                initial_capital=settings.initial_capital_rub,
                timeframe=settings.timeframe,
                metrics_only=metrics_only,
                sizing_intents=sizing_intents,
                _cache=cal_cache,
            )
            # The search only needs trades, daily marks and admissions from partial replays.
            return payload if through is not None else calendar_proof(self.root, effective, payload)

        return replay

    # ----------------------------------------------------------- pair adapter

    def _prepare_pair(
        self,
        selection: dict[str, Any],
        settings: ResearchSettings,
        *,
        sizing_intents=False,
        carry_plugin=None,
    ) -> Callable[..., dict[str, Any]]:
        pair = _pair(selection.get("pair"))
        near, far = pair["near"], pair["far"]
        strategy = selection.get("strategy")
        if not isinstance(strategy, str) or (strategy not in STRATEGIES and carry_plugin is None):
            raise W0Error("неизвестная стратегия")
        screen_strategy = "funding_only" if carry_plugin is not None else strategy
        inputs = self.screener_inputs
        funding = ProofFunding(inputs)
        timeframe = get_timeframe(settings.timeframe)
        if not timeframe.supports_pair_proof:
            raise W0Error(f"{timeframe.label}: pair-proof не поддерживается.")
        entry = _pair_catalog(
            inputs,
            registry=(pair,),
            funding=funding,
            initial_capital=settings.initial_capital_rub,
            timeframe=settings.timeframe,
        )["pairs"][0]
        if not entry["proof_strategies"].get(screen_strategy):
            raise W0Error(entry["data_reasons"][screen_strategy])
        # With a holdout, evaluate liquidity on the actual training replay, not the full catalog flag.
        if not (settings.validation_percent or settings.allow_failed_volume) and not entry[
            "strategies"
        ].get(screen_strategy):
            raise W0Error("Не пройдено правило ликвидности пары.")

        @cache
        def risk_for(quantity):
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
        instruments = _load_instruments(inputs.contracts)
        terms = {secid: instruments.get(secid, {}) for secid in (near, far)}
        dates, prices = _benchmark_marks(inputs.benchmark)
        mean_reversion = strategy == "mean_reversion"
        prepared = (
            self._price_inputs(
                inputs, pair, funding, settings.validation_percent, settings.timeframe
            )
            if mean_reversion
            else self._funding_inputs(inputs, pair, funding, settings.validation_percent)
        )

        # Reuse immutable input hashes throughout this request's Optuna trials.
        source_hashes = {
            "benchmark_sha256": _sha256_bytes(inputs.benchmark),
            "contracts_sha256": _sha256_bytes(inputs.contracts),
            "historical_contracts_sha256": _sha256_bytes(
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

        def replay(request, through=None, *, metrics_only: bool = False, scalar_summary=None):
            execution = {
                **execution_settings(request),
                "sizing_intents": sizing_intents,
                "fill": pair_fill(request, strategy, default_price_fill=settings.price_fill),
                "initial_capital_rub": settings.initial_capital_rub,
            }
            if (
                request.get("strategy") != strategy
                or request.get("pair") != pair["pair_id"]
                or request.get("universe", PAIR_UNIVERSE) != PAIR_UNIVERSE
                or (carry_plugin is not None and automatic(request) != sizing_intents)
            ):
                raise W0Error("Запрос не соответствует подготовленному окну стратегии.")
            params = (
                _parameters(request, strategy)
                if carry_plugin is None
                else _plugin_parameters(carry_plugin, request, pair, settings)
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
                run_mean_reversion(MeanReversionSpec(**common), metrics_only=metrics_only)
                if mean_reversion
                else run_funding(FundingSpec(**common), metrics_only=metrics_only)
            )
            if carry_plugin is not None:
                result["accounting_policy"] = "carry_settlement.v1"
                if not metrics_only and abs(result["summary"]["ledger_reconciliation_rub"]) > 0.02:
                    raise W0Error("Учёт carry-плагина не согласован с журналом сделок.")
            return result

        return replay

    def _prepare_daily_pair_plugin(self, selection, settings, plugin):
        """Pair-plugin route: plugin intents, separate price and accounting calculation."""
        return PairPluginRoute(self, selection, settings, plugin).prepare()

    # ---------------------------------------------------------- pair specifics

    def _price_inputs(self, inputs, pair, funding, validation_percent, timeframe="1d"):
        series, book = pair_price_inputs(inputs, pair, funding, timeframe=timeframe)
        analysis_rows = [row for row in series["rows"] if positive_pair_volume(row)]
        if len(analysis_rows) < 2:
            raise W0Error("fewer than two positive-volume common observations")
        if book is None:
            return series, analysis_rows, None, {}
        window = FundingWindow(book, (r["date"] for r in analysis_rows), validation_percent)
        hashes = funding.source(pair["pair_id"], pair["near"])["source"]["raw_response_sha256"]
        return series, analysis_rows, window, {"near_funding_history_sha256": hashes}

    def _funding_inputs(self, inputs, pair, funding, validation_percent):
        return funding_inputs(inputs, pair, funding, validation_percent)
