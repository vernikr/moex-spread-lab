"""Isolated P7.2 strict regressions for `data/lake/h1_migration.py` (issue #60)."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from moex_spread_lab.data.lake import data_lake, h1_migration, h1_seal, parquet_store
from moex_spread_lab.data.lake.bar_types import DataLakeError
from moex_spread_lab.data.lake.parquet_store import ParquetBarStore
from moex_spread_lab.data.retention import h1_archive


def _write_h1_series(
    root: Path,
    secid: str,
    closes: list[float],
    *,
    folder: str = "data/w1/candles",
) -> Path:
    path = root / folder / f"{secid}_1h.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    source = {
        "url": f"https://iss.moex.example/{secid}/page-0",
        "received_at": "2026-11-20T10:00:00+00:00",
        "raw_response_sha256": "a" * 64,
    }
    rows = [
        {
            "begin": f"2026-01-01 {10 + idx:02d}:00:00",
            "end": f"2026-01-01 {10 + idx:02d}:59:59",
            "open": close - 0.5,
            "high": close + 0.5,
            "low": close - 1.0,
            "close": close,
            "value": 100.0,
            "volume": 10 + idx,
            "price_proxy": str(close),
            "price_proxy_kind": "candle_close",
            "price_proxy_assumption": "test_proxy",
            "source": source,
        }
        for idx, close in enumerate(closes)
    ]
    payload = {
        "schema_version": "iss-candles.v1",
        "secid": secid,
        "board": "RFUD",
        "interval": 60,
        "timeframe": "1h",
        "from": "2026-01-01",
        "till": "2026-01-02",
        "coverage_status": "available",
        "price_interpretation": "candle_price_proxy_for_medium_term_analysis",
        "rows": rows,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_p72_h1_migration_facade_identity_and_signatures_issue60() -> None:
    assert h1_migration.SCHEMA == "h1-source-plan.v1"
    assert h1_migration.GROUPS == ("candles", "candles-screen")
    assert h1_migration.DataLakeError is data_lake.DataLakeError
    assert h1_migration.ParquetBarStore is data_lake.ParquetBarStore
    assert h1_migration.ParquetBarStore is parquet_store.ParquetBarStore
    assert h1_migration._atomic_write_json is data_lake._atomic_write_json
    assert h1_migration._read_legacy_series is data_lake._read_legacy_series
    assert h1_seal.build_h1_plan is h1_migration.build_h1_plan
    assert h1_seal.verify_h1_stage is h1_migration.verify_h1_stage
    assert h1_archive.build_h1_plan is h1_migration.build_h1_plan

    assert list(inspect.signature(h1_migration.build_h1_plan).parameters) == ["root"]
    assert list(inspect.signature(h1_migration.write_h1_plan).parameters) == ["root", "path"]
    assert list(inspect.signature(h1_migration.stage_h1).parameters) == [
        "root",
        "plan_path",
        "output",
    ]
    assert list(inspect.signature(h1_migration.verify_h1_stage).parameters) == [
        "root",
        "plan_path",
        "output",
    ]


def test_p72_parquet_store_read_manifest_identity_and_legacy_patch_hook_issue60(
    tmp_path: Path,
) -> None:
    legacy = _write_h1_series(tmp_path, "CNYRUBF", [10.0, 11.0, 12.0])
    empty_store = ParquetBarStore(tmp_path, tmp_path / "empty-lake")
    assert empty_store.read_manifest() == {
        "schema_version": "market-data-lake.v1",
        "datasets": [],
    }

    store = ParquetBarStore(tmp_path)
    store.import_legacy(legacy)
    via_private = store._read_manifest()
    via_public = store.read_manifest()
    assert via_public is via_private
    assert len(via_public["datasets"]) == 1

    sentinel: dict[str, Any] = {"schema_version": "market-data-lake.v1", "datasets": []}
    with patch.object(ParquetBarStore, "_read_manifest", return_value=sentinel) as mocked:
        assert store.read_manifest() is sentinel
        mocked.assert_called_once_with()


def test_p72_h1_migration_payload_contract_and_patch_points_issue60(tmp_path: Path) -> None:
    _write_h1_series(tmp_path, "CNYRUBF", [10.0, 11.0, 12.0], folder="data/w1/candles")
    _write_h1_series(tmp_path, "CNYRUBF", [20.0, 21.0, 22.0], folder="data/w1/candles-screen")
    plan_path = tmp_path / "plan.json"
    output = tmp_path / "data-lake/candidates/h1"

    plan = h1_migration.write_h1_plan(tmp_path, plan_path)
    assert plan == h1_migration.build_h1_plan(tmp_path)
    assert set(plan) == {
        "schema_version",
        "root",
        "files",
        "screen_selection",
        "source_fingerprint",
        "source_count",
        "selected_count",
        "logical_bytes",
    }
    assert plan["source_count"] == 2
    assert plan["selected_count"] == 1
    assert plan["screen_selection"] == {"CNYRUBF": "data/w1/candles-screen/CNYRUBF_1h.json"}

    stage_report = h1_migration.stage_h1(tmp_path, plan_path, output)
    assert stage_report["status"] == "verified_candidate"
    assert stage_report["runtime_activated"] is False
    assert stage_report["source_deletion_allowed"] is False
    assert len(stage_report["datasets"]) == 2

    verified = h1_migration.verify_h1_stage(tmp_path, plan_path, output)
    assert verified == {
        "status": "verified_candidate",
        "source_count": 2,
        "selected_count": 1,
        "source_fingerprint": plan["source_fingerprint"],
        "runtime_activated": False,
        "source_deletion_allowed": False,
    }

    # Patching legacy `_read_manifest` on ParquetBarStore still intercepts verification.
    empty_manifest: dict[str, Any] = {"schema_version": "market-data-lake.v1", "datasets": []}
    with (
        patch.object(ParquetBarStore, "_read_manifest", return_value=empty_manifest),
        pytest.raises(DataLakeError, match="candidate lake membership differs from plan"),
    ):
        h1_migration.verify_h1_stage(tmp_path, plan_path, output)

    # Patching `h1_migration._atomic_write_json` remains effective during staging.
    calls: list[Path] = []
    real_write = h1_migration._atomic_write_json

    def _recording_write(path: Path, value: dict[str, Any]) -> None:
        calls.append(path)
        real_write(path, value)

    second_output = tmp_path / "data-lake/candidates/h1-patched"
    with patch.object(h1_migration, "_atomic_write_json", side_effect=_recording_write):
        h1_migration.stage_h1(tmp_path, plan_path, second_output)
    assert len(calls) == 4
    assert all(path == second_output / "h1-stage.json" for path in calls)
