"""Pair-plugin proof route.

A registered daily-pair plugin supplies trusted entry/exit intents; prices, costs,
ledger and capital accounting are calculated here, independently of the plugin.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from moex_spread_lab.accounting.position_sizing import automatic
from moex_spread_lab.core.contracts import WindowSnapshot
from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.core.universe import PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error, run_id
from moex_spread_lab.strategies import attach_entry_levels, pair_fill
from moex_spread_lab.strategies.funding_carry.proof_funding import ProofFunding
from moex_spread_lab.strategies.pair_screener.pair_screener import _pair, execution_settings
from moex_spread_lab.strategies.registry import BUILTIN_STRATEGY_IDS

from . import pair_plugin_accounting as _pair_plugin_accounting
from .fixed_plugin_payload import fixed_daily_pair_plugin_payload
from .payload_tools import (
    TradeSpec,
    benchmark_marks,
    fill_block,
    point_value,
    trade_block,
)
from .plugin_notes import (
    apply_pair_notes,
    merge_pair_series_row,
)

_Obj = dict[str, Any]

# ``pair_plugin_accounting`` is not yet on the strict list; bind its callables
# through explicit signatures so this strict file sees fully-typed calls without
# changing runtime behavior or the lazy-import contract of the accounting module.
_prepare_daily_pair_unit = cast(
    Callable[..., tuple[_Obj, dict[str, float]]],
    _pair_plugin_accounting.__dict__["prepare_daily_pair_unit"],
)
_size_daily_pair_plugin = cast(
    Callable[..., _Obj], _pair_plugin_accounting.__dict__["size_daily_pair_plugin"]
)


class PairPluginRoute:
    """Pair-plugin route: plugin intents with an independently calculated account."""

    def __init__(
        self, service: Any, selection: dict[str, Any], settings: ResearchSettings, plugin: Any
    ):
        self.service = service
        self.selection = selection
        self.settings = settings
        self.plugin = plugin
        self._builtin = plugin.strategy_id in BUILTIN_STRATEGY_IDS

    def _template_request(self, req: dict[str, Any], fill: str) -> dict[str, Any]:
        """Native template request; a built-in proves its own engine with real parameters."""
        if self._builtin:
            return {**req, "parameters": dict(req.get("parameters") or {}), "fill": fill}
        return {**req, "strategy": "mean_reversion", "parameters": {}, "fill": fill}

    def _ensure_request(self, request: Any, pair_id: str) -> None:
        """Reject a request that does not match the prepared plugin window."""
        req = cast(dict[str, Any], request)
        if (
            not isinstance(request, dict)
            or req.get("strategy") != self.plugin.strategy_id
            or req.get("universe") != PAIR_UNIVERSE
            or req.get("pair") != pair_id
            or automatic(req) != automatic(self.selection)
            or (automatic(req) and req.get("quantity") is not None)
        ):
            raise W0Error("Запрос не соответствует подготовленному окну плагина.")

    def _validate_window(self) -> dict[str, str]:
        """Reject windows the plugin cannot serve and resolve the pair being proven."""
        if (
            self.selection.get("universe") != PAIR_UNIVERSE
            or self.settings.timeframe not in ("1d", "1h")
            or (self.settings.timeframe == "1h" and self.plugin.requires_daily_bars)
        ):
            raise W0Error("Парный плагин не поддерживает выбранное окно или интервал.")
        return _pair(self.selection.get("pair"))

    def _prepared_inputs(self, pair: dict[str, str]) -> tuple[Any, ...]:
        """Series, funding window and provenance shared by every trial of this window."""
        inputs = self.service.screener_inputs
        series, _, funding_window, provenance = self.service._price_inputs(
            inputs,
            pair,
            ProofFunding(inputs),
            self.settings.validation_percent,
            self.settings.timeframe,
        )
        return series, funding_window, provenance, inputs

    @staticmethod
    def _unit_or_actual(
        plugin: Any,
        request: dict[str, Any],
        prepared: tuple[Any, ...],
        fill: str,
        through: str | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        """Run the plugin's intents for this trial: sized in automatic mode, unit otherwise."""
        snapshot, funding_window, marks, settings = prepared
        params = request.get("parameters", {})
        if automatic(request):
            actual = _size_daily_pair_plugin(
                plugin,
                snapshot,
                params,
                fill,
                funding_window,
                marks,
                settings,
                through=through,
            )
            return None, None, actual
        execution = execution_settings(request)
        unit, _ = _prepare_daily_pair_unit(
            plugin,
            snapshot,
            params,
            fill,
            funding_window,
            marks,
            settings,
            through=through,
        )
        return unit, execution, None

    def prepare(self) -> Callable[..., dict[str, Any]]:
        """Return the request-scoped replay callable for this plugin window."""
        pair = self._validate_window()
        # Native pair preflight owns data, funding, liquidity and capital screens. A built-in
        # keeps its real parameters: the native replay is the canonical intent pipeline itself.
        native_selection: dict[str, Any] = {
            **self.selection,
            "strategy": "mean_reversion",
            "parameters": (dict(self.selection.get("parameters") or {}) if self._builtin else {}),
        }
        native = self.service._prepare_native(native_selection, settings=self.settings)
        if self._builtin:
            return self._builtin_replay(native, pair["pair_id"])
        series, funding_window, provenance, inputs = self._prepared_inputs(pair)
        snapshot = WindowSnapshot(
            universe=PAIR_UNIVERSE,
            pair=pair["pair_id"],
            timeframe=self.settings.timeframe,
            near=pair["near"],
            far=pair["far"],
            rows=series["rows"],
            contracts=series["contracts"],
        )
        marks = {row["date"]: row["benchmark_price"] for row in series["rows"]}
        benchmark = benchmark_marks(inputs.benchmark)
        plugin_prepared = (snapshot, funding_window, marks, self.settings)

        def replay(
            request: Any,
            through: str | None = None,
            *,
            metrics_only: bool = False,
            scalar_summary: Any | None = None,
        ) -> dict[str, Any]:
            req = cast(dict[str, Any], request)
            self._ensure_request(req, pair["pair_id"])
            fill = pair_fill(
                req, self.plugin.strategy_id, default_price_fill=self.settings.price_fill
            )
            unit, execution, actual = self._unit_or_actual(
                self.plugin, req, plugin_prepared, fill, through
            )
            # The native proof supplies trusted market/chart/screen metadata only;
            # never retain its decisions, ledger or indicator values.
            template = native(self._template_request(req, fill), through=through, template=True)
            if actual is None:
                if unit is None or execution is None:
                    raise W0Error("Плагин не вернул единичный расчёт для фиксированного размера.")
                return fixed_daily_pair_plugin_payload(
                    template, unit, (series, pair, provenance), execution, benchmark
                )
            return enrich_sized_plugin_payload(
                template, actual, series, pair, provenance, benchmark
            )

        return replay

    def _builtin_replay(
        self, native: Callable[..., dict[str, Any]], pair_id: str
    ) -> Callable[..., dict[str, Any]]:
        """A built-in plugin is proven by the native replay of the same intent pipeline."""

        def replay(
            request: Any,
            through: str | None = None,
            *,
            metrics_only: bool = False,
            scalar_summary: Any | None = None,
        ) -> dict[str, Any]:
            req = cast(dict[str, Any], request)
            self._ensure_request(req, pair_id)
            fill = pair_fill(
                req, self.plugin.strategy_id, default_price_fill=self.settings.price_fill
            )
            return native(
                self._template_request(req, fill), through=through, metrics_only=metrics_only
            )

        return replay


