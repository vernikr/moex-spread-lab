"""Service facade for `/api/proof/*`."""

from __future__ import annotations

import sys
from collections.abc import Callable
from functools import cache as cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import moex_spread_lab.core.universe as _universe
import moex_spread_lab.core.w0 as _w0
from moex_spread_lab.accounting.account_risk import assess_vtb_pair as assess_vtb_pair
from moex_spread_lab.accounting.position_sizing import (
    automatic,
)
from moex_spread_lab.accounting.position_sizing import (
    size_proof as size_proof,
)
from moex_spread_lab.core.contracts import plugin_is_builtin
from moex_spread_lab.core.settings import STRICT_SCREENS, ResearchSettings
from moex_spread_lab.core.timeframes import get_timeframe
from moex_spread_lab.core.universe import (
    CALENDAR_UNIVERSE as CALENDAR_UNIVERSE,
)
from moex_spread_lab.core.universe import (
    PAIR_UNIVERSE,
)
from moex_spread_lab.core.w0 import W0Error, run_id
from moex_spread_lab.strategies import pair_fill as pair_fill
from moex_spread_lab.strategies.funding_carry.proof_funding import (
    FundingWindow,
)
from moex_spread_lab.strategies.funding_carry.proof_funding import (
    ProofFunding as ProofFunding,
)
from moex_spread_lab.strategies.pair_screener.pair_screener import (
    ScreenerInputs,
    pair_price_inputs,
)
from moex_spread_lab.strategies.pair_screener.pair_screener import (
    _pair as _pair,
)
from moex_spread_lab.strategies.pair_screener.pair_screener import (
    _parameters as _parameters,
)
from moex_spread_lab.strategies.pair_screener.pair_screener import (
    catalog as _pair_catalog,
)
from moex_spread_lab.strategies.pair_screener.pair_screener import (
    execution_settings as execution_settings,
)

from .calendar_adapter import calendar_proof as calendar_proof
from .carry_payload import run_carry
from .carry_route import funding_inputs
from .pair_plugins import PairPluginRoute
from .pair_route import builtin_pair_proof, check_sizing_request, prepare_native, prepare_pair
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

_HashedJSONCache: Any = vars(_w0)["_HashedJSONCache"]
STRATEGIES: dict[str, dict[str, Any]] = cast(
    dict[str, dict[str, Any]], vars(_universe)["STRATEGIES"]
)

# Replay hooks resolved through the module at call time, so tests may patch them.
run_mean_reversion = builtin_pair_proof
run_funding = run_carry

PROOF_SCHEMA = "execution-proof.v1"
FORCED_EXIT_REASONS = frozenset(
    {"end_of_sample", "end_of_window", "open_at_data_end_no_executable_exit"}
)
_BENCHMARK_MARKS_CACHE: Any = _HashedJSONCache(32)
_INSTRUMENTS_CACHE: Any = _HashedJSONCache(32)
_SHA256_CACHE: Any = _HashedJSONCache(512)

_benchmark_marks = benchmark_marks
_load_instruments = load_instruments
_sha256_bytes = sha256_bytes
_mark = mark
_load_contracts_for_side_cost = load_contracts_for_side_cost
_result = result_label
_clean = clean
_events = events


def reference_eval() -> bool:
    """Reference vs metrics-only fast path."""
    import os

    return os.environ.get("MOEX_PROOF_REFERENCE_EVAL", "0") == "1"


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


def _calendar_sizing_unit(
    root: Any, request: Any, unit: Any, through: Any, metrics_only: bool
) -> Any:
    if unit.get("schema_version") == "calendar-proof.v1":
        if through is None:
            unit = calendar_proof(root, request, unit)
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
                    run_id(
                        {
                            "selection": unit.get("selection", {}),
                            "daily": unit.get("series", []),
                            "source_hashes": {
                                k: v
                                for k, v in cast(
                                    dict[str, Any], unit.get("provenance") or {}
                                ).items()
                                if isinstance(v, str) and "sha256" in k
                            },
                        }
                    ),
                )
    return unit


