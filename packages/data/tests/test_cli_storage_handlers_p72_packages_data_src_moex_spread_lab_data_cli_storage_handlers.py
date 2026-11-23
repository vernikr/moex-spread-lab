"""Isolated regression coverage for the strict typing pass (Issue #52).

``cli_storage_handlers`` now narrows producer payloads at the CLI boundary
through module-level protocols instead of passing ``Any`` around. These tests
pin the observable behaviour of the affected handlers: normalized payload
shape, exit codes, contract keys and the published report.
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest

from moex_spread_lab.data.cli_storage_handlers import (
    STORAGE_DISPATCH,
    _run_data_inventory,
    _run_storage_policy,
)


def _capture(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert isinstance(payload, dict)
    return payload


def test_storage_dispatch_contract_is_unchanged() -> None:
    assert sorted(STORAGE_DISPATCH) == [
        "archive-catalog",
        "data-archive",
        "data-archive-candidates",
        "data-compact",
        "data-gc",
        "data-h1-archive",
        "data-h1-seal",
        "data-h1-stage",
        "data-import-candles",
        "data-inventory",
        "data-pin-raw",
        "data-refresh",
        "data-verify",
        "storage-budget",
    ]


def test_storage_budget_returns_zero_without_budget_breach(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = Namespace(
        command="storage-budget",
        root=tmp_path,
        archives_root=None,
        max_allocated_bytes=10_000_000_000,
        output=None,
    )
    assert _run_storage_policy(args) == 0
    payload = _capture(capsys)
    assert payload["schema_version"] == "storage-budget.v1"
    assert payload["status"] == "reported"
    assert payload["deletion_authorized"] is False
    assert payload["total"] == {"file_count": 0, "logical_bytes": 0, "allocated_bytes": 0}
    assert "created_at" in payload


def test_storage_budget_exit_two_when_over_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "data" / "lake").mkdir(parents=True)
    (tmp_path / "data" / "lake" / "bars.parquet").write_bytes(b"x" * 4096)
    args = Namespace(
        command="storage-budget",
        root=tmp_path,
        archives_root=None,
        max_allocated_bytes=0,
        output=None,
    )
    assert _run_storage_policy(args) == 2
    payload = _capture(capsys)
    assert payload["status"] == "over_budget"
    assert payload["max_allocated_bytes"] == 0


def test_storage_policy_error_is_wrapped_as_w0_error(tmp_path: Path) -> None:
    from moex_spread_lab.core.w0 import W0Error

    args = Namespace(
        command="storage-budget",
        root=tmp_path,
        archives_root=None,
        max_allocated_bytes=-1,
        output=None,
    )
    with pytest.raises(W0Error, match="storage policy report failed"):
        _run_storage_policy(args)


def test_data_inventory_json_payload_keeps_contract_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = Namespace(root=tmp_path, hashes=False, fast=True, format="json", output=None)
    assert _run_data_inventory(args) == 0
    payload = _capture(capsys)
    assert payload["schema_version"] == "data-inventory.v1"
    assert payload["datasets"] == []
    assert payload["summary"]["file_count"] == 0


def test_data_inventory_summary_format_hides_file_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = Namespace(root=tmp_path, hashes=False, fast=True, format="text", output=None)
    assert _run_data_inventory(args) == 0
    payload = _capture(capsys)
    assert payload["status"] == "ok"
    assert "files" not in payload
    assert "datasets" not in payload
    assert payload["output"] is None


def test_data_inventory_writes_output_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "inventory.json"
    args = Namespace(root=tmp_path, hashes=False, fast=True, format="text", output=output)
    assert _run_data_inventory(args) == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["schema_version"] == "data-inventory.v1"
    assert _capture(capsys)["output"] == str(output)
