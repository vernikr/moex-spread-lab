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
    diagnostic = json.loads(raw)["result"]
    assert diagnostic["run_id"] == normal["run_id"]
    assert diagnostic["selected_parameters"] == normal["selected_parameters"]
    after = {p.name: p.read_bytes() for p in analytics.glob("*") if p.is_file()}
    assert after == before