def _intent_request(request: Any, registry: Any, settings: Any) -> dict[str, Any]:
    strategy = request.get("strategy", "zscore_mean_reversion")
    selected_plugin = (
        registry.get(strategy) if isinstance(strategy, str) and strategy in registry else None
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


def prepare_fixed(
    service: Any,
    selection: Any,
    calendar: Any,
    settings: ResearchSettings,
    sizing_intents: Any,
) -> Callable[..., dict[str, Any]]:
    """Request-scoped replay of one instrument/strategy with different numeric settings.

    Load the selected inputs once, not the entire registry per trial. Outside this
    request a new session sees refreshed local files. `through` cuts the actual
    simulator input (including carry's tariff reference), not just its output.
    """
    if not isinstance(selection, dict):
        raise W0Error("выбор должен быть объектом")
    selection = cast(dict[str, Any], selection)
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
        return service._prepare_pair(
            selection, settings, sizing_intents=sizing_intents, carry_plugin=plugin
        )
    if universe != CALENDAR_UNIVERSE:
        raise W0Error("неизвестная вселенная")
    spec = get_timeframe(settings.timeframe)
    if not spec.supports_calendar:
        raise W0Error(f"{spec.label}: календарный proof не поддерживается на выбранном интервале.")
    calendar = calendar if calendar is not None else service.calendar_data
    cal_cache: dict[str, Any] = {}

    def replay(
        request: Any, through: Any = None, *, metrics_only: bool = False, scalar_summary: Any = None
    ) -> dict[str, Any]:
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
        return payload if through is not None else calendar_proof(service.root, effective, payload)

    return replay


def prepare_automatic(
    service: Any,
    selection: dict[str, Any],
    calendar: Any,
    settings: ResearchSettings,
    registry: Any,
    strategy: Any,
) -> Callable[..., dict[str, Any]]:
    """Automatic sizing for request-scoped proof replays."""
    replay = service._prepare_fixed(
        _intent_request(selection, registry, settings),
        calendar=calendar,
        settings=settings,
        sizing_intents=True,
    )
    dates, prices = _benchmark_marks(service.root / "data/benchmark/lqdt-lineage.json")
    contracts = _load_instruments(service.root / "data/w1/current-intake.json")
    marks_cache: dict[str | None, dict[str, float]] = {}
    scan_cols_cache: dict[str | None, Any] = {}

    def sized_replay(
        request: Any,
        through: str | None = None,
        *,
        metrics_only: bool = False,
        scalar_summary: tuple[str, str] | None = None,
        template: bool = False,
    ) -> dict[str, Any]:
        from moex_spread_lab.strategies.base import carry_proof_plugin_ids

        if strategy in carry_proof_plugin_ids(registry) and (
            not isinstance(request, dict)
            or not automatic(cast(dict[str, Any], request))
            or cast(dict[str, Any], request).get("quantity") is not None
        ):
            raise W0Error("Запрос не соответствует подготовленному автоматическому размеру carry.")
        unit = replay(
            _intent_request(request, registry, settings),
            through=through,
            metrics_only=metrics_only,
            **({"template": True} if template else {}),
        )
        unit = _calendar_sizing_unit(service.root, request, unit, through, metrics_only)
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


class ProofService:
    """Facade for /api/proof/*."""

    def __init__(self, root: Path, calendar: Any = None, *, pool_factory: Any = None) -> None:
        self.root = Path(root).resolve()
        self._calendar: Any = calendar
        self._inputs: Any = None
        self._pool: Any = None
        self._pool_factory: Any = pool_factory

    @property
    def calendar_data(self) -> Any:
        if self._calendar is None:
            from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData

            self._calendar = CalendarData(self.root)
        return self._calendar

    @property
    def screener_inputs(self) -> Any:
        if self._inputs is None:
            self._inputs = ScreenerInputs(self.root)
        return self._inputs

    def close(self) -> None:
        """Shut down cohort worker pool."""
        if self._pool is not None:
            self._pool.shutdown(wait_for=True)
            self._pool = None

    def worker_pool(self) -> Any:
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
        selection: Any,
        *,
        calendar: Any = None,
        settings: ResearchSettings = STRICT_SCREENS,
    ) -> Callable[..., dict[str, Any]]:
        if not isinstance(selection, dict):
            raise W0Error("выбор должен быть объектом")
        selection = cast(dict[str, Any], selection)
        from moex_spread_lab.strategies.base import daily_pair_proof_plugin_ids

        from .strategy_catalog import get_strategy_registry

        registry = cast(Any, get_strategy_registry())
        strategy = selection.get("strategy")
        if isinstance(strategy, str) and strategy in daily_pair_proof_plugin_ids(registry):
            check_sizing_request(selection)
            return self._prepare_daily_pair_plugin(selection, settings, registry.get(strategy))
        return self._prepare_native(selection, calendar=calendar, settings=settings)

    def _prepare_native(
        self,
        selection: dict[str, Any],
        *,
        calendar: Any = None,
        settings: ResearchSettings = STRICT_SCREENS,
    ) -> Callable[..., dict[str, Any]]:
        """Prepare a built-in-engine replay, bypassing adapter routing."""
        return prepare_native(self, selection, calendar=calendar, settings=settings)

    def _prepare_automatic(
        self,
        selection: Any,
        calendar: Any,
        settings: ResearchSettings,
        registry: Any,
        strategy: Any,
    ) -> Callable[..., dict[str, Any]]:
        return prepare_automatic(self, selection, calendar, settings, registry, strategy)

    def _prepare_fixed(
        self,
        selection: Any,
        *,
        calendar: Any = None,
        settings: ResearchSettings = STRICT_SCREENS,
        sizing_intents: bool = False,
    ) -> Callable[..., dict[str, Any]]:
        return prepare_fixed(self, selection, calendar, settings, sizing_intents)

    def _prepare_pair(
        self,
        selection: dict[str, Any],
        settings: ResearchSettings,
        *,
        sizing_intents: bool = False,
        carry_plugin: Any = None,
    ) -> Callable[..., dict[str, Any]]:
        return prepare_pair(
            self, selection, settings, sizing_intents, carry_plugin, sys.modules[__name__]
        )

    def _prepare_daily_pair_plugin(
        self, selection: dict[str, Any], settings: ResearchSettings, plugin: Any
    ) -> Callable[..., dict[str, Any]]:
        """Pair-plugin route."""
        return PairPluginRoute(self, selection, settings, plugin).prepare()

    def _price_inputs(
        self, inputs: Any, pair: Any, funding: Any, validation_percent: float, timeframe: str = "1d"
    ) -> tuple[Any, Any, Any, dict[str, Any]]:
        series, book = pair_price_inputs(inputs, pair, funding, timeframe=timeframe)
        rows = [r for r in cast(list[dict[str, Any]], series["rows"]) if positive_pair_volume(r)]
        if len(rows) < 2:
            raise W0Error("fewer than two positive-volume common observations")
        if book is None:
            return series, rows, None, {}
        p = cast(dict[str, Any], pair)
        window = FundingWindow(book, (r["date"] for r in rows), validation_percent)
        hashes = cast(dict[str, Any], funding.source(p["pair_id"], p["near"])["source"])[
            "raw_response_sha256"
        ]
        return series, rows, window, {"near_funding_history_sha256": hashes}

    def _funding_inputs(
        self, inputs: Any, pair: Any, funding: Any, validation_percent: float
    ) -> Any:
        return funding_inputs(inputs, pair, funding, validation_percent)


if TYPE_CHECKING:
    _facade_hook_refs: tuple[object, ...] = (
        _plugin_parameters,
        _calendar_sizing_unit,
        _intent_request,
        _validate_pair_screen,
        _validate_pair_request,
        _validate_carry_result,
    )


# Typed internal exports used by other proof modules.
shared_benchmark_marks = _benchmark_marks
shared_mark = _mark
