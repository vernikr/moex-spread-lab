"""Bridge transport, candidate patch guard and workstation sync safety regressions."""

from __future__ import annotations

import argparse
import base64
import json
import platform
import shutil
import subprocess
from pathlib import Path

import pytest

from moex_spread_lab.remote import cli as remote_cli
from moex_spread_lab.remote import workstation as workstation_remote


class _Response:
    def __init__(self, body: bytes = b'{"ok": true}') -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, *_args):
        return self._body


def test_request_sends_bearer_only_in_header_and_refuses_redirects(monkeypatch):
    captured = {}

    class Opener:
        def open(self, req, **_kwargs):
            captured["request"] = req
            return _Response()

    monkeypatch.setattr(remote_cli.urllib.request, "build_opener", lambda *a: Opener())
    assert remote_cli.request(
        "https://example.test/exec", "token-xyz", body={"command": "pwd"}
    ) == {"ok": True}
    req = captured["request"]
    assert req.get_header("Authorization") == "Bearer token-xyz"
    assert b"token-xyz" not in req.data and "token-xyz" not in req.full_url
    redirect = remote_cli.NoRedirect().redirect_request(
        None, None, 302, "", {}, "https://evil.test"
    )
    assert redirect is None


def test_status_failure_and_env_validation_keep_the_token_out_of_output(monkeypatch, capsys):
    monkeypatch.setenv("ARENA_BRIDGE_URL", "https://example.test")
    monkeypatch.setenv("ARENA_BRIDGE_TOKEN", "token-xyz")
    calls = []

    def request(url, _token, *, body=None):
        calls.append((url, body))
        if url.endswith("/health"):
            return {"ok": True}
        return {"ok": True, "exit_code": 1, "stderr": "failure token-xyz"}

    monkeypatch.setattr(remote_cli, "request", request)
    assert remote_cli.run(argparse.Namespace(remote_command="status")) == 1
    assert "token-xyz" not in capsys.readouterr().err
    assert "token-xyz" not in json.dumps(calls)
    monkeypatch.setenv("ARENA_BRIDGE_URL", "http://example.test")  # plain HTTP is refused
    assert remote_cli.run(argparse.Namespace(remote_command="sync")) == 2
    assert "token-xyz" not in capsys.readouterr().err


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
    return calls


_FF = "fast-forwarded"
_SKIP = "skipped-no-dependency-changes"
# (changed paths, .venv present, expected result subset, uv sync ran, launchd restart ran)
SYNC_SCENARIOS = [
    (
        ["packages/proof/src/moex_spread_lab/proof/service.py"],
        True,
        {
            "dependencies_synced": False,
            "restarted": True,
            "health": {"git_head": "b" * 40},
            "outcomes": {"published": _FF, "synced": _SKIP, "health-confirmed": "confirmed"},
        },
        False,
        True,
    ),
    (
        ["docs/development/github-project.md", "AGENTS.md", ".github/workflows/ci.yml"],
        True,
        {
            "restarted": False,
            "changed_files": 3,
            "outcomes": {"published": _FF, "synced": _SKIP, "health-confirmed": "not-required"},
        },
        False,
        False,
    ),
    (
        ["uv.lock", "docs/readme.md"],
        True,
        {
            "dependencies_synced": True,
            "outcomes": {"synced": "uv-sync", "health-confirmed": "confirmed"},
        },
        True,
        True,
    ),
    (["docs/x.md"], False, {"dependencies_synced": True}, True, None),
    (
        ["packages/core/src/moex_spread_lab/core/_fastcore.c"],
        True,
        {
            "fastcore_source_changed": True,
            "fastcore_verified": True,
            "outcomes": {"health-confirmed": "confirmed"},
        },
        None,
        None,
    ),
]


@pytest.mark.parametrize(("changed", "venv", "expected", "uv_sync", "restart"), SYNC_SCENARIOS)
def test_sync_restarts_and_resyncs_only_when_the_change_requires_it(
    monkeypatch, tmp_path, changed, venv, expected, uv_sync, restart
):
    calls = _stub_sync(monkeypatch, tmp_path, new_head="b" * 40, changed=changed, venv=venv)
    result = workstation_remote.sync(tmp_path)
    assert result["head"] == "b" * 40
    assert ("git", "merge", "--ff-only", "origin/main") in calls
    for key, value in expected.items():
        if isinstance(value, dict):
            assert {name: result[key][name] for name in value} == value, key
        else:
            assert result[key] == value, key
    if uv_sync is not None:
        assert any(call[:2] == ("uv", "sync") for call in calls) is uv_sync
    if restart is not None:
        assert any(call[:3] == ("launchctl", "kickstart", "-k") for call in calls) is restart