def enrich_sized_plugin_payload(
    template: dict[str, Any],
    actual: dict[str, Any],
    series: dict[str, Any],
    pair: dict[str, Any],
    provenance: dict[str, Any],
    benchmark: tuple[list[str], dict[str, float]],
) -> dict[str, Any]:
    """Merge plugin decisions into the native template: market metadata stays native.

    Only plugin trades, ledger, summary and sizing cross into the payload; the series
    keeps its native chart/screen columns with `signal_z` cleared.
    """
    by_date: dict[str, dict[str, Any]] = {row["date"]: row for row in template["series"]}
    enriched_rows: list[dict[str, Any]] = [
        merge_pair_series_row(by_date[row["date"]], row, builtin=False) for row in actual["series"]
    ]
    indexed: dict[str, dict[str, Any]] = {row["date"]: row for row in enriched_rows}
    far_mult = point_value(series["contracts"][pair["far"]])
    enriched_trades = [
        _enriched_trade(trade, indexed, far_mult, benchmark) for trade in actual["trades"]
    ]
    if abs(actual["summary"]["ledger_reconciliation_rub"]) > 0.02:
        raise W0Error("Учёт плагина не согласован с журналом сделок.")
    actual["summary"]["fx_point_value_drift_rub"] = None
    template.update(
        selection=actual["selection"],
        series=enriched_rows,
        trades=enriched_trades,
        ledger=actual["ledger"],
        summary=actual["summary"],
        sizing=actual["sizing"],
        skipped_entries=actual["skipped_entries"],
    )
    template["provenance"].update({**provenance, **actual["provenance"]})
    apply_pair_notes(template, enriched_trades)
    attach_entry_levels(template)
    template["run_id"] = run_id(
        {
            "selection": template["selection"],
            "series": template["series"],
            "trades": template["trades"],
            "ledger": template["ledger"],
            "sizing": template["sizing"],
            "provenance": template["provenance"],
        }
    )
    return template


def _enriched_trade(
    trade: dict[str, Any],
    indexed: dict[str, dict[str, Any]],
    far_mult: float,
    benchmark: tuple[list[str], dict[str, float]],
) -> dict[str, Any]:
    """Trade row with real fill blocks rebuilt against the enriched series."""
    blocks: dict[str, dict[str, Any]] = {}
    for event in ("entry", "exit"):
        raw = trade[event]
        blocks[event] = fill_block(
            raw["date"],
            raw["spread_fill"],
            raw["near_price"],
            raw["far_price"],
            indexed,
            raw["costs"],
            far_value=raw["far_price"] * far_mult,
        )
        blocks[event]["margin_rub"] = raw.get("margin_rub")
    full = trade_block(
        TradeSpec(
            number=trade["number"],
            side=trade["side"],
            quantity=trade["quantity"],
            signal=trade["signal"],
            entry=blocks["entry"],
            exit=blocks["exit"],
            exit_reason=trade["exit_reason"],
            margin_pair=trade["margin_rub"],
            gross_rub=trade["gross_rub"],
            costs_rub=trade["costs_rub"],
            net_rub=trade["net_rub"],
            open_at_end=trade["open_at_end"],
            funding_rub=trade.get("funding_rub"),
        ),
        benchmark_dates=benchmark[0],
        benchmark_prices=benchmark[1],
    )
    if "sizing" in trade:
        full["sizing"] = trade["sizing"]
    return full
