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

import sys
from collections.abc import Callable
from functools import cache as cache
from pathlib import Path
from typing import Any

from moex_spread_lab.accounting.account_risk import assess_vtb_pair as assess_vtb_pair
from moex_spread_lab.accounting.position_sizing import AUTO_SIZING, automatic
from moex_spread_lab.accounting.position_sizing import size_proof as size_proof
from moex_spread_lab.core.evaluation_periods import STRICT_SCREENS, ResearchSettings
from moex_spread_lab.core.timeframes import get_timeframe
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE as CALENDAR_UNIVERSE
from moex_spread_lab.core.universe import (
    PAIR_UNIVERSE,
)
from moex_spread_lab.core.universe import (
    STRATEGIES as STRATEGIES,
)
from moex_spread_lab.core.w0 import W0Error, _HashedJSONCache
from moex_spread_lab.core.w0 import _run_id as _run_id
from moex_spread_lab.strategies import pair_fill as pair_fill
from moex_spread_lab.strategies.funding_carry.proof_funding import FundingWindow
from moex_spread_lab.strategies.funding_carry.proof_funding import ProofFunding as ProofFunding
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
from moex_spread_lab.strategies.pair_screener.pair_screener import catalog as _pair_catalog
from moex_spread_lab.strategies.pair_screener.pair_screener import (
    execution_settings as execution_settings,
)

from .calendar_adapter import calendar_proof as calendar_proof
from .funding_engine import FundingSpec as FundingSpec
from .funding_engine import funding_inputs
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

PROOF_SCHEMA = "execution-proof.v1"
FORCED_EXIT_REASONS = frozenset(
    {"end_of_sample", "end_of_window", "open_at_data_end_no_executable_exit"}
)
_BENCHMARK_MARKS_CACHE: _HashedJSONCache[
    tuple[str, int, int], tuple[tuple[list[str], dict[str, float]], str]
] = _HashedJSONCache(32)
_INSTRUMENTS_CACHE: _HashedJSONCache[
    tuple[str, int, int, int | None, int | None], tuple[dict[str, dict[str, Any]], str]
] = _HashedJSONCache(32)
_SHA256_CACHE: _HashedJSONCache[tuple[str, int, int], tuple[None, str]] = _HashedJSONCache(512)

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
    return plugin_parameters(plugin, request, pair, settings, W0Error, PAIR_UNIVERSE)


def _calendar_sizing_unit(root, request, unit, through, metrics_only):
    return calendar_sizing_unit(root, request, unit, through, metrics_only, sys.modules[__name__])


def _intent_request(request, registry, settings):
    return intent_request(request, registry, settings)


def _validate_pair_screen(inputs, pair, funding, screen_strategy, settings):
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


def _validate_pair_request(request, strategy, pair, carry_plugin, sizing_intents):
    if (
        request.get("strategy") != strategy
        or request.get("pair") != pair["pair_id"]
        or request.get("universe", PAIR_UNIVERSE) != PAIR_UNIVERSE
        or (carry_plugin is not None and automatic(request) != sizing_intents)
    ):
        raise W0Error("Запрос не соответствует подготовленному окну стратегии.")


def _validate_carry_result(result, carry_plugin, metrics_only):
    if carry_plugin is not None:
        result["accounting_policy"] = "carry_settlement.v1"
        if not metrics_only and abs(result["summary"]["ledger_reconciliation_rub"]) > 0.02:
            raise W0Error("Учёт carry-плагина не согласован с журналом сделок.")


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

        return self._prepare_automatic(selection, calendar, settings, registry, strategy)

    def _prepare_automatic(self, selection, calendar, settings, registry, strategy):
        return prepare_automatic(
            self, selection, calendar, settings, registry, strategy, sys.modules[__name__]
        )

    def _prepare_fixed(
        self,
        selection: dict[str, Any],
        *,
        calendar=None,
        settings: ResearchSettings = STRICT_SCREENS,
        sizing_intents=False,
    ) -> Callable[..., dict[str, Any]]:
        return prepare_fixed(
            self, selection, calendar, settings, sizing_intents, sys.modules[__name__]
        )

    # ----------------------------------------------------------- pair adapter

    def _prepare_pair(
        self,
        selection: dict[str, Any],
        settings: ResearchSettings,
        *,
        sizing_intents=False,
        carry_plugin=None,
    ) -> Callable[..., dict[str, Any]]:
        return prepare_pair(
            self, selection, settings, sizing_intents, carry_plugin, sys.modules[__name__]
        )

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
