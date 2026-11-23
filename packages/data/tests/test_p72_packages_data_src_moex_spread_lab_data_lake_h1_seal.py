"""Isolated regressions for the strict-typed H1 seal facade."""

from __future__ import annotations

from pathlib import Path

from moex_spread_lab.data.lake import bar_types, h1_migration, h1_seal
from test_h1_seal import prepared  # noqa: F401  # pytest fixture


def test_p72_packages_data_src_moex_spread_lab_data_lake_h1_seal_facade_identity() -> None:
    assert h1_seal._signature is bar_types._signature
    assert h1_seal.build_h1_plan is h1_migration.build_h1_plan
    assert h1_seal.verify_h1_stage is h1_migration.verify_h1_stage
    assert h1_seal.SCHEMA == bar_types.H1_SEAL_SCHEMA


def test_p72_packages_data_src_moex_spread_lab_data_lake_h1_seal_payload_contract(
    prepared: tuple[Path, Path, Path, Path, Path],  # noqa: F811  # fixture shadows import
) -> None:
    root, _intake, _screen, plan, candidate = prepared
    sealed = h1_seal.activate_h1(root, plan, candidate)
    assert sealed == h1_seal.verify_sealed_h1(root)
    assert sealed["status"] == "sealed_verified"
    assert sealed["source_deletion_allowed"] is False
    restored = h1_seal.deactivate_h1(root)
    assert restored["status"] == "deactivated"
    assert restored["restored_json_count"] == sealed["dataset_count"]
