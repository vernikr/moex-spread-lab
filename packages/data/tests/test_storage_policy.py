import json

import pytest

from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.data.retention.storage_policy import (
    archive_catalog,
    publish_report,
    storage_budget,
)


def test_budget_empty_read_only(tmp_path):
    result = storage_budget(tmp_path)
    assert result["total"]["file_count"] == 0
    assert result["ttl_dry_run"]["raw_default_lake"]["status"] == "dry_run"
    assert not list(tmp_path.iterdir())


def test_budget_partition_and_limit(tmp_path):
    paths = [
        "data/w1/candles/X_1h.json",
        "data/analytics/runs/r/summary.json",
        "data-lake/raw/file.bin",
        "data-lake/candidates/h1/bars.parquet",
        "archives/evidence.txt",
    ]
    for name in paths:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"1234")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = storage_budget(tmp_path, tmp_path / "archives", 0)
    assert result["status"] == "over_budget"
    assert result["total"]["logical_bytes"] == 20
    assert set(result["classes"]) == {
        "active_input",
        "run_artifacts",
        "raw",
        "retained_lake",
        "archives_and_evidence",
    }
    assert all(p.read_bytes() == b for p, b in before.items())


def test_budget_rejects_links_overlap_negative(tmp_path):
    with pytest.raises(W0Error):
        storage_budget(tmp_path, tmp_path)
    with pytest.raises(W0Error):
        storage_budget(tmp_path, max_allocated_bytes=-1)
    (tmp_path / "data").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(W0Error):
        storage_budget(tmp_path)


def test_retired_catalog_fails_closed(tmp_path):
    with pytest.raises(W0Error, match="retired"):
        archive_catalog([tmp_path / "archive.tar.zst"], "2099-01-01")
    assert not list(tmp_path.iterdir())


def test_report_no_clobber(tmp_path):
    output = tmp_path / "report.json"
    publish_report(output, {"status": "reported"})
    with pytest.raises(FileExistsError):
        publish_report(output, {})
    assert json.loads(output.read_text()) == {"status": "reported"}
