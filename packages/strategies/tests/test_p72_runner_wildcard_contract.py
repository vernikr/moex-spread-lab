"""Integration regression for the pre-P7.2 runner star-import surface."""

from moex_spread_lab.strategies.pair_screener import pair_screener, runner


def test_runner_wildcard_and_hook_identity():
    namespace = {}
    exec("from moex_spread_lab.strategies.pair_screener.runner import *", namespace)
    assert set(namespace) - {"__builtins__"} == {
        "Any",
        "Callable",
        "FundingWindow",
        "MAX_QUANTITY",
        "STRATEGIES",
        "ScreenerError",
        "ScreenerInputs",
        "StrategyRunOutcome",
        "TimeframeSpec",
        "annotations",
        "assemble_screener_result",
        "assess_vtb_pair",
        "build_carry_strategy",
        "build_carry_study",
        "build_result",
        "catalog",
        "dataclass",
        "execution_settings",
        "fetch_history",
        "get_timeframe",
        "hashlib",
        "math",
        "mean_reversion_notes",
        "pair_price_inputs",
        "read_settings",
        "run_mean_reversion_baseline",
        "tariff_profile",
    }
    assert pair_screener._parameters is runner._parameters
