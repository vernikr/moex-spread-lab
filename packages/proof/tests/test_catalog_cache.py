"""S4c: the derived catalog cache is byte-for-byte equivalent to a fresh build."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from moex_spread_lab.core.w0 import canonical_bytes
from moex_spread_lab.proof.proof_pool import get_worker_pool
from moex_spread_lab.proof.service import ProofService
from proof_fixtures import two_calendar_windows


def _catalog_bytes(service: ProofService) -> tuple[dict, bytes]:
    payload = service.catalog()
    return payload, canonical_bytes(payload)


def _fresh_service(root: Path) -> ProofService:
    return ProofService(root, pool_factory=get_worker_pool)


def _same_size_flip(raw: bytes) -> bytes:
    digit = next(i for i, byte in enumerate(raw) if 49 <= byte <= 56)
    return raw[:digit] + bytes([raw[digit] + 1]) + raw[digit + 1 :]


class DerivedCatalogCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("MOEX_DERIVED_CACHE", None)
        self.addCleanup(os.environ.pop, "MOEX_DERIVED_CACHE", None)

    def test_cached_response_is_byte_for_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            two_calendar_windows(root)
            first_service = _fresh_service(root)
            first, first_bytes = _catalog_bytes(first_service)
            first_service.close()
            # A restart drops every in-memory structure; the derived cache is the
            # only thing allowed to survive, and it must reproduce the same bytes.
            second_service = _fresh_service(root)
            second, second_bytes = _catalog_bytes(second_service)
            second_service.close()
            self.assertEqual(first, second)
            self.assertEqual(first_bytes, second_bytes)
            cache_files = list((root / "data" / "analytics" / "derived-cache").glob("*.json"))
            self.assertEqual(len(cache_files), 1)
            self.assertEqual(cache_files[0].read_bytes(), first_bytes)

    def test_cache_hit_skips_the_build_entirely(self) -> None:
        from moex_spread_lab.proof import catalog_payload

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            two_calendar_windows(root)
            service = _fresh_service(root)
            try:
                calls = []
                original_build = catalog_payload._build_catalog

                def counting_build(*args):
                    calls.append(args)
                    return original_build(*args)

                catalog_payload._build_catalog = counting_build
                try:
                    service.catalog()
                    service.catalog()
                finally:
                    catalog_payload._build_catalog = original_build
                self.assertEqual(len(calls), 1)
            finally:
                service.close()

    def test_same_size_same_mtime_tick_edit_invalidates(self) -> None:
        from moex_spread_lab.proof import catalog_payload

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            two_calendar_windows(root)
            service = _fresh_service(root)
            try:
                service.catalog()
                target = next((root / "data" / "spread-history-cache").glob("*.json"))
                raw = target.read_bytes()
                size = len(raw)
                stamp_ns = 1_800_000_000_000_000_000  # frozen inside a single 1-second tick
                target.write_bytes(_same_size_flip(raw))
                os.utime(target, ns=(stamp_ns, stamp_ns))
                self.assertEqual(target.stat().st_size, size)
                self.assertEqual(target.stat().st_mtime_ns, stamp_ns)

                calls = []
                original_build = catalog_payload._build_catalog

                def counting_build(*args):
                    calls.append(args)
                    return original_build(*args)

                catalog_payload._build_catalog = counting_build
                try:
                    service.catalog()
                finally:
                    catalog_payload._build_catalog = original_build
                self.assertEqual(len(calls), 1, "stale cache served after a content edit")
            finally:
                service.close()

    def test_disabled_cache_never_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            two_calendar_windows(root)
            os.environ["MOEX_DERIVED_CACHE"] = "0"
            service = _fresh_service(root)
            try:
                service.catalog()
            finally:
                service.close()
            self.assertFalse((root / "data" / "analytics" / "derived-cache").exists())

    def test_cached_catalog_warm_path_is_fast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            two_calendar_windows(root)
            service = _fresh_service(root)
            try:
                service.catalog()
                started = time.perf_counter()
                for _ in range(5):
                    service.catalog()
                elapsed = time.perf_counter() - started
            finally:
                service.close()
            self.assertLess(elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()
