"""Compare the internal pair-plugin composition with actual proof input loading."""

from dataclasses import replace

import pytest

from funding_fixtures import PAIR, fixture, request
from moex_spread_lab.core.contracts import WindowSnapshot
from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.proof_runtime.pair_plugin_accounting import size_daily_pair_plugin
from moex_spread_lab.proof_runtime.service import ProofService
from moex_spread_lab.strategies.funding_carry.proof_funding import ProofFunding
from moex_spread_lab.strategies.pair_mean_reversion.plugin import PairMeanReversionPlugin
from moex_spread_lab.strategies.pair_screener.pair_screener import _pair


class ExplicitSignalMeanReversion(PairMeanReversionPlugin):
    """Adapt legacy intents to the explicit pair-price intent schema."""

    def evaluate_intents(self, window, params, fill):
        run = super().evaluate_intents(window, params, fill)
        days = [row["date"] for row in window.rows]
        delay = 0 if fill == "reference_same_bar" else 1
        return replace(
            run,
            intents=tuple(
                {
                    **item,
                    "signal": {
                        "kind": "z",
                        "date": days[days.index(item["entry_date"][:10]) - delay],
                        "value": item["entry_signal_z"],
                    },
                }
                for item in run.intents
            ),
        )


@pytest.mark.parametrize("fill", ["next_open", "next_close", "reference_same_bar"])
@pytest.mark.parametrize("validation", [0, 20])
def test_loaded_pair_plugin_matches_native_accounting(tmp_path, fill, validation):
    fixture(tmp_path)
    service = ProofService(tmp_path)
    settings = ResearchSettings(validation_percent=validation, price_fill=fill)
    selection = {**request(), "sizing": "max_affordable", "fill": fill}
    native = service.prepare(selection, settings=settings)
    pair = _pair(PAIR)
    series, _, funding_window, _ = service._price_inputs(
        service.screener_inputs, pair, ProofFunding(service.screener_inputs), validation, "1d"
    )
    snap = WindowSnapshot(
        universe="pair_screen",
        pair=PAIR,
        near=pair["near"],
        far=pair["far"],
        rows=series["rows"],
        contracts=series["contracts"],
    )
    marks = {row["date"]: row["benchmark_price"] for row in series["rows"]}
    for through in (None, funding_window.training_end) if validation else (None,):
        expected = native(selection, through=through)
        actual = size_daily_pair_plugin(
            ExplicitSignalMeanReversion(),
            snap,
            selection["parameters"],
            fill,
            funding_window,
            marks,
            settings,
            through=through,
        )
        assert actual["trades"]
        assert len(actual["trades"]) == len(expected["trades"])
        for a, e in zip(actual["trades"], expected["trades"], strict=True):
            for key in (
                "side",
                "quantity",
                "exit_reason",
                "open_at_end",
                "funding_rub",
                "gross_rub",
                "costs_rub",
                "net_rub",
            ):
                assert a[key] == e[key], key
            for event in ("entry", "exit"):
                for key in ("date", "near_price", "far_price", "spread_fill", "costs"):
                    assert a[event][key] == e[event][key], (event, key)
        for a, e in zip(actual["ledger"], expected["ledger"], strict=True):
            for key in (
                "date",
                "quantity",
                "mark_pnl_rub",
                "funding_rub",
                "costs_rub",
                "cumulative_net_rub",
            ):
                assert a[key] == e[key], key
        assert actual["summary"]["ledger_reconciliation_rub"] == 0
