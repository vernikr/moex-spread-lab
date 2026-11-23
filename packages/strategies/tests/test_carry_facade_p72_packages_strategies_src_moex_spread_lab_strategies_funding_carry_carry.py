"""Regression coverage for the carry facade (P7.2 leaf #103).

``carry.py`` is a pure facade: it re-exports the historical carry entry points
from ``strategy_costs``, ``strategy_runner``, ``study``, the ISS catalog and the
``core`` contracts loader so downstream patch points and the backward
root compatibility shim keep working unchanged. The leaf
only clears ``reportPrivateUsage`` noise inside this file, so the re-exported
surface must stay identical: same names, same objects, same ``__all__`` order,
and the same live benchmark cache. Workspace tests may not import the root
compatibility shim (architecture DAG), so the facade surface is pinned here
module by module.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any


def _benchmark_payload(dates: list[str]) -> dict[str, Any]:
    return {
        "schema_version": "lqdt-lineage.v1",
        "rows": [
            {
                "date": date,
                "price": 1.0,
                "source_kind": "observed",
                "source_ticker": "LQDT",
            }
            for date in dates
        ],
    }


class CarryFacadeTests(unittest.TestCase):
    def setUp(self) -> None:
        from moex_spread_lab.core import w0
        from moex_spread_lab.data.iss import catalog as iss_catalog
        from moex_spread_lab.strategies.funding_carry import (
            carry,
            strategy_costs,
            strategy_runner,
            study,
        )

        self.carry = carry
        self.origins: dict[str, ModuleType] = {
            "CARRY_SCHEMA": study,
            "FUNDING_REGIMES": study,
            "STRATEGY_SCHEMA": study,
            "CarryError": study,
            "CarryStrategyError": study,
            "build_carry_study": study,
            "fetch_history": study,
            "_funding_regime": study,
            "_normalize_history": study,
            "_round": study,
            "HISTORY_FIELDS": iss_catalog,
            "HISTORY_SCHEMA": iss_catalog,
            "IssHistoryError": iss_catalog,
            "_history_url": iss_catalog,
            "build_carry_strategy": strategy_runner,
            "load_contracts": w0,
            "_BENCHMARK_PARSED_CACHE": strategy_costs,
            "_benchmark_rows": strategy_costs,
            "_transaction_cost_profile": strategy_costs,
        }

    def test_all_surface_and_order_are_unchanged(self):
        self.assertEqual(
            self.carry.__all__,
            [
                "CARRY_SCHEMA",
                "FUNDING_REGIMES",
                "HISTORY_FIELDS",
                "HISTORY_SCHEMA",
                "STRATEGY_SCHEMA",
                "_BENCHMARK_PARSED_CACHE",
                "CarryError",
                "CarryStrategyError",
                "IssHistoryError",
                "_benchmark_rows",
                "_funding_regime",
                "_history_url",
                "_normalize_history",
                "_round",
                "_transaction_cost_profile",
                "build_carry_strategy",
                "build_carry_study",
                "fetch_history",
                "load_contracts",
            ],
        )

    def test_every_export_keeps_origin_identity(self):
        for name, origin in self.origins.items():
            with self.subTest(name=name):
                self.assertTrue(hasattr(self.carry, name))
                self.assertIs(getattr(self.carry, name), getattr(origin, name))

    def test_private_facade_behaviour_is_unchanged_offline(self):
        carry = self.carry
        self.assertEqual(carry._round(1.234567891), 1.23456789)
        url = carry._history_url("CRZ6", "2026-01-01", "2026-01-02", 0)
        self.assertIn("securities/CRZ6.json", url)
        self.assertIn("from=2026-01-01", url)
        self.assertIn("till=2026-01-02", url)
        self.assertEqual(
            carry._funding_regime("2023-08-22", carry.FUNDING_REGIMES),
            carry.FUNDING_REGIMES[0],
        )
        self.assertIsNone(carry._funding_regime("2023-08-22", ()))

    def test_benchmark_rows_returns_the_rows_the_facade_publishes(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = Path(tmp) / "lqdt-lineage.json"
        path.write_text(json.dumps(_benchmark_payload(["2026-01-05"])), encoding="utf-8")
        rows, digest = self.carry._benchmark_rows(path)
        self.assertEqual(rows["2026-01-05"]["price"], 1.0)
        self.assertEqual(rows["2026-01-05"]["source_kind"], "observed")
        self.assertEqual(len(digest), 64)
        # The facade publishes the live cost cache: the parsed rows must land in it.
        resolved = path.resolve()
        stat = resolved.stat()
        key = (str(resolved), stat.st_mtime_ns, stat.st_size)
        cached = self.carry._BENCHMARK_PARSED_CACHE.get(key)
        self.assertIsNotNone(cached)
        self.assertEqual(cached, (rows, digest))


if __name__ == "__main__":
    unittest.main()
