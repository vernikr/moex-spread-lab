import io
import json
import tarfile
from pathlib import Path

import pytest
import zstandard as zstd

from moex_spread_lab.core.w0 import W0Error, canonical_bytes, sha256_bytes, sha256_file
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


def _write_run_artifact(
    artifact_dir: Path,
    *,
    expires_at: str,
    pinned: bool,
    detail_status: str = "available",
) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for name in ("daily", "trades"):
        if detail_status == "available":
            (artifact_dir / f"{name}.parquet").write_bytes(b"PAR1-detail")
    manifest = {
        "schema_version": "research-run-artifact.v1",
        "artifact_id": f"run-{artifact_dir.name}",
        "retention": {
            "detail_status": detail_status,
            "pinned": pinned,
            "expires_at": expires_at,
        },
        "details": {
            "daily": {"file": "daily.parquet"},
            "trades": {"file": "trades.parquet"},
        },
    }
    (artifact_dir / "manifest.json").write_bytes(canonical_bytes(manifest))


def test_budget_run_details_ttl_classifies_expired_retained_and_pinned(tmp_path):
    runs_root = tmp_path / "data/analytics/runs"
    _write_run_artifact(
        runs_root / "expired",
        expires_at="2020-01-01T00:00:00+00:00",
        pinned=False,
    )
    _write_run_artifact(
        runs_root / "retained",
        expires_at="2099-01-01T00:00:00+00:00",
        pinned=False,
    )
    _write_run_artifact(
        runs_root / "pinned",
        expires_at="2020-01-01T00:00:00+00:00",
        pinned=True,
    )
    _write_run_artifact(
        runs_root / "pruned",
        expires_at="2020-01-01T00:00:00+00:00",
        pinned=False,
        detail_status="expired",
    )

    budget = storage_budget(tmp_path)
    assert budget["ttl_dry_run"]["run_details"] == {
        "status": "dry_run",
        "expired_artifact_count": 1,
        "retained_artifact_count": 2,
        "planned_detail_files": ["expired/daily.parquet", "expired/trades.parquet"],
    }


def test_budget_rejects_links_overlap_negative(tmp_path):
    with pytest.raises(W0Error):
        storage_budget(tmp_path, tmp_path)
    with pytest.raises(W0Error):
        storage_budget(tmp_path, max_allocated_bytes=-1)
    (tmp_path / "data").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(W0Error):
        storage_budget(tmp_path)


def make_archive(tmp_path, member="x.json"):
    archive = tmp_path / "test.tar.zst"
    manifest = {
        "files": [{"path": member, "logical_bytes": 2, "sha256": sha256_bytes(b"{}")}],
        "git_commit": "abc123",
    }
    with archive.open("wb") as raw, zstd.ZstdCompressor().stream_writer(raw) as stream:
        with tarfile.open(fileobj=stream, mode="w|") as tar:
            for name, body in [
                ("manifest.json", canonical_bytes(manifest)),
                ("data/" + member, b"{}"),
            ]:
                info = tarfile.TarInfo(name)
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
    receipt = Path(str(archive) + ".verified.json")
    receipt.write_text(
        json.dumps(
            {"status": "verified", "archive_sha256": sha256_file(archive), "manifest": manifest}
        )
    )
    return archive, receipt


def test_catalog_verified_no_expiry_and_no_clobber(tmp_path):
    archive, receipt = make_archive(tmp_path)
    result = archive_catalog([archive], "2099-01-01")
    assert result["entries"][0]["verification"] == "full_stream_member_hashes"
    assert result["entries"][0]["automatic_expiry"] is False
    output = tmp_path / "catalog.json"
    publish_report(output, result)
    with pytest.raises(FileExistsError):
        publish_report(output, {})
    assert json.loads(output.read_text()) == result


@pytest.mark.parametrize("case", ["corrupt", "duplicate", "missing", "receipt", "symlink", "past"])
def test_catalog_fail_closed(tmp_path, case):
    archive, receipt = make_archive(tmp_path)
    args, review = [archive], "2099-01-01"
    if case == "corrupt":
        archive.write_bytes(b"bad")
    if case == "duplicate":
        args *= 2
    if case == "missing":
        receipt.unlink()
    if case == "receipt":
        receipt.write_text("{}")
    if case == "symlink":
        link = tmp_path / "alias.tar.zst"
        link.symlink_to(archive)
        args = [link]
    if case == "past":
        review = "2000-01-01"
    with pytest.raises((W0Error, KeyError)):
        archive_catalog(args, review)


def test_catalog_unsafe_member(tmp_path):
    archive, _ = make_archive(tmp_path, "../escape")
    with pytest.raises(W0Error):
        archive_catalog([archive], "2099-01-01")
