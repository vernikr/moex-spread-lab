"""Opt-in cache telemetry is per context and does not alter LRU behaviour."""

from concurrent.futures import ThreadPoolExecutor

from moex_spread_lab.core.cache_metrics import collect_cache_metrics, record_cache_lookup
from moex_spread_lab.core.hashed_json_cache import HashedJSONCache


def test_json_cache_hits_misses_and_context_cleanup():
    cache = HashedJSONCache(1)
    key = ("file", 1, 2)
    with collect_cache_metrics() as counters:
        assert cache.get(key) is None
        cache.put(key, ({"v": 1}, "hash"))
        assert cache.get(key) == ({"v": 1}, "hash")
        record_cache_lookup("zscore", True)
        assert counters == {"json_hits": 1, "json_misses": 1, "zscore_hits": 1, "zscore_misses": 0}
    cache.get(key)
    assert counters["json_hits"] == 1


def test_other_threads_do_not_contaminate_request():
    with collect_cache_metrics() as counters, ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(record_cache_lookup, "json", True).result()
        assert counters["json_hits"] == 0
