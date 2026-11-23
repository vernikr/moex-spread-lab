"""Regression coverage for the P7.2 strict slice of data/retention/inventory_policy.py (#71).

The slice is behavior-preserving: path classification keeps returning the same
policy rows, JSON/JSONL metadata inspection keeps its keys and value shapes, the
re-export facade in ``data_inventory`` keeps pointing at these helpers and the
``run_id``/payload contracts are untouched.
"""

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from moex_spread_lab.data.retention import data_inventory, inventory_policy
from moex_spread_lab.data.retention.inventory_policy import (
    DATA_INVENTORY_SCHEMA,
    _inspect_file,
    _json_time_range,
    _path_timeframe,
    _policy,
    _resolve_payload_row_count,
)


class InventoryPolicyContractTests(unittest.TestCase):
    def test_facade_reexports_stay_bound_to_the_same_helpers(self) -> None:
        self.assertEqual(data_inventory._policy, inventory_policy._policy)
        self.assertEqual(data_inventory._inspect_file, inventory_policy._inspect_file)
        self.assertEqual(data_inventory._path_timeframe, inventory_policy._path_timeframe)
        self.assertEqual(data_inventory.DATA_INVENTORY_SCHEMA, DATA_INVENTORY_SCHEMA)
        self.assertEqual(
            sorted(inventory_policy.__all__),
            [
                "DATA_INVENTORY_SCHEMA",
                "_Policy",
                "_inspect_file",
                "_path_timeframe",
                "_policy",
            ],
        )

    def test_policy_table_classifies_each_registered_path_family(self) -> None:
        exact = _policy("w1/current-intake.json", {})
        self.assertEqual(
            (exact.dataset, exact.classification, exact.retention),
            (
                "w1/current-intake",
                "active_input",
                "keep",
            ),
        )
        self.assertEqual(
            _policy("w1/historical-contracts.json", {}).dataset, "w1/historical-contracts"
        )

        prefix = _policy("w1/candles-cache/CNYRUBF/60/page-000000.json", {})
        self.assertEqual(
            (prefix.classification, prefix.retention),
            (
                "rebuildable_cache",
                "ttl_or_rebuild",
            ),
        )

        screened = _policy("w1/candles-screen/CNYRUBF_1h.json", {})
        self.assertEqual(screened.dataset, "w1/candles-screen")

        calendar = _policy("spread-history-cache/FUTOI.json", {})
        self.assertEqual(calendar.retention, "keep_while_calendar_research_active")

        funding = _policy("carry-history-cache/FUTOI.json", {})
        self.assertEqual(funding.dataset, "carry-history-cache")
        self.assertEqual(funding.retention, "keep_while_funding_research_active")

        benchmark = _policy("benchmark-cache/spread.json", {})
        self.assertEqual(benchmark.dataset, "benchmark-cache")
        self.assertIn("LQDT benchmark", benchmark.consumers)

    def test_policy_table_marks_active_timeframes_and_unknown_paths(self) -> None:
        hourly = _policy("w1/candles/CNYRUBF_1h.json", {})
        self.assertEqual((hourly.dataset, hourly.classification), ("w1/candles/1h", "active_input"))

        minute = _policy("w1/candles/CNYRUBF_10m.json", {})
        self.assertEqual(
            (minute.dataset, minute.classification, minute.retention),
            ("w1/candles/10m", "archive_candidate", "pin_or_archive"),
        )

        runtime = _policy("analytics/screener-runs.jsonl", {})
        self.assertEqual(
            (runtime.dataset, runtime.classification),
            (
                "analytics/runtime",
                "derived_runtime",
            ),
        )

        derived = _policy("analytics/cny-diagnostic.json", {})
        self.assertEqual(derived.classification, "derived_archive_candidate")

        unclassified = _policy("other.bin", {})
        self.assertEqual(
            (unclassified.dataset, unclassified.classification, unclassified.retention),
            ("root", "unclassified", "review"),
        )
        self.assertEqual(unclassified.consumers, ())

    def test_timeframe_resolution_prefers_metadata_over_file_suffix(self) -> None:
        self.assertEqual(_path_timeframe("w1/candles/CNYRUBF_10m.json", {"timeframe": "1d"}), "1d")
        self.assertEqual(_path_timeframe("w1/candles/CNYRUBF_10m.json", {}), "10m")
        self.assertIsNone(_path_timeframe("w1/candles/CNYRUBF.json", {}))
        self.assertIsNone(_path_timeframe("w1/candles/CNYRUBF.json", {"timeframe": ""}))

    def test_time_range_reads_declared_bounds_then_row_timestamps(self) -> None:
        self.assertEqual(
            _json_time_range({"from": "2026-01-01", "till": "2026-01-02"}),
            {"from": "2026-01-01", "till": "2026-01-02"},
        )
        self.assertEqual(_json_time_range({"from": "2026-01-01"}), {"from": "2026-01-01"})
        self.assertEqual(
            _json_time_range(
                {
                    "rows": [
                        {"begin": "2026-01-02"},
                        {"timestamp": "2026-01-01"},
                        {"date": ""},
                        {"begin": "2026-01-03"},
                        "not-a-row",
                    ]
                }
            ),
            {"from": "2026-01-01", "till": "2026-01-03"},
        )
        self.assertEqual(
            _json_time_range({"daily": [{"date": "2026-02-01"}]}),
            {"from": "2026-02-01", "till": "2026-02-01"},
        )
        self.assertIsNone(_json_time_range({"rows": []}))
        self.assertIsNone(_json_time_range({}))

    def test_row_count_prefers_explicit_value_and_falls_back_to_list_length(self) -> None:
        self.assertEqual(_resolve_payload_row_count({"row_count": 7, "rows": [1, 2]}), 7)
        self.assertIsNone(_resolve_payload_row_count({"row_count": -1, "rows": [1, 2]}))
        self.assertEqual(_resolve_payload_row_count({"trades": [1, 2, 3]}), 3)
        self.assertIsNone(_resolve_payload_row_count({"rows": "nope"}))
        self.assertIsNone(_resolve_payload_row_count({}))

    def test_json_inspection_keeps_metadata_keys_and_value_shapes(self) -> None:
        payload: dict[str, Any] = {
            "schema_version": "iss-candles.v1",
            "timeframe": "1h",
            "rows": [{"begin": "2026-01-01"}, {"begin": "2026-01-02"}],
            "source": {"iss": True, "page": 1},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candles.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(
                _inspect_file(path),
                {
                    "schema_version": "iss-candles.v1",
                    "timeframe": "1h",
                    "row_count": 2,
                    "time_range": {"from": "2026-01-01", "till": "2026-01-02"},
                    "source_metadata_keys": ["iss", "page"],
                },
            )

    def test_json_inspection_reports_non_dict_roots_and_broken_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            list_root = root / "list.json"
            list_root.write_text("[1, 2]", encoding="utf-8")
            self.assertEqual(_inspect_file(list_root), {"json_root": "list"})

            broken = root / "broken.json"
            broken.write_text("{", encoding="utf-8")
            self.assertEqual(_inspect_file(broken), {"inspection_error": "JSONDecodeError"})

            binary = root / "binary.json"
            binary.write_bytes(b"\x80\x81")
            self.assertEqual(_inspect_file(binary), {"inspection_error": "UnicodeDecodeError"})

            missing = root / "missing.json"
            self.assertEqual(_inspect_file(missing), {"inspection_error": "FileNotFoundError"})

    def test_jsonl_inspection_counts_non_empty_records_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jsonl = root / "runs.jsonl"
            jsonl.write_text('{"a": 1}\n\n{"b": 2}\n', encoding="utf-8")
            self.assertEqual(_inspect_file(jsonl), {"record_count": 2})

            empty = root / "empty.jsonl"
            empty.write_text("", encoding="utf-8")
            self.assertEqual(_inspect_file(empty), {"record_count": 0})

    def test_other_suffixes_are_not_inspected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.bin"
            path.write_bytes(b"\x00")
            self.assertEqual(_inspect_file(path), {})


if __name__ == "__main__":
    unittest.main()
