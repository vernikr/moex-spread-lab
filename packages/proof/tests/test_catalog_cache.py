"""S4c: the derived catalog cache is byte-for-byte equivalent to a fresh build."""

import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from moex_spread_lab.core.w0 import canonical_bytes
from moex_spread_lab.proof.proof_pool import get_worker_pool
from moex_spread_lab.proof.service import ProofService
from proof_fixtures import two_calendar_windows


@pytest.fixture(autouse=True)
def _clean_derived_cache_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("MOEX_DERIVED_CACHE", raising=False)
    yield


def _catalog_bytes(service: ProofService) -> tuple[dict, bytes]:
    payload = service.catalog()
    return payload, canonical_bytes(payload)


def _fresh_service(root: Path) -> ProofService:
    return ProofService(root, pool_factory=get_worker_pool)


def _same_size_flip(raw: bytes) -> bytes:
    digit = next(i for i, byte in enumerate(raw) if 49 <= byte <= 56)
    return raw[:digit] + bytes([raw[digit] + 1]) + raw[digit + 1 :]


def _count_build_catalog(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Count ``payload_tools._build_catalog`` calls while keeping its behaviour."""
    from moex_spread_lab.proof import payload_tools

    calls: list[tuple] = []
    original_build = payload_tools._build_catalog

    def counting(*args):
        calls.append(args)
        return original_build(*args)

    monkeypatch.setattr(payload_tools, "_build_catalog", counting)
    return calls


def test_catalog_remains_valid_after_years_without_file_edits(tmp_path, monkeypatch):
    from moex_spread_lab.data.iss import iss_http

    two_calendar_windows(tmp_path)
    clock = [datetime.now(UTC) + timedelta(seconds=1)]
    monkeypatch.setattr(iss_http, "_cache_now", lambda: clock[0])
    service = _fresh_service(tmp_path)
    calls = _count_build_catalog(monkeypatch)
    try:
        before = service.catalog()
        assert service.catalog() == before
        assert len(calls) == 1
        clock[0] += timedelta(days=730)
        fresh_service = _fresh_service(tmp_path)
        try:
            after_restart = fresh_service.catalog()
            assert len(calls) == 1
            after = service.catalog()
            assert after == after_restart
            assert len(calls) == 1
            monkeypatch.setenv("MOEX_DERIVED_CACHE", "0")
            assert after == service.catalog()
        finally:
            fresh_service.close()
        assert after == before
    finally:
        service.close()


def test_cached_response_is_byte_for_byte_identical(tmp_path: Path):
    two_calendar_windows(tmp_path)
    first_service = _fresh_service(tmp_path)
    first, first_bytes = _catalog_bytes(first_service)
    first_service.close()
    # A restart drops every in-memory structure; the derived cache is the
    # only thing allowed to survive, and it must reproduce the same bytes.
    second_service = _fresh_service(tmp_path)
    second, second_bytes = _catalog_bytes(second_service)
    second_service.close()
    assert first == second
    assert first_bytes == second_bytes
    cache_files = list((tmp_path / "data" / "analytics" / "derived-cache").glob("*.json"))
    assert len(cache_files) == 1
    assert cache_files[0].read_bytes() == first_bytes


def test_cache_hit_skips_the_build_entirely(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    two_calendar_windows(tmp_path)
    service = _fresh_service(tmp_path)
    try:
        calls = _count_build_catalog(monkeypatch)
        service.catalog()
        service.catalog()
    finally:
        service.close()
    assert len(calls) == 1


def test_same_size_same_mtime_tick_edit_invalidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    two_calendar_windows(tmp_path)
    service = _fresh_service(tmp_path)
    try:
        service.catalog()
        target = next((tmp_path / "data" / "spread-history-cache").glob("*.json"))
        raw = target.read_bytes()
        size = len(raw)
        stamp_ns = 1_800_000_000_000_000_000  # frozen inside a single 1-second tick
        target.write_bytes(_same_size_flip(raw))
        os.utime(target, ns=(stamp_ns, stamp_ns))
        assert target.stat().st_size == size
        assert target.stat().st_mtime_ns == stamp_ns

        calls = _count_build_catalog(monkeypatch)
        service.catalog()
        assert len(calls) == 1, "stale cache served after a content edit"
    finally:
        service.close()


def test_disabled_cache_never_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    two_calendar_windows(tmp_path)
    monkeypatch.setenv("MOEX_DERIVED_CACHE", "0")
    service = _fresh_service(tmp_path)
    try:
        service.catalog()
    finally:
        service.close()
    assert not (tmp_path / "data" / "analytics" / "derived-cache").exists()


def test_cached_catalog_warm_path_is_fast(tmp_path: Path):
    two_calendar_windows(tmp_path)
    service = _fresh_service(tmp_path)
    try:
        service.catalog()
        started = time.perf_counter()
        for _ in range(5):
            service.catalog()
        elapsed = time.perf_counter() - started
    finally:
        service.close()
    assert elapsed < 2.0
