"""Loopback diagnostics preserve preferences and do not persist proof history."""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from moex_spread_lab.core.settings import save_settings
from moex_spread_lab.web.server import ScreenerInputs, ScreenerStore, make_handler
from proof_fixtures import two_calendar_windows


@pytest.fixture
def server(tmp_path):
    two_calendar_windows(tmp_path)
    save_settings(tmp_path, {"random_seed": 41})
    store = ScreenerStore(ScreenerInputs(tmp_path))
    http = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store))
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield http.server_port, tmp_path
    http.shutdown()
    http.server_close()
    store.close()
    thread.join()


def request(port, method, path, body=None, headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        hdr = dict(headers or {})
        if body is not None:
            hdr["Content-Type"] = "application/json"
        conn.request(method, path, json.dumps(body) if body is not None else None, hdr)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_health_is_loopback_only_even_with_preview_optin(server, monkeypatch):
    port, _ = server
    monkeypatch.setenv("MSL_ALLOWED_HOSTS", "*")
    status, raw = request(port, "GET", "/api/dev/health")
    assert status == 200 and json.loads(raw)["port"] == port and len(raw) < 2048
    status, _ = request(port, "GET", "/api/dev/health", headers={"Host": "evil.test"})
    assert status == 403
    status, _ = request(
        port,
        "POST",
        "/api/dev/probe",
        {"action": "catalog"},
        headers={"Origin": "https://evil.test"},
    )
    assert status == 403


def test_run_matches_existing_route_and_does_not_persist(server):
    port, root = server
    analytics = root / "data/analytics"
    before = {p.name: p.read_bytes() for p in analytics.glob("*") if p.is_file()}
    selection = {"asset": "SR", "pair": "SRH3/SRM3"}
    status, normal = request(port, "POST", "/api/proof/run", selection)
    assert status == 200
    status, raw = request(
        port, "POST", "/api/dev/probe", {"action": "run", "payload": selection, "repeat": 2}
    )
    assert status == 200
    probe = json.loads(raw)
    assert probe["result"]["run_id"] == json.loads(normal)["run_id"]
    assert len(probe["telemetry"]["wall_ms"]) == 2 and len(raw) < 2048
    status, _ = request(
        port,
        "POST",
        "/api/dev/probe",
        {"action": "catalog", "settings_override": {"random_seed": 42}},
    )
    assert status == 200
    after = {p.name: p.read_bytes() for p in analytics.glob("*") if p.is_file()}
    assert after == before


def test_validation_rejects_invalid_types_and_override(server):
    port, _ = server
    for body in (
        {"action": []},
        {"action": "catalog", "response_mode": []},
        {"action": "catalog", "repeat": 0},
        {"action": "catalog", "settings_override": {"random_seed": -1}},
    ):
        status, _ = request(port, "POST", "/api/dev/probe", body)
        assert status == 400


def test_discovery_record_is_atomic_and_removed_only_by_owner(tmp_path):
    from moex_spread_lab.web.server_instance import remove_instance, write_instance

    first = write_instance(tmp_path, 1234)
    path = tmp_path / "data/analytics/server-instance.json"
    assert json.loads(path.read_text())["port"] == 1234
    second = write_instance(tmp_path, 5678)
    remove_instance(tmp_path, first)
    assert json.loads(path.read_text())["port"] == 5678
    remove_instance(tmp_path, second)
    assert not path.exists()


def test_optimization_matches_saved_route_without_new_history(server):
    port, root = server
    selection = {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion"}
    status, raw = request(port, "POST", "/api/proof/optimize", selection)
    assert status == 200, raw
    normal = json.loads(raw)
    analytics = root / "data/analytics"
    before = {p.name: p.read_bytes() for p in analytics.glob("*") if p.is_file()}
    status, raw = request(
        port,
        "POST",
        "/api/dev/probe",
        {"action": "optimize", "payload": selection, "response_mode": "full"},
    )
    assert status == 200, raw
    result = json.loads(raw)
    diagnostic = result["result"]
    phases = result["telemetry"]["operation_phase_ms"]
    assert all(
        phases[key] > 0 for key in ("prepare_cohort", "optuna_trials", "final_report", "plateau")
    )
    assert diagnostic["run_id"] == normal["run_id"]
    assert diagnostic["selected_parameters"] == normal["selected_parameters"]
    after = {p.name: p.read_bytes() for p in analytics.glob("*") if p.is_file()}
    assert after == before


def test_pool_census_counts_live_workers_only():
    from types import SimpleNamespace

    from moex_spread_lab.web.dev_pool_metrics import pool_snapshot

    assert pool_snapshot(SimpleNamespace(_proof=None))["active_pool_workers"] == 0
    live = SimpleNamespace(pid=17, is_alive=lambda: True)
    dead = SimpleNamespace(pid=18, is_alive=lambda: False)
    executor = SimpleNamespace(_processes={17: live, 18: dead})
    pool = SimpleNamespace(_worker_slots=[executor], _executor=None)
    store = SimpleNamespace(_proof=SimpleNamespace(_pool=pool))
    assert pool_snapshot(store) == {
        "pool_initialized": True,
        "worker_slots": 1,
        "active_pool_workers": 1,
    }


def test_probe_reports_real_request_thread_cache_lookups(server):
    port, _ = server
    selection = {
        "universe": "calendar_native",
        "asset": "SR",
        "pair": "SRH3/SRM3",
        "strategy": "zscore_mean_reversion",
    }
    status, raw = request(
        port, "POST", "/api/dev/probe", {"action": "run", "payload": selection, "repeat": 2}
    )
    assert status == 200, raw
    telemetry = json.loads(raw)["telemetry"]
    counts = telemetry["cache_metrics_main_thread"]
    assert counts["json_hits"] + counts["json_misses"] > 0
    assert counts["zscore_hits"] + counts["zscore_misses"] > 0
    assert telemetry["pool"]["active_pool_workers"] == 0


def test_worker_resource_sampling_is_labeled_and_handles_process_exit(monkeypatch):
    from types import SimpleNamespace

    from moex_spread_lab.web import dev_pool_resources as resources

    assert resources._cpu_seconds("1-02:03:04.5") == 93784.5
    monkeypatch.setattr(
        resources.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=" 10 1234 01:02.50\n"),
    )
    result = resources.worker_resources({10, 11})
    assert result == {
        "sampled_workers": 1,
        "rss_kb": 1234,
        "cpu_lifetime_ms": 62500,
        "complete": False,
    }
    assert resources.worker_resources(set())["complete"] is True


def test_pool_probe_worker_metrics_and_exact_windows_parity(server, monkeypatch):
    monkeypatch.setenv("MOEX_PROOF_FORCE_POOL", "1")
    monkeypatch.setenv("MOEX_PROOF_WORKERS", "2")
    port, root = server
    common = {"universe": "calendar_native", "asset": "SR", "strategy": "zscore_mean_reversion"}
    requests = [
        {**common, "pair": "SRH3/SRM3"},
        {**common, "pair": "SRM3/SRU3"},
        {"universe": "pair_screen", "pair": "CNYRUBF/CRZ6", "strategy": "mean_reversion"},
    ]
    payload = {"requests": requests}
    analytics = root / "data/analytics"
    before = {p.name: p.read_bytes() for p in analytics.glob("*") if p.is_file()}
    status, raw = request(port, "POST", "/api/proof/windows", payload)
    assert status == 200, raw
    normal = json.loads(raw)
    status, raw = request(
        port,
        "POST",
        "/api/dev/probe",
        {
            "action": "windows",
            "payload": payload,
            "response_mode": "full",
            "repeat": 2,
        },
    )
    assert status == 200, raw
    probe = json.loads(raw)
    assert probe["result"] == normal
    workers = probe["telemetry"]["worker_task_metrics"]
    assert workers["tasks"] >= 4 and workers["workers"] == 2
    assert sum(workers["cache"].values()) > 0
    assert workers["cpu_user_ms"] >= 0 and workers["cpu_sys_ms"] >= 0
    after = {p.name: p.read_bytes() for p in analytics.glob("*") if p.is_file()}
    assert after == before
