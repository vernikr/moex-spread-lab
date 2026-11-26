from moex_spread_lab.core.contracts import WindowSnapshot
from moex_spread_lab.strategies.pair_mean_reversion import plugin as plugin_module
from moex_spread_lab.strategies.pair_mean_reversion.plugin import PairMeanReversionPlugin


def test_signal_day_uses_positive_volume_rows(monkeypatch):
    rows = [
        {
            "date": "2026-01-01",
            "near_volume": 10,
            "far_volume": 10,
            "near_open": 100,
            "far_open": 90,
        },
        {
            "date": "2026-01-02",
            "near_volume": 0,
            "far_volume": 10,
            "near_open": 101,
            "far_open": 91,
        },
        {
            "date": "2026-01-03",
            "near_volume": 10,
            "far_volume": 10,
            "near_open": 102,
            "far_open": 92,
        },
        {
            "date": "2026-01-04",
            "near_volume": 10,
            "far_volume": 10,
            "near_open": 103,
            "far_open": 93,
        },
    ]
    contracts = {
        "NEAR": {"tick_size": 1, "tick_value": 1, "lot_size": 1, "initial_margin": 1},
        "FAR": {"tick_size": 1, "tick_value": 1, "lot_size": 1, "initial_margin": 1},
    }

    def fake_baseline(series, **options):
        assert [row["date"] for row in series["rows"]] == ["2026-01-01", "2026-01-03", "2026-01-04"]
        return {
            "trades": [
                {
                    "direction": "long_near_short_far",
                    "entry_date": "2026-01-03",
                    "exit_date": "2026-01-04",
                    "entry_near_fill": 102,
                    "entry_far_fill": 92,
                    "exit_near_fill": 103,
                    "exit_far_fill": 93,
                    "entry_signal_z": 2.0,
                    "exit_reason": "mean_reversion_exit",
                }
            ]
        }

    monkeypatch.setattr(plugin_module, "run_mean_reversion_baseline", fake_baseline)
    window = WindowSnapshot(
        universe="pair_screen",
        pair="NEAR/FAR",
        near="NEAR",
        far="FAR",
        rows=rows,
        contracts=contracts,
    )
    result = PairMeanReversionPlugin().evaluate_intents(
        window, {"lookback": 5, "entry_z": 2.0, "exit_z": 0.5}, "next_open"
    )
    assert result.intents[0]["signal"]["date"] == "2026-01-01"
