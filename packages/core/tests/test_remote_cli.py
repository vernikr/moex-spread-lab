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


def _stub_sync(monkeypatch, tmp_path, *, new_head, changed, venv=True):
    """Stub the workstation shell: HEAD moves old -> new_head with the given diff."""
    calls = []
    old_head = "0" * 40

    def command(root, *args):
        calls.append(args)
        if "status" in args:
            return ""
        if "branch" in args:
            return "main"
        if "rev-parse" in args:
            if "origin/main" in args:
                return new_head
            return (
                old_head
                if len([c for c in calls if "rev-parse" in c and "origin" not in c]) == 1
                else new_head
            )
        if "diff" in args:
            return "\n".join(changed)
        return ""

    if venv:
        venv_python = tmp_path / ".venv/bin/python3"
        venv_python.parent.mkdir(parents=True, exist_ok=True)
        venv_python.write_text("#!/bin/sh\nexit 0\n")

    monkeypatch.setattr(workstation_remote, "command", command)
    monkeypatch.setattr(workstation_remote, "service", lambda root: {"launchd_loaded": True})
    monkeypatch.setattr(
        workstation_remote,
        "await_readiness",
        lambda root, expected_head, **kwargs: {"git_head": expected_head, "pid": 1234},
    )
    return calls, old_head


def test_sync_fetches_fast_forwards_syncs_and_restarts(monkeypatch, tmp_path):
    calls, _old = _stub_sync(
        monkeypatch,
        tmp_path,
        new_head="b" * 40,
        changed=["packages/proof/src/moex_spread_lab/proof_runtime/service.py"],
    )
    result = workstation_remote.sync(tmp_path)
    assert result["head"] == "b" * 40
    assert result["dependencies_synced"] is False  # no dependency change, .venv present
    assert result["restarted"] is True
    assert result["outcomes"] == {
        "published": "fast-forwarded",
        "synced": "skipped-no-dependency-changes",
        "health-confirmed": "confirmed",
    }
    assert result["health"]["git_head"] == "b" * 40
    assert ("git", "merge", "--ff-only", "origin/main") in calls
    assert not any(call[:2] == ("uv", "sync") for call in calls)
    assert any(call[:3] == ("launchctl", "kickstart", "-k") for call in calls)


def test_sync_noop_when_head_unchanged(monkeypatch, tmp_path):
    calls, old_head = _stub_sync(monkeypatch, tmp_path, new_head="a" * 40, changed=[])
    # HEAD is already at origin/main after fetch.

    def command(root, *args):
        calls.append(args)
        if "status" in args:
            return ""
        if "branch" in args:
            return "main"
        if "rev-parse" in args:
            return "a" * 40
        return ""

    monkeypatch.setattr(workstation_remote, "command", command)
    monkeypatch.setattr(
        workstation_remote,
        "await_readiness",
        lambda *a, **k: pytest.fail("no readiness poll on a no-op sync"),
    )
    result = workstation_remote.sync(tmp_path)
    assert result["outcomes"]["published"] == "up-to-date"
    assert result["outcomes"]["synced"] == "skipped-up-to-date"
    assert result["outcomes"]["health-confirmed"] == "not-required"
    assert result["changed_files"] == 0 and result["restarted"] is False
    assert not any("merge" in call for call in calls)
    assert not any(call[:2] == ("uv", "sync") for call in calls)
    assert not any(call[:2] == ("launchctl", "kickstart") for call in calls)


def test_sync_docs_only_fast_forwards_without_restart(monkeypatch, tmp_path):
    calls, _old = _stub_sync(
        monkeypatch,
        tmp_path,
        new_head="c" * 40,
        changed=[
            "docs/development/agent-performance-2/plan.md",
            "AGENTS.md",
            ".github/workflows/ci.yml",
        ],
    )
    result = workstation_remote.sync(tmp_path)
    assert result["outcomes"] == {
        "published": "fast-forwarded",
        "synced": "skipped-no-dependency-changes",
        "health-confirmed": "not-required",
    }
    assert result["restarted"] is False and result["changed_files"] == 3
    assert any(call[:4] == ("git", "merge", "--ff-only", "origin/main") for call in calls)
    assert not any(call[:2] == ("uv", "sync") for call in calls)
    assert not any(call[:2] == ("launchctl", "kickstart") for call in calls)


