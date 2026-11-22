"""Bridge transport and workstation sync safety regressions."""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

import pytest

from moex_spread_lab.core import remote_cli, workstation_remote


def test_request_sends_bearer_only_in_header(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, *args):
            return b'{"ok": true}'

    class Opener:
        def open(self, req, timeout):
            captured["request"] = req
            return Response()

    monkeypatch.setattr(remote_cli.urllib.request, "build_opener", lambda *a: Opener())
    assert remote_cli.request(
        "https://example.test/exec", "token-xyz", body={"command": "pwd"}
    ) == {"ok": True}
    req = captured["request"]
    assert req.get_header("Authorization") == "Bearer token-xyz"
    assert b"token-xyz" not in req.data and "token-xyz" not in req.full_url


def test_status_and_redaction(monkeypatch, capsys):
    monkeypatch.setenv("ARENA_BRIDGE_URL", "https://example.test")
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "token-xyz")
    calls = []

    def request(url, token, *, body=None):
        calls.append((url, body))
        if url.endswith("/health"):
            return {"ok": True}
        return {"ok": True, "exit_code": 1, "stderr": "failure token-xyz"}

    monkeypatch.setattr(remote_cli, "request", request)
    assert remote_cli.run(argparse.Namespace(remote_command="status")) == 1
    assert "token-xyz" not in capsys.readouterr().err
    assert "token-xyz" not in json.dumps(calls)


def test_redirects_are_rejected():
    assert (
        remote_cli.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test")
        is None
    )


def test_dirty_checkout_and_wrong_branch_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(workstation_remote, "command", lambda root, *args: " M edited.py")
    with pytest.raises(RuntimeError, match="tracked checkout"):
        workstation_remote.sync(tmp_path)
    monkeypatch.setattr(
        workstation_remote, "command", lambda root, *args: "" if "status" in args else "feature"
    )
    with pytest.raises(RuntimeError, match="main"):
        workstation_remote.sync(tmp_path)


def test_sync_fetches_fast_forwards_syncs_and_restarts(monkeypatch, tmp_path):
    calls = []

    def command(root, *args):
        calls.append(args)
        if "status" in args:
            return ""
        if "branch" in args:
            return "main"
        if "rev-parse" in args:
            return "abc"
        if "diff" in args:
            return "src/other.py"
        return ""

    monkeypatch.setattr(workstation_remote, "command", command)
    monkeypatch.setattr(workstation_remote, "service", lambda root: {"launchd_loaded": True})
    result = workstation_remote.sync(tmp_path)
    assert result["head"] == "abc" and result["dependencies_synced"]
    assert ("git", "merge", "--ff-only", "origin/main") in calls
    assert ("uv", "sync", "--frozen") in calls
    assert any(call[:3] == ("launchctl", "kickstart", "-k") for call in calls)


def test_environment_validation(monkeypatch, capsys):
    monkeypatch.setenv("ARENA_BRIDGE_URL", "http://example.test")
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "secret")
    assert remote_cli.run(SimpleNamespace(remote_command="sync")) == 2
    assert "secret" not in capsys.readouterr().err


def test_probe_command_validates_json_and_keeps_credentials_out(monkeypatch):
    monkeypatch.setenv("MOEX_WORKSTATION_ROOT", "/checkout with spaces")
    args = argparse.Namespace(
        remote_command="probe",
        json='{"pair":"x"}',
        settings='{"random_seed":41}',
        action="run",
        profile=False,
        sample_worker_rss=True,
        repeat=1,
        warmup=0,
        full=False,
    )
    command = remote_cli._workstation_command(args, "pass")
    assert "checkout with spaces" in command and "random_seed" in command
    assert '"sample_worker_rss": true' in command
    args.json = "[]"
    with pytest.raises(ValueError, match="JSON objects"):
        remote_cli._workstation_command(args, "pass")
