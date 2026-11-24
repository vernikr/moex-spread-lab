"""Compare the internal pair-plugin composition with actual proof input loading."""

from dataclasses import replace

import pytest

from funding_fixtures import PAIR, fixture, request
from moex_spread_lab.core.contracts import WindowSnapshot
from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.proof.pair_plugin_accounting import size_daily_pair_plugin
from moex_spread_lab.proof.service import ProofService
from moex_spread_lab.strategies.funding_carry.proof_funding import ProofFunding
from moex_spread_lab.strategies.pair_mean_reversion.plugin import PairMeanReversionPlugin
from moex_spread_lab.strategies.pair_screener.pair_screener import _pair
from pair_plugin_real_proof_parity_assertions_p72_packages_proof_tests_test_pair_plugin_real_proof_parity import (
    assert_loaded_pair_plugin_matches_native_accounting,
)


class ExplicitSignalMeanReversion(PairMeanReversionPlugin):
    """Adapt legacy intents to the explicit pair-price intent schema."""

    supported_fills: tuple[str, ...] = ("next_open", "next_close", "reference_same_bar")

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
    # The built-in now runs on its own adapter route; the native engine stays reachable as the
    # accounting reference for this parity check. Automatic sizing derives the quantity itself.
    native = service._prepare_native(
        {key: value for key, value in selection.items() if key != "quantity"}, settings=settings
    )
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
        assert_loaded_pair_plugin_matches_native_accounting(actual, expected)


def _proof_settings(service: ProofService) -> ResearchSettings:
    """Mirror the strict settings ``ProofService.proof`` derives from the saved ones."""
    saved = ResearchSettings.load(service.root)
    return ResearchSettings(
        validation_percent=saved.validation_percent,
        allow_failed_volume=False,
        allow_failed_capital=False,
        slippage_ticks=saved.slippage_ticks,
        initial_capital_rub=saved.initial_capital_rub,
        timeframe=saved.timeframe,
        price_fill=saved.price_fill,
    )


@pytest.mark.parametrize("fill", ["next_open", "reference_same_bar"])
def test_builtin_mean_reversion_adapter_payload_matches_native_engine(tmp_path, fill):
    """The built-in runs its own adapter route; the payload must stay byte-identical."""
    fixture(tmp_path)
    service = ProofService(tmp_path)
    settings = _proof_settings(service)

    fixed = {**request(), "fill": fill}
    native_fixed = service._prepare_native(
        {key: value for key, value in fixed.items() if key != "quantity"}, settings=settings
    )
    assert service.proof(fixed) == native_fixed(fixed)

    auto = {**request(), "fill": fill, "sizing": "max_affordable"}
    auto.pop("quantity")
    auto["parameters"] = {"lookback": 7, "entry_z": 1.2, "exit_z": 0.6}
    native_auto = service._prepare_native(auto, settings=settings)
    assert service.proof(auto) == native_auto(auto)
