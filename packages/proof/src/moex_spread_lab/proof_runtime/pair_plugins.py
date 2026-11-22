"""Pair-plugin proof route.

A registered daily-pair plugin supplies trusted entry/exit intents; prices, costs,
ledger and capital accounting are calculated here, independently of the plugin.
"""

from __future__ import annotations

from typing import Any

from moex_spread_lab.accounting.position_sizing import automatic
from moex_spread_lab.core.contracts import WindowSnapshot
from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.core.universe import PAIR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error, _run_id
from moex_spread_lab.strategies import attach_entry_levels, pair_fill
from moex_spread_lab.strategies.funding_carry.proof_funding import ProofFunding
from moex_spread_lab.strategies.pair_screener.pair_screener import _pair, execution_settings

from .fixed_plugin_payload import fixed_daily_pair_plugin_payload
from .payload_tools import (
    TradeSpec,
    benchmark_marks,
    fill_block,
    point_value,
    trade_block,
)
from .plugin_notes import OPEN_POSITION_NOTE, OPEN_POSITION_PREFIX, PLUGIN_SOURCE_NOTE


class PairPluginRoute:
    """Pair-plugin route: plugin intents with an independently calculated account."""

    def __init__(
        self, service: Any, selection: dict[str, Any], settings: ResearchSettings, plugin: Any
    ):
        self.service = service
        self.selection = selection
        self.settings = settings
        self.plugin = plugin

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
        from .pair_plugin_accounting import prepare_daily_pair_unit, size_daily_pair_plugin

        snapshot, funding_window, marks, settings = prepared
        params = request.get("parameters", {})
        if automatic(request):
            actual = size_daily_pair_plugin(
                plugin, snapshot, params, fill, funding_window, marks, settings, through=through
            )
            return None, None, actual
        execution = execution_settings(request)
        unit, _ = prepare_daily_pair_unit(
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

    def prepare(self):
        """Return the request-scoped replay callable for this plugin window."""
        pair = self._validate_window()
        # Native pair preflight owns data, funding, liquidity and capital screens.
        native_selection = {**self.selection, "strategy": "mean_reversion", "parameters": {}}
        native = self.service.prepare(native_selection, settings=self.settings)
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

        def replay(request, through=None, *, metrics_only=False, scalar_summary=None):
            if (
                not isinstance(request, dict)
                or request.get("strategy") != self.plugin.strategy_id
                or request.get("universe") != PAIR_UNIVERSE
                or request.get("pair") != pair["pair_id"]
                or automatic(request) != automatic(self.selection)
                or (automatic(request) and request.get("quantity") is not None)
            ):
                raise W0Error("Запрос не соответствует подготовленному окну плагина.")
            fill = pair_fill(
                request, self.plugin.strategy_id, default_price_fill=self.settings.price_fill
            )
            unit, execution, actual = self._unit_or_actual(
                self.plugin, request, plugin_prepared, fill, through
            )
            # The native proof supplies trusted market/chart/screen metadata only;
            # never retain its decisions, ledger or indicator values.
            template = native(
                {**request, "strategy": "mean_reversion", "parameters": {}, "fill": fill},
                through=through,
            )
            if actual is None:
                if unit is None or execution is None:
                    raise W0Error("Плагин не вернул единичный расчёт для фиксированного размера.")
                return fixed_daily_pair_plugin_payload(
                    template,
                    unit,
                    (series, pair, provenance),
                    execution,
                    benchmark,
                )
            return enrich_sized_plugin_payload(
                template, actual, series, pair, provenance, benchmark
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
    by_date = {row["date"]: row for row in template["series"]}
    enriched_rows = [{**by_date[row["date"]], **row, "signal_z": None} for row in actual["series"]]
    indexed = {row["date"]: row for row in enriched_rows}
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
    template["notes"] = [
        note for note in template["notes"] if not note.startswith(OPEN_POSITION_PREFIX)
    ]
    if any(trade["open_at_end"] for trade in enriched_trades):
        template["notes"].append(OPEN_POSITION_NOTE)
    template["notes"].append(PLUGIN_SOURCE_NOTE)
    attach_entry_levels(template)
    template["run_id"] = _run_id(
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
    blocks = {}
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
