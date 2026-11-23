"""Isolated regression coverage for the P7.2 retention inventory slice.

The strict typing pass over ``data_inventory`` is behaviour preserving: the
public builder, the inventory payload and the private facade hooks that other
retention modules import must stay identical. This module pins that contract and
the merged time-range aggregation edge cases touched by the pass.

The hooks are declared surface of ``inventory_policy`` through its ``__all__``
(landed with the policy helper slice); the assertions below keep that
declaration and the facade identity from drifting.
"""

import json
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.data.retention import inventory_policy
from moex_spread_lab.data.retention.data_inventory import (
    DATA_INVENTORY_SCHEMA,
    _inspect_file,
    _path_timeframe,
    _Policy,
    _policy,
    build_data_inventory,
)

_FACADE_HOOKS = ("_Policy", "_inspect_file", "_path_timeframe", "_policy")


class DataInventoryFacadeTests(unittest.TestCase):
    def test_inventory_policy_declares_reexported_facade_hooks(self):
        self.assertIn("DATA_INVENTORY_SCHEMA", inventory_policy.__all__)
        for hook in _FACADE_HOOKS:
            self.assertIn(hook, inventory_policy.__all__)

    def test_facade_hooks_stay_importable_and_consistent(self):
        self.assertIs(_Policy, inventory_policy._Policy)
        self.assertIs(_inspect_file, inventory_policy._inspect_file)
        self.assertIs(_path_timeframe, inventory_policy._path_timeframe)
        self.assertIs(_policy, inventory_policy._policy)
        self.assertEqual(DATA_INVENTORY_SCHEMA, "data-inventory.v1")
        self.assertEqual(_path_timeframe("w1/candles/CNYRUBF_1h.json", {}), "1h")

    def test_schema_constant_is_exported_from_data_inventory(self):
        from moex_spread_lab.data.retention import data_inventory

        self.assertEqual(data_inventory.DATA_INVENTORY_SCHEMA, DATA_INVENTORY_SCHEMA)
        for hook in _FACADE_HOOKS:
            self.assertIn(hook, data_inventory.__all__)


class DataInventoryTimeRangeTests(unittest.TestCase):
    def _inventory_for(self, root: Path, relative: str, payload: dict) -> dict:
        path = root / "data" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return build_data_inventory(root)

    def test_open_ended_windows_are_merged_from_available_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._inventory_for(
                root,
                "w1/candles/CNYRUBF_1h.json",
                {
                    "schema_version": "iss-candles.v1",
                    "timeframe": "1h",
                    "from": "2026-01-01",
                    "rows": [],
                },
            )
            self._inventory_for(
                root,
                "w1/candles/USDRUBF_1h.json",
                {
                    "schema_version": "iss-candles.v1",
                    "timeframe": "1h",
                    "till": "2026-01-05",
                    "rows": [],
                },
            )
            result = build_data_inventory(root)
        dataset = result["datasets"][0]
        self.assertEqual(dataset["dataset"], "w1/candles/1h")
        self.assertEqual(dataset["file_count"], 2)
        self.assertEqual(dataset["time_range"], {"from": "2026-01-01", "till": "2026-01-05"})

    def test_malformed_time_range_is_ignored_without_leaking_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._inventory_for(
                root,
                "w1/candles/CNYRUBF_1h.json",
                {
                    "schema_version": "iss-candles.v1",
                    "timeframe": "1h",
                    "time_range": ["2026-01-01", "2026-01-02"],
                    "rows": [],
                },
            )
            result = build_data_inventory(root)
        dataset = result["datasets"][0]
        self.assertNotIn("time_range", dataset)
        self.assertEqual(dataset["schema_versions"], ["iss-candles.v1"])


if __name__ == "__main__":
    unittest.main()
