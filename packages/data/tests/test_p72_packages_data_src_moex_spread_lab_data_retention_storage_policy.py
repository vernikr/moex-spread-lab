"""Regression coverage for the #72 strict-typing refactor of run-artifact manifests.

`storage_policy` now narrows `json.loads` payloads through explicit `isinstance`
checks before casting. These tests pin the observable outcome of every branch of
that narrowing: the dry-run TTL states, the fail-closed error messages and the
unchanged identity of the re-exported archive verifier.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.data.retention import storage_policy
from moex_spread_lab.data.retention.data_archive_candidates import verify_archive
from moex_spread_lab.data.retention.storage_policy import storage_budget


def _run_manifest(
    *,
    expires_in_days: float = -1.0,
    pinned: bool = False,
    detail_status: str = "available",
) -> dict[str, Any]:
    expires_at = (
        None if pinned else (datetime.now(UTC) + timedelta(days=expires_in_days)).isoformat()
    )
    return {
        "schema_version": storage_policy.RUN_ARTIFACT_SCHEMA,
        "details": {"daily": {"file": "daily.parquet"}, "trades": {"file": "trades.parquet"}},
        "retention": {
            "pinned": pinned,
            "detail_ttl_days": 30,
            "expires_at": expires_at,
            "detail_status": detail_status,
        },
    }


def _write_run(root: Path, name: str, manifest: object) -> Path:
    run = root / "data/analytics/runs" / name
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for detail in ("daily.parquet", "trades.parquet"):
        (run / detail).write_bytes(b"detail")
    return run


class TestRunArtifactManifestTtlP72PackagesDataSrcMoexSpreadLabDataRetentionStoragePolicy:
    def test_expired_manifest_plans_existing_detail_files(self, tmp_path: Path) -> None:
        run = _write_run(tmp_path, "expired", _run_manifest(expires_in_days=-1))
        (run / "trades.parquet").unlink()
        report = storage_budget(tmp_path)["ttl_dry_run"]["run_details"]
        assert report["status"] == "dry_run"
        assert report["expired_artifact_count"] == 1
        assert report["retained_artifact_count"] == 0
        assert report["planned_detail_files"] == ["expired/daily.parquet"]

    def test_pinned_manifest_is_retained_without_expiry_parsing(self, tmp_path: Path) -> None:
        _write_run(tmp_path, "pinned", _run_manifest(pinned=True, expires_in_days=-1))
        report = storage_budget(tmp_path)["ttl_dry_run"]["run_details"]
        assert report["retained_artifact_count"] == 1
        assert report["expired_artifact_count"] == 0
        assert report["planned_detail_files"] == []

    def test_future_expiry_is_retained(self, tmp_path: Path) -> None:
        _write_run(tmp_path, "retained", _run_manifest(expires_in_days=30))
        report = storage_budget(tmp_path)["ttl_dry_run"]["run_details"]
        assert report["retained_artifact_count"] == 1
        assert report["planned_detail_files"] == []

    def test_unavailable_details_are_skipped(self, tmp_path: Path) -> None:
        _write_run(tmp_path, "moved", _run_manifest(detail_status="evicted"))
        report = storage_budget(tmp_path)["ttl_dry_run"]["run_details"]
        assert report["expired_artifact_count"] == 0
        assert report["retained_artifact_count"] == 0
        assert report["planned_detail_files"] == []

    @pytest.mark.parametrize("payload", ["[]", '"manifest"', "42", "null"])
    def test_non_object_manifest_root_fails_closed(self, tmp_path: Path, payload: str) -> None:
        run = _write_run(tmp_path, "broken", json.loads(payload))
        with pytest.raises(W0Error, match="unsupported run artifact schema"):
            storage_budget(tmp_path)
        assert (run / "manifest.json").is_file()

    def test_wrong_schema_version_fails_closed(self, tmp_path: Path) -> None:
        manifest = _run_manifest()
        manifest["schema_version"] = "research-run-artifact.v0"
        _write_run(tmp_path, "legacy", manifest)
        with pytest.raises(W0Error, match="unsupported run artifact schema"):
            storage_budget(tmp_path)

    @pytest.mark.parametrize("section", ["details", "retention"])
    def test_missing_or_non_object_section_fails_closed(self, tmp_path: Path, section: str) -> None:
        manifest = _run_manifest()
        manifest[section] = ["not", "an", "object"]
        _write_run(tmp_path, "partial", manifest)
        with pytest.raises(W0Error, match="run artifact manifest is incomplete"):
            storage_budget(tmp_path)

    @pytest.mark.parametrize("record", [{"file": 7}, "daily", {}, {"file": None}])
    def test_invalid_detail_record_fails_closed(self, tmp_path: Path, record: object) -> None:
        manifest = _run_manifest()
        manifest["details"]["daily"] = record
        _write_run(tmp_path, "baddetail", manifest)
        with pytest.raises(W0Error, match="run artifact daily metadata is invalid"):
            storage_budget(tmp_path)

    @pytest.mark.parametrize("expires_at", [None, 20260101, "not-a-date", "2026-01-01T00:00:00"])
    def test_unusable_expiry_fails_closed(self, tmp_path: Path, expires_at: object) -> None:
        manifest = _run_manifest()
        manifest["retention"]["expires_at"] = expires_at
        _write_run(tmp_path, "badexpiry", manifest)
        with pytest.raises(W0Error, match="expires_at"):
            storage_budget(tmp_path)

    def test_rebound_verifier_keeps_the_public_name_and_function_object(self) -> None:
        assert storage_policy.verify_archive is verify_archive