def test_sync_is_a_no_op_when_head_is_already_at_origin(monkeypatch, tmp_path):
    monkeypatch.setattr(workstation_remote, "service", lambda root: {"launchd_loaded": True})
    monkeypatch.setattr(
        workstation_remote,
        "await_readiness",
        lambda *_args, **_kwargs: pytest.fail("no readiness poll on a no-op sync"),
    )
    calls = []

    def command(root, *args):
        calls.append(args)
        if "status" in args:
            return ""
        if "branch" in args:
            return "main"
        return "a" * 40 if "rev-parse" in args else ""

    monkeypatch.setattr(workstation_remote, "command", command)
    first, second = workstation_remote.sync(tmp_path), workstation_remote.sync(tmp_path)
    for result in (first, second):
        assert result["outcomes"] == {
            "published": "up-to-date",
            "synced": "skipped-up-to-date",
            "health-confirmed": "not-required",
        }
        assert result["changed_files"] == 0 and result["restarted"] is False
    assert sum("fetch" in call for call in calls) == 2  # both runs verify, neither restarts
    assert not any("merge" in call or call[:1] in {("uv",), ("launchctl",)} for call in calls)


@pytest.mark.parametrize(
    ("path", "runtime"),
    [
        ("src/moex_spread_lab/cli.py", True),
        ("packages/proof/src/moex_spread_lab/proof/service.py", True),
        ("packages/core/pyproject.toml", True),  # dependency input
        ("some/new/unknown-tool.py", True),  # fail safe towards freshness
        ("packages/proof/tests/test_proof_plateau.py", False),
        ("docs/development/plan.md", False),
        (".github/workflows/ci.yml", False),
        ("config/code-health-baseline.json", False),
    ],
)
def test_runtime_path_classification(path, runtime):
    assert workstation_remote._is_runtime_path(path) is runtime


def _pre310_interpreter() -> str | None:
    for name in ("python3.9", "python3.8"):
        if path := shutil.which(name):
            return path
    system = Path("/usr/bin/python3")
    if platform.system() == "Darwin" and system.is_file():
        version = subprocess.run(
            [str(system), "--version"], capture_output=True, text=True, check=True
        ).stdout.split()
        if version[:2] == ["Python", "3"] and int(version[2].split(".")[0]) < 10:
            return str(system)
    return None


@pytest.mark.skipif(
    _pre310_interpreter() is None, reason="no pre-3.10 interpreter to prove the transport"
)
def test_transported_workstation_source_imports_on_a_pre310_interpreter():
    # The client transports this file as `python3 -c "<base64 code>"`, and the
    # bridge resolves that to the macOS system 3.9: annotations must stay
    # unevaluated or the sync dies before any action runs (issue #326).
    source = Path(workstation_remote.__file__).read_bytes()
    encoded = base64.b64encode(source).decode("ascii")
    script = (
        "import base64\n"
        f"exec(compile(base64.b64decode('{encoded}'), '<workstation>', 'exec'),"
        " {'__name__': 'workstation_transport'})\n"
        "print('imported')"
    )
    interpreter = _pre310_interpreter()
    assert interpreter is not None
    result = subprocess.run(
        [interpreter, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "imported"


def test_await_readiness_requires_matching_head_and_is_bounded(monkeypatch, tmp_path):
    attempts = []

    def urlopen(url, **_kwargs):
        attempts.append(url)
        head = "wrong-head" if len(attempts) < 3 else "b" * 40
        return _Response(b'{"git_head": "' + head.encode() + b'"}')

    monkeypatch.setattr(workstation_remote.urllib.request, "urlopen", urlopen)
    health = workstation_remote.await_readiness(tmp_path, "b" * 40, timeout_s=10, interval_s=0.01)
    assert health["git_head"] == "b" * 40 and len(attempts) == 3

    monkeypatch.setattr(
        workstation_remote.urllib.request,
        "urlopen",
        lambda url, **_kwargs: _Response(b'{"git_head": "wrong-head"}'),
    )
    with pytest.raises(RuntimeError, match="did not become ready"):
        workstation_remote.await_readiness(tmp_path, "b" * 40, timeout_s=0.2, interval_s=0.05)


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


def _patch_options(monkeypatch, diff_output):
    class Result:
        stdout = diff_output

    monkeypatch.setattr(remote_cli.subprocess, "run", lambda *args, **kwargs: Result())
    return remote_cli._candidate_options(True)


def test_patch_guard_accepts_source_and_rejects_market_data(monkeypatch):
    source = (
        "diff --git a/packages/core/src/moex_spread_lab/core/w0.py b/packages/core/src/moex_spread_lab/core/w0.py\n"
        "--- a/packages/core/src/moex_spread_lab/core/w0.py\n"
        "+++ b/packages/core/src/moex_spread_lab/core/w0.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    assert _patch_options(monkeypatch, source)["patch"] == source
    for diff in (
        "diff --git a/data/w1/candles/X_1d.json b/data/w1/candles/X_1d.json\n",
        "diff --git a/data-lake/h1/seal.json b/data-lake/h1/seal.json\n",
    ):
        with pytest.raises(ValueError, match="market data"):
            _patch_options(monkeypatch, diff)
