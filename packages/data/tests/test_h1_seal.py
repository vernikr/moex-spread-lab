from unittest.mock import patch

import pytest

import test_store_cutover as fixtures
from moex_spread_lab.cli import main
from moex_spread_lab.data.lake.data_lake import DataLakeError, MarketDataStore
from moex_spread_lab.data.lake.h1_migration import stage_h1, write_h1_plan
from moex_spread_lab.data.lake.h1_seal import activate_h1, deactivate_h1, verify_sealed_h1


def _read_projected_h1(store: MarketDataStore, secid: str):
    path = store.select_h1(secid)
    assert path is not None
    series = store.read(path, columns=("begin", "open", "close", "volume"))
    return (
        series.source_sha256,
        [(r["begin"], r["open"], r["close"], r["volume"]) for r in series.rows],
    )


@pytest.fixture
def prepared(tmp_path):
    helper = fixtures.StoreCutoverTests()
    intake = helper._write_series(tmp_path, "NEAR", "1h", 60, [10.0, 11.0, 12.0])
    screen = helper._write_series(
        tmp_path, "NEAR", "1h", 60, [20.0, 21.0, 22.0], folder="data/w1/candles-screen"
    )
    plan = tmp_path / "plan.json"
    candidate = tmp_path / "data-lake/candidates/h1"
    write_h1_plan(tmp_path, plan)
    stage_h1(tmp_path, plan, candidate)
    return tmp_path, intake, screen, plan, candidate


def test_no_json_read_and_screen_priority_and_calendar_equivalence(prepared):
    root, intake, screen, plan, candidate = prepared
    before = _read_projected_h1(MarketDataStore(root), "NEAR")
    activate_h1(root, plan, candidate)
    store = MarketDataStore(root)
    source = store.read(screen)
    assert source.backend == "sealed_parquet"
    intake.unlink()
    screen.unlink()
    fresh_store = MarketDataStore(root)
    assert fresh_store.select_h1("NEAR") == screen
    assert fresh_store.source_available(screen)
    after = fresh_store.read(screen, columns=("begin", "open", "close", "volume"))
    assert after.rows[0]["close"] == 20.0
    assert after.source_sha256 == source.source_sha256
    assert after.source_path == str(screen)
    assert _read_projected_h1(fresh_store, "NEAR") == before
    assert verify_sealed_h1(root)["retained_json_count"] == 0
    with pytest.raises(DataLakeError):
        deactivate_h1(root)


@pytest.mark.parametrize(
    "fault",
    ["bars", "sources", "seal", "marker", "missing_seal", "missing_marker", "source_changed"],
)
def test_tampering_never_falls_back_even_with_warm_cache(prepared, fault):
    root, intake, screen, plan, candidate = prepared
    activate_h1(root, plan, candidate)
    store = MarketDataStore(root)
    store.read(screen)
    if fault in ("bars", "sources"):
        name = "data.parquet" if fault == "bars" else "sources.parquet"
        path = next((candidate / "candles-screen").rglob(name))
        path.write_bytes(b"corrupt")
    elif fault == "source_changed":
        screen.write_text(screen.read_text() + " ")
    else:
        name = "h1-seal.enabled.json" if "marker" in fault else "h1-seal.json"
        path = root / "data-lake" / name
        if fault.startswith("missing"):
            path.unlink()
        else:
            path.write_text("{}")
    with pytest.raises(DataLakeError):
        store.read(screen)
    assert intake.exists()  # Alternate copy must not be selected.


def test_missing_preferred_parquet_never_selects_intake(prepared):
    root, intake, screen, plan, candidate = prepared
    activate_h1(root, plan, candidate)
    screen.unlink()
    next((candidate / "candles-screen").rglob("data.parquet")).unlink()
    store = MarketDataStore(root)
    assert store.select_h1("NEAR") == screen
    with pytest.raises(DataLakeError):
        store.read(screen)


def test_new_screened_copy_invalidates_selection(prepared):
    root, intake, screen, plan, candidate = prepared
    screen.unlink()
    plan.unlink()
    write_h1_plan(root, plan)
    new_candidate = root / "data-lake/candidates/new"
    stage_h1(root, plan, new_candidate)
    activate_h1(root, plan, new_candidate)
    screen.write_bytes(intake.read_bytes())
    store = MarketDataStore(root)
    with pytest.raises(DataLakeError):
        store.select_h1("NEAR")
    with pytest.raises(DataLakeError):
        store.read(intake)


def test_deactivation_requires_originals_and_preserves_evidence(prepared):
    root, intake, screen, plan, candidate = prepared
    activate_h1(root, plan, candidate)
    with pytest.raises(DataLakeError):
        activate_h1(root, plan, candidate)
    saved = screen.read_bytes()
    screen.unlink()
    with pytest.raises(DataLakeError):
        deactivate_h1(root)
    screen.write_bytes(saved)
    result = deactivate_h1(root)
    assert result["status"] == "deactivated"
    assert MarketDataStore(root).read(screen).backend == "legacy_json"
    activate_h1(root, plan, candidate)
    deactivate_h1(root)
    assert len(list((root / "data-lake/h1-seal-history").iterdir())) == 2


def test_manifest_mutation_does_not_change_sealed_record(prepared):
    root, intake, screen, plan, candidate = prepared
    activate_h1(root, plan, candidate)
    (candidate / "candles-screen/manifest.json").write_text("{}")
    screen.unlink()
    assert MarketDataStore(root).read(screen).rows[0]["close"] == 20.0


def test_cli_unknown_input_and_source_symlink(prepared):
    root, intake, screen, plan, candidate = prepared
    args = ["moex-spread-lab", "data-h1-seal", "--root", str(root)]
    with patch(
        "sys.argv", args + ["--activate", "--plan", str(plan), "--candidate", str(candidate)]
    ):
        assert main() == 0
    with patch("sys.argv", args + ["--verify"]):
        assert main() == 0
    store = MarketDataStore(root)
    with pytest.raises(DataLakeError):
        store.read(root / "data/w1/candles/UNKNOWN_1h.json")
    screen.unlink()
    screen.symlink_to(intake)
    with pytest.raises(DataLakeError):
        store.read(screen)
