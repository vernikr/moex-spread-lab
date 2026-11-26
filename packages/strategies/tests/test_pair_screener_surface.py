from moex_spread_lab.core.hashed_json_cache import HashedJSONCache
from moex_spread_lab.strategies.pair_screener import inputs as inputs_module
from moex_spread_lab.strategies.pair_screener import normalization as normalization_module
from moex_spread_lab.strategies.pair_screener.inputs import ScreenerInputs


def test_pair_screener_uses_bounded_caches_and_curated_exports():
    assert isinstance(inputs_module._CANDLE_SUMMARY_CACHE, HashedJSONCache)
    assert isinstance(normalization_module._NORMALIZED_BY_TS_CACHE, HashedJSONCache)
    assert not {"annotations", "Any", "dataclass", "math"} & set(inputs_module.__all__)
    assert not {"annotations", "Any", "bisect_left", "partial"} & set(normalization_module.__all__)
    assert not hasattr(ScreenerInputs, "screen_history")
    assert not hasattr(ScreenerInputs, "search_history")