def test_sync_dependency_change_reconciles_venv_and_restarts(monkeypatch, tmp_path):
    calls, _old = _stub_sync(
        monkeypatch, tmp_path, new_head="d" * 40, changed=["uv.lock", "docs/readme.md"]
    )
    result = workstation_remote.sync(tmp_path)
    assert ("uv", "sync", "--frozen") in calls
    assert result["dependencies_synced"] is True
    assert result["outcomes"]["synced"] == "uv-sync"
    assert result["outcomes"]["health-confirmed"] == "confirmed"
    assert any(call[:2] == ("launchctl", "kickstart") for call in calls)


def test_sync_missing_venv_reconciles_even_for_docs_only(monkeypatch, tmp_path):
    calls, _old = _stub_sync(
        monkeypatch, tmp_path, new_head="e" * 40, changed=["docs/x.md"], venv=False
    )
    result = workstation_remote.sync(tmp_path)
    assert ("uv", "sync", "--frozen") in calls
    assert result["dependencies_synced"] is True


def test_sync_fastcore_change_verifies_binary_and_restarts(monkeypatch, tmp_path):
    calls, _old = _stub_sync(
        monkeypatch,
        tmp_path,
        new_head="f" * 40,
        changed=["packages/core/src/moex_spread_lab/core/_fastcore.c"],
    )
    result = workstation_remote.sync(tmp_path)
    assert result["fastcore_source_changed"] is True
    assert result["fastcore_verified"] is True
    assert result["outcomes"]["health-confirmed"] == "confirmed"


def test_sync_repeated_sha_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(workstation_remote, "service", lambda root: {"launchd_loaded": True})
    state = {"head": "a" * 40, "fetches": 0}

    def command(root, *args):
        if "status" in args or "branch" in args:
            return "" if "status" in args else "main"
        if "fetch" in args:
            state["fetches"] += 1
            return ""
        if "rev-parse" in args:
            return state["head"]
        return ""

    monkeypatch.setattr(workstation_remote, "command", command)
    first = workstation_remote.sync(tmp_path)
    second = workstation_remote.sync(tmp_path)
    assert first["outcomes"]["published"] == "up-to-date"
    assert second["outcomes"]["published"] == "up-to-date"
    assert state["fetches"] == 2  # both runs verify, neither restarts


def test_runtime_path_classification():
    runtime = workstation_remote._is_runtime_path
    assert runtime("src/moex_spread_lab/cli.py") is True
    assert runtime("packages/proof/src/moex_spread_lab/proof_runtime/service.py") is True
    assert runtime("packages/proof/tests/test_proof_plateau.py") is False
    assert runtime("packages/web/tests/browser/x.py") is False
    assert runtime("packages/core/pyproject.toml") is True  # dependency input
    assert runtime("uv.lock") is True
    assert runtime("docs/development/plan.md") is False
    assert runtime("AGENTS.md") is False
    assert runtime(".github/workflows/ci.yml") is False
    assert runtime("scripts/check_health.sh") is False
    assert runtime("config/code-health-baseline.json") is False
    assert runtime("reports/perf/records.jsonl") is False
    assert runtime("some/new/unknown-tool.py") is True  # fail safe towards freshness


def test_await_readiness_requires_matching_head_and_is_bounded(monkeypatch, tmp_path):
    attempts = []

    class Response:
        def __init__(self, head):
            self._head = head

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, *args):
            return b'{"git_head": "' + self._head.encode() + b'"}'

    def urlopen(url, timeout):
        attempts.append(url)
        if len(attempts) < 3:
            return Response("wrong-head")
        return Response("b" * 40)

    monkeypatch.setattr(workstation_remote.urllib.request, "urlopen", urlopen)
    health = workstation_remote.await_readiness(tmp_path, "b" * 40, timeout_s=10, interval_s=0.01)
    assert health["git_head"] == "b" * 40 and len(attempts) == 3

    def always_wrong(url, timeout):
        return Response("wrong-head")

    monkeypatch.setattr(workstation_remote.urllib.request, "urlopen", always_wrong)
    with pytest.raises(RuntimeError, match="did not become ready"):
        workstation_remote.await_readiness(tmp_path, "b" * 40, timeout_s=0.2, interval_s=0.05)


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
