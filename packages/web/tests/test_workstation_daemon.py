"""Safe installation and termination of the macOS launch agent."""

import argparse
import plistlib
import signal
from unittest.mock import Mock

from moex_spread_lab.web import cli
from moex_spread_lab.web import workstation_daemon as daemon


def test_dry_run_never_writes(tmp_path, capsys):
    assert daemon.install(tmp_path / "checkout with spaces", apply=False, home=tmp_path) == 0
    assert not (tmp_path / "Library").exists()
    assert "checkout with spaces" in capsys.readouterr().out


def test_definition_uses_checkout_interpreter_and_loopback_server(tmp_path):
    config = daemon.agent_definition(tmp_path, tmp_path)
    assert config["ProgramArguments"] == [
        str(tmp_path / ".venv/bin/python3"),
        "-m",
        "moex_spread_lab.cli",
        "screener",
        "--root",
        str(tmp_path),
        "--port",
        "8765",
    ]
    assert config["KeepAlive"] is True
    assert plistlib.loads(plistlib.dumps(config)) == config


def test_apply_refuses_existing_plist_and_port(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    path = tmp_path / "Library/LaunchAgents" / f"{daemon.LABEL}.plist"
    path.parent.mkdir(parents=True)
    path.write_text("existing")
    assert daemon.install(tmp_path, apply=True, home=tmp_path) == 1
    assert path.read_text() == "existing"
    path.unlink()
    python = tmp_path / ".venv/bin/python3"
    python.parent.mkdir(parents=True)
    python.write_text("")
    sock = Mock()
    sock.__enter__ = Mock(return_value=sock)
    sock.__exit__ = Mock(return_value=False)
    sock.connect_ex.return_value = 0
    monkeypatch.setattr(daemon.socket, "socket", lambda: sock)
    assert daemon.install(tmp_path, apply=True, home=tmp_path) == 1
    assert not path.exists()


def test_apply_bootstrap_and_rollback(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    python = tmp_path / ".venv/bin/python3"
    python.parent.mkdir(parents=True)
    python.write_text("")
    sock = Mock()
    sock.__enter__ = Mock(return_value=sock)
    sock.__exit__ = Mock(return_value=False)
    sock.connect_ex.return_value = 1
    monkeypatch.setattr(daemon.socket, "socket", lambda: sock)
    result = Mock(returncode=1, stderr="failed")
    monkeypatch.setattr(daemon.subprocess, "run", lambda *a, **kw: result)
    path = tmp_path / "Library/LaunchAgents" / f"{daemon.LABEL}.plist"
    assert daemon.install(tmp_path, apply=True, home=tmp_path) == 1
    assert not path.exists()
    result.returncode = 0
    assert daemon.install(tmp_path, apply=True, home=tmp_path) == 0
    assert plistlib.loads(path.read_bytes())["Label"] == daemon.LABEL


def test_sigterm_exits_cleanly_and_restores_handler(tmp_path, monkeypatch):
    from moex_spread_lab.web import server

    old_handler = signal.getsignal(signal.SIGTERM)

    def serve(*args):
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(server, "serve", serve)
    assert cli.run(argparse.Namespace(command="screener", root=tmp_path, port=8765)) == 0
    assert signal.getsignal(signal.SIGTERM) == old_handler
