"""Tests for offline, isolated browser-smoke orchestration."""

from __future__ import annotations

import os
from pathlib import Path

from scripts import run_browser_smokes as runner


def _suite(directory: Path, name: str, text: str) -> None:
    (directory / f"{name}_smoke.py").write_text(text, encoding="utf-8")


def test_filter_offline_and_suppressed_success_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "SMOKE_DIR", tmp_path)
    monkeypatch.setenv("MOEX_OFFLINE_ONLY", "0")
    _suite(
        tmp_path,
        "first",
        "import os\nassert os.environ['MOEX_OFFLINE_ONLY'] == '1'\nprint('quiet')\n",
    )
    _suite(tmp_path, "second", "raise RuntimeError('should not run')\n")
    assert runner.main(["--filter", "first", "--jobs", "1"]) == 0
    output = capsys.readouterr().out
    assert "first_smoke.py ... PASS" in output
    assert "quiet" not in output and "second" not in output
    assert os.environ["MOEX_OFFLINE_ONLY"] == "0"


def test_failure_reports_traceback_and_other_suites_still_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "SMOKE_DIR", tmp_path)
    _suite(tmp_path, "broken", "raise RuntimeError('deliberate failure')\n")
    _suite(tmp_path, "working", "print('success')\n")
    assert runner.main(["--jobs", "1"]) == 1
    output = capsys.readouterr().out
    assert "broken_smoke.py ... FAIL" in output
    assert "Traceback" in output and "deliberate failure" in output
    assert "working_smoke.py ... PASS" in output
    assert "success" not in output


def test_no_matching_suite_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "SMOKE_DIR", tmp_path)
    assert runner.main(["--filter", "missing"]) == 1
    assert "No browser smoke" in capsys.readouterr().err


def test_cpu_limits_and_job_rejection(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.os, "cpu_count", lambda: 2)
    assert runner.cpu_limit() == 1
    monkeypatch.setattr(runner, "SMOKE_DIR", tmp_path)
    import pytest

    with pytest.raises(SystemExit) as exc:
        runner.main(["--jobs", "2"])
    assert exc.value.code == 2
