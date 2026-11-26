import json
from unittest.mock import patch

import pytest

import test_store_cutover as fixtures
from moex_spread_lab.data.lake.data_lake import DataLakeError, MarketDataStore, ParquetBarStore
from moex_spread_lab.data.lake.h1_migration import (
    build_h1_plan,
    stage_h1,
    verify_h1_stage,
    write_h1_plan,
)


@pytest.fixture
def inputs(tmp_path):
    helper = fixtures.StoreCutoverTests()
    intake = helper._write_series(tmp_path, "NEAR", "1h", 60, [10.0, 11.0, 12.0])
    screen = helper._write_series(
        tmp_path, "NEAR", "1h", 60, [20.0, 21.0, 22.0], folder="data/w1/candles-screen"
    )
    plan = tmp_path / "plan.json"
    output = tmp_path / "data-lake/candidates/h1"
    return tmp_path, intake, screen, plan, output


def test_namespace_separation_priority_and_no_activation(inputs):
    root, intake, screen, path, output = inputs
    plan = write_h1_plan(root, path)
    assert plan == build_h1_plan(root)
    assert plan["source_count"] == 2 and plan["selected_count"] == 1
    assert plan["screen_selection"]["NEAR"] == "data/w1/candles-screen/NEAR_1h.json"
    before = [p.read_bytes() for p in (intake, screen)]
    result = stage_h1(root, path, output)
    assert result["status"] == "verified_candidate"
    assert not result["runtime_activated"] and not result["source_deletion_allowed"]
    assert verify_h1_stage(root, path, output)["source_count"] == 2
    assert ParquetBarStore(root, output / "candles").read_bars("NEAR", "1h")[
        "close"
    ].to_pylist() == [10.0, 11.0, 12.0]
    assert ParquetBarStore(root, output / "candles-screen").read_bars("NEAR", "1h")[
        "close"
    ].to_pylist() == [20.0, 21.0, 22.0]
    assert [p.read_bytes() for p in (intake, screen)] == before
    assert MarketDataStore(root).read(screen).backend == "legacy_json"
    assert not (root / "data-lake/manifest.json").exists()


@pytest.mark.parametrize("change", ["hash", "new_screen", "removed_screen", "tampered_plan"])
def test_stale_plan_fails_before_creating_output(inputs, change):
    root, intake, screen, path, output = inputs
    if change == "new_screen":
        screen.unlink()
    write_h1_plan(root, path)
    if change == "hash":
        intake.write_text(intake.read_text() + " ")
    if change == "new_screen":
        screen.write_bytes(intake.read_bytes())
    if change == "removed_screen":
        screen.unlink()
    if change == "tampered_plan":
        value = json.loads(path.read_text())
        value["screen_selection"]["NEAR"] = str(intake)
        path.write_text(json.dumps(value))
    with pytest.raises(DataLakeError, match="stale"):
        stage_h1(root, path, output)
    assert not output.exists()


@pytest.mark.parametrize("change", ["symlink", "identity", "interval", "order"])
def test_invalid_source_refused(inputs, change):
    root, intake, screen, path, output = inputs
    if change == "symlink":
        screen.unlink()
        screen.symlink_to(intake)
    else:
        value = json.loads(screen.read_text())
        if change == "identity":
            value["secid"] = "OTHER"
        if change == "interval":
            value["interval"] = 24
        if change == "order":
            value["rows"].reverse()
        screen.write_text(json.dumps(value))
    with pytest.raises(DataLakeError):
        write_h1_plan(root, path)
    assert not path.exists()


def test_corruption_and_incomplete_stage_fail_closed(inputs):
    root, intake, screen, path, output = inputs
    write_h1_plan(root, path)
    stage_h1(root, path, output)
    parquet = next((output / "candles").rglob("data.parquet"))
    parquet.write_bytes(b"broken")
    with pytest.raises(DataLakeError):
        verify_h1_stage(root, path, output)
    report = output / "h1-stage.json"
    value = json.loads(report.read_text())
    value["status"] = "staging"
    report.write_text(json.dumps(value))
    with pytest.raises(DataLakeError, match="incomplete"):
        verify_h1_stage(root, path, output)


def test_existing_output_data_root_and_failure_journal(inputs):
    root, intake, screen, path, output = inputs
    write_h1_plan(root, path)
    with pytest.raises(DataLakeError, match="outside"):
        stage_h1(root, path, root / "data/new")
    output.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        stage_h1(root, path, output)
    output.rmdir()
    with patch.object(ParquetBarStore, "import_legacy", side_effect=DataLakeError("injected")):
        with pytest.raises(DataLakeError):
            stage_h1(root, path, output)
    assert json.loads((output / "h1-stage.json").read_text())["status"] == "failed"
    assert intake.exists() and screen.exists()
