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
    AUTO_SIZING,
    automatic,
)
from moex_spread_lab.accounting.position_sizing import (
    size_proof as size_proof,
)
from moex_spread_lab.core.evaluation_periods import STRICT_SCREENS, ResearchSettings
from moex_spread_lab.core.timeframes import get_timeframe
from moex_spread_lab.core.universe import (
    CALENDAR_UNIVERSE as CALENDAR_UNIVERSE,
)
from moex_spread_lab.core.universe import (
    PAIR_UNIVERSE,
)
from moex_spread_lab.core.w0 import W0Error
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

from . import funding_engine as _funding_engine
from .calendar_adapter import calendar_proof as calendar_proof
from .funding_engine import FundingSpec as FundingSpec
from .funding_payloads import run_funding as run_funding
from .mean_reversion_payloads import run_mean_reversion as run_mean_reversion
from .mean_reversion_spec import MeanReversionSpec as MeanReversionSpec
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
from .service_calendar import calendar_sizing_unit, prepare_fixed
from .service_pair import prepare_pair
from .service_sizing import prepare_automatic
from .service_validation import intent_request, plugin_parameters

_HashedJSONCache: Any = vars(_w0)["_HashedJSONCache"]
_run_id: Any = vars(_w0)["_run_id"]
STRATEGIES: dict[str, dict[str, Any]] = cast(
    dict[str, dict[str, Any]], vars(_universe)["STRATEGIES"]
)
funding_inputs: Callable[..., Any] = cast(
    Callable[..., Any], vars(_funding_engine)["funding_inputs"]
)

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
    return plugin_parameters(plugin, request, pair, settings, W0Error, PAIR_UNIVERSE)


def _calendar_sizing_unit(
    root: Any, request: Any, unit: Any, through: Any, metrics_only: bool
) -> Any:
    return calendar_sizing_unit(root, request, unit, through, metrics_only, sys.modules[__name__])


def _intent_request(request: Any, registry: Any, settings: Any) -> dict[str, Any]:
    return intent_request(request, registry, settings)


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
    if carry_plugin is None:
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
            if selection.get("sizing") not in (None, AUTO_SIZING):
                raise W0Error("Неизвестный режим расчёта размера позиции.")
            if automatic(selection) and selection.get("quantity") is not None:
                raise W0Error("При автоматическом размере количество не задаётся вручную.")
            return self._prepare_daily_pair_plugin(selection, settings, registry.get(strategy))
        if selection.get("sizing") not in (None, AUTO_SIZING):
            raise W0Error("Неизвестный режим расчёта размера позиции.")
        if not automatic(selection):
            return self._prepare_fixed(selection, calendar=calendar, settings=settings)

        return self._prepare_automatic(selection, calendar, settings, registry, strategy)

    def _prepare_automatic(
        self,
        selection: Any,
        calendar: Any,
        settings: ResearchSettings,
        registry: Any,
        strategy: Any,
    ) -> Callable[..., dict[str, Any]]:
        return prepare_automatic(
            self, selection, calendar, settings, registry, strategy, sys.modules[__name__]
        )

    def _prepare_fixed(
        self,
        selection: Any,
        *,
        calendar: Any = None,
        settings: ResearchSettings = STRICT_SCREENS,
        sizing_intents: bool = False,
    ) -> Callable[..., dict[str, Any]]:
        return prepare_fixed(
            self, selection, calendar, settings, sizing_intents, sys.modules[__name__]
        )

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
