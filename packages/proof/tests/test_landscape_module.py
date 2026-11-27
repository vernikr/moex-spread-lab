"""Compatibility and progress contracts for extracted landscape diagnostics."""

from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof import landscape, proof_kernel, proof_search
from moex_spread_lab.proof.landscape import Axis


def test_kernel_and_search_reexport_landscape_api():
    assert proof_kernel.Landscape is proof_search.Landscape is landscape.Landscape
    assert proof_kernel._lamps is proof_search._lamps is landscape._lamps
    assert proof_kernel._ordered is proof_search._ordered
    assert (proof_kernel.GREEN, proof_kernel.AMBER) == (landscape.GREEN, landscape.AMBER)


def test_lamps_progress_preserves_profiles_and_skips_cached_probes():
    axes = {"entry_z": Axis(0.5, 2.0, 0.1), "exit_z": Axis(0.0, 1.5, 0.1)}
    center = {"entry_z": 1.0, "exit_z": 0.9}
    calls = []

    def evaluate(point):
        calls.append(point.copy())
        if point["entry_z"] == 1.2:
            raise W0Error("unavailable")
        return {"utility": 10.0}

    land = landscape.Landscape(axes, evaluate)
    land.measure(center)
    events = []
    result = landscape._lamps(
        land, center, on_progress=lambda done, total: events.append((done, total))
    )
    assert events == [(1, 4), (2, 4), (3, 4), (4, 4)]
    assert len(calls) == 5
    assert result == landscape._lamps(land, center)
    assert len(calls) == 5
    assert result["fields"]["entry_z"]["neighbors"][1]["reason"] == "unavailable"
    assert all(p["exit_z"] < p["entry_z"] for p in calls)
    blocked = landscape._lamps(
        land, center, reason="blocked", on_progress=lambda *_: events.append(None)
    )
    assert all(field["reason"] == "blocked" for field in blocked["fields"].values())
    assert len(events) == 4
