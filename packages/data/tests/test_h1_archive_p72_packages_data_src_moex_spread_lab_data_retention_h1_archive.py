"""Facade and payload contracts of the strict ``h1_archive`` conversion (#70).

The module keeps its historical module-level hooks (``build_h1_plan``,
``verify_sealed_h1``, ``_gain``, ``_json_new`` …) so callers and tests can keep
patching them by dotted path. The plan payload is validated in place, so JSON
round-trips must be unchanged by strict typing.
"""

import json
from pathlib import Path
from typing import Any, get_type_hints

import pytest

from moex_spread_lab.core.w0 import canonical_bytes, sha256_bytes
from moex_spread_lab.data.lake.data_lake import DataLakeError, ParquetBarStore
from moex_spread_lab.data.lake.h1_seal import (
    SealedH1Reader,
    activate_h1,
)
from moex_spread_lab.data.lake.h1_seal import (
    verify_sealed_h1 as lake_verify_sealed_h1,
)
from moex_spread_lab.data.retention import h1_archive
from test_h1_seal import prepared  # noqa: F401  # pytest fixture

FACADE_NAMES = (
    "SCHEMA",
    "build_plan",
    "execute",
    "build_h1_plan",
    "verify_sealed_h1",
    "verify_archive",
    "_publish",
    "_json_new",
    "_evidence",
    "_validate",
    "_allocated",
    "_gain",
    "_rehearse_h1_restore",
    "_archive_only_h1",
    "_prepare_retry_journal",
    "_unlink_h1_sources",
    "_apply_h1_archive",
    "_safe_data_path",
    "_validate_archive_path",
    "_prune_empty_parents",
)


def test_module_level_facade_hooks_stay_resolvable_and_typed():
    assert h1_archive.SCHEMA == "sealed-h1-archive.v1"
    for name in FACADE_NAMES[1:]:
        assert callable(getattr(h1_archive, name)), name
    hints = get_type_hints(h1_archive.execute)
    assert hints["return"] == dict[str, Any]
    assert set(hints) == {
        "root",
        "plan_path",
        "mode",
        "archive",
        "evidence",
        "retry_restored",
        "return",
    }
    assert hints["archive"] == Path | None and hints["evidence"] == Path | None
    assert hints["retry_restored"] is bool
    # The historical re-exports must stay the sealed-lake implementations.
    assert h1_archive.verify_sealed_h1 is lake_verify_sealed_h1
    assert h1_archive.verify_sealed_h1.__module__ == "moex_spread_lab.data.lake.h1_seal"


def test_plan_payload_round_trips_and_rejects_altered_fields(prepared, tmp_path):  # noqa: F811
    root, intake, screen, plan_path, candidate = prepared
    ParquetBarStore(root).import_legacy(intake)
    activate_h1(root, plan_path, candidate)
    seal = SealedH1Reader(root).payload()
    assert seal is not None
    evidence = root / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "source_fingerprint": seal["plan"]["source_fingerprint"],
                "seal_sha256": sha256_bytes(canonical_bytes(seal)),
                "checks": dict.fromkeys(("regression", "performance", "ui_smoke"), "passed"),
            }
        )
    )
    archive = tmp_path / "archives/h1.tar.zst"
    archive_plan = tmp_path / "archive-plan.json"
    result = h1_archive.execute(root, archive_plan, "dry-run", archive=archive, evidence=evidence)
    assert result == {
        "status": "planned",
        "source_count": 2,
        "source_fingerprint": seal["plan"]["source_fingerprint"],
    }
    payload = json.loads(archive_plan.read_text(encoding="utf-8"))
    assert payload["schema_version"] == h1_archive.SCHEMA
    assert payload["minimum_net_gain_fraction"] == 0.8
    assert payload["files"] == [
        {
            "path": str(entry["legacy_path"]).removeprefix("data/"),
            "sha256": entry["legacy_sha256"],
            "logical_bytes": entry["logical_bytes"],
        }
        for entry in seal["plan"]["files"]
    ]
    altered = dict(payload, minimum_net_gain_fraction=0.79)
    archive_plan.write_text(json.dumps(altered))
    with pytest.raises(DataLakeError, match="does not match active seal"):
        h1_archive.execute(root, archive_plan, "archive-only")
    assert not archive.exists()


def test_patched_facade_hooks_stay_dynamic(prepared, tmp_path):  # noqa: F811
    """Dotted-path monkeypatching must keep reaching the module namespace."""
    original = h1_archive.build_h1_plan
    calls = {"count": 0}

    def counting(root):
        calls["count"] += 1
        return original(root)

    h1_archive.build_h1_plan = counting
    try:
        root, intake, screen, plan_path, candidate = prepared
        ParquetBarStore(root).import_legacy(intake)
        activate_h1(root, plan_path, candidate)
        seal = SealedH1Reader(root).payload()
        assert seal is not None
        evidence = root / "evidence.json"
        evidence.write_text(
            json.dumps(
                {
                    "source_fingerprint": seal["plan"]["source_fingerprint"],
                    "seal_sha256": sha256_bytes(canonical_bytes(seal)),
                    "checks": dict.fromkeys(("regression", "performance", "ui_smoke"), "passed"),
                }
            )
        )
        result = h1_archive.execute(
            root,
            tmp_path / "archive-plan.json",
            "dry-run",
            archive=tmp_path / "archives/h1.tar.zst",
            evidence=evidence,
        )
        assert result["status"] == "planned"
        assert calls["count"] >= 1
    finally:
        h1_archive.build_h1_plan = original
