"""Fingerprint-keyed derived cache: invalidation semantics and storage."""

from __future__ import annotations

import os
from pathlib import Path

from moex_spread_lab.core import derived_cache


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
