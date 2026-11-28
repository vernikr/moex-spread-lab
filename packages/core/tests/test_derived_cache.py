"""Derived cache, opt-in cache telemetry and core JSON boundary contracts."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from moex_spread_lab.core import derived_cache
from moex_spread_lab.core.derived_cache import (
    HashedJSONCache,
    collect_cache_metrics,
    record_cache_lookup,
)
from moex_spread_lab.core.w0 import JSONBoundaryError, as_str_dict, load_object


class DomainError(ValueError):
    pass


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "data" / "benchmark").mkdir(parents=True)
    (root / "data" / "analytics").mkdir(parents=True)
    (root / "src").mkdir(parents=True)
    (root / "data" / "benchmark" / "lqdt-lineage.json").write_text('{"v":1}')
    (root / "data" / "analytics" / "screener-runs.jsonl").write_text('{"run":1}\n')
    (root / "src" / "module.py").write_text("VALUE = 1\n")
    return root


def _mtime_ns(path: Path) -> int:
    return path.stat().st_mtime_ns


def test_fingerprint_covers_data_and_sources(tmp_path):
    root = _root(tmp_path)
    first = derived_cache.fingerprint(root, "ns", ("a", "1"))
    assert first == derived_cache.fingerprint(root, "ns", ("a", "1"))
    assert first != derived_cache.fingerprint(root, "ns", ("a", "2"))  # parts are keyed
    assert first != derived_cache.fingerprint(root, "other", ("a", "1"))

    (root / "data" / "benchmark" / "lqdt-lineage.json").write_text('{"v":2}')
    assert derived_cache.fingerprint(root, "ns", ("a", "1")) != first


def test_fingerprint_covers_symlinked_data_directories(tmp_path):
    root = _root(tmp_path)
    external = tmp_path / "market-data"
    external.mkdir()
    (external / "candles.csv").write_text("a,b\n1,2\n")
    (root / "data" / "market").symlink_to(external, target_is_directory=True)
    before = derived_cache.fingerprint(root, "ns")
    files = derived_cache.contributing_files(root)
    assert any(entry[0] == "data/market/candles.csv" for entry in files)
    (external / "candles.csv").write_text("a,b\n3,4\nCHANGED\n")
    assert derived_cache.fingerprint(root, "ns") != before


def test_symlink_cycle_is_not_walked_twice(tmp_path):
    root = _root(tmp_path)
    loop_a = root / "data" / "loop_a"
    loop_a.mkdir()
    (loop_a / "file.txt").write_text("payload\n")
    (loop_a / "back").symlink_to(root / "data", target_is_directory=True)
    files = derived_cache.contributing_files(root)
    paths = [entry[0] for entry in files]
    assert paths.count("data/loop_a/file.txt") == 1


def test_analytics_runtime_state_is_excluded(tmp_path):
    root = _root(tmp_path)
    before = derived_cache.fingerprint(root, "ns")
    (root / "data" / "analytics" / "screener-runs.jsonl").write_text('{"run":2}\n')
    assert derived_cache.fingerprint(root, "ns") == before


def test_same_size_same_mtime_tick_edit_invalidates(tmp_path):
    root = _root(tmp_path)
    target = root / "data" / "benchmark" / "lqdt-lineage.json"
    stamp_ns = 1_700_000_000_000_000_000  # frozen inside a single 1-second tick
    target.write_text('{"v":111}')
    os.utime(target, ns=(stamp_ns, stamp_ns))
    before = derived_cache.fingerprint(root, "ns")
    # Same size, same mtime, different bytes: only a content-independent part of
    # the key can save us, and utime cannot restore ctime, so the key must move.
    target.write_text('{"v":222}')
    os.utime(target, ns=(stamp_ns, stamp_ns))
    after = derived_cache.fingerprint(root, "ns")
    assert before != after
    assert target.read_text() == '{"v":222}'
    assert _mtime_ns(target) == stamp_ns


def test_warm_fingerprint_reuses_memoized_digests(tmp_path, monkeypatch):
    root = _root(tmp_path)
    # The freshness guard keeps very fresh (coarse-clock) files unmemoized;
    # zero it to exercise the warm path.
    monkeypatch.setattr(derived_cache, "_DIGEST_MEMO_MIN_AGE_NS", 0)
    real_sha256_file = derived_cache.sha256_file
    calls = 0

    def counting_sha256_file(path):
        nonlocal calls
        calls += 1
        return real_sha256_file(path)

    monkeypatch.setattr(derived_cache, "sha256_file", counting_sha256_file)
    derived_cache.fingerprint(root, "ns")
    assert calls >= 2  # cold pass hashes every small contributing file
    calls = 0
    warm = derived_cache.fingerprint(root, "ns")
    assert derived_cache.fingerprint(root, "ns") == warm
    assert calls == 0  # warm pass re-stats but never re-reads file bytes


def test_store_load_roundtrip_is_byte_for_byte(tmp_path):
    root = _root(tmp_path)
    payload = b'{"assets":[{"asset":"SR"}],"timeframe":"1d"}'
    path = derived_cache.store_bytes(root, "k" * 64, payload)
    assert path.read_bytes() == payload
    assert derived_cache.load_bytes(root, "k" * 64) == payload
    assert derived_cache.load_bytes(root, "0" * 64) is None


def test_corrupt_entry_is_a_miss_not_an_error(tmp_path):
    root = _root(tmp_path)
    cache = derived_cache.cache_dir(root)
    cache.mkdir(parents=True)
    (cache / ("f" * 64 + ".json")).write_bytes(b"{not json")
    assert derived_cache.load_bytes(root, "f" * 64) is None


def test_disabled_cache_bypasses_reads(tmp_path, monkeypatch):
    root = _root(tmp_path)
    monkeypatch.setenv("MOEX_DERIVED_CACHE", "0")
    derived_cache.store_bytes(root, "k" * 64, b"{}")
    assert derived_cache.load_bytes(root, "k" * 64) is None


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


def test_as_str_dict_narrows_json_object_and_keeps_nested_values() -> None:
    value = {"name": "demo", "values": [1, None, {"enabled": True}]}

    assert as_str_dict(value) == value


@pytest.mark.parametrize("value", [None, [], "text", {1: "not a JSON object"}])
def test_as_str_dict_rejects_non_object_or_non_string_keys(value: object) -> None:
    with pytest.raises(JSONBoundaryError, match="string keys"):
        as_str_dict(value, label="payload")


def test_load_object_reads_utf8_and_requires_object_root(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"ключ": [1, "текст"]}, ensure_ascii=False), encoding="utf-8")

    assert load_object(path) == {"ключ": [1, "текст"]}

    path.write_text("[]", encoding="utf-8")
    with pytest.raises(JSONBoundaryError, match="JSON object"):
        load_object(path)


def test_load_object_preserves_the_callers_domain_exception(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(DomainError, match="cannot read settings"):
        load_object(path, label="settings", error=DomainError)


@pytest.mark.parametrize(
    "module",
    [
        "moex_spread_lab.core.cache_metrics",
        "moex_spread_lab.core.hashed_json_cache",
        "moex_spread_lab.core.json_types",
    ],
)
def test_retired_modules_cannot_be_imported(module: str) -> None:
    import importlib

    with pytest.raises(ModuleNotFoundError, match=module.replace(".", r"\.")) as exc:
        importlib.import_module(module)
    assert exc.value.name == module
