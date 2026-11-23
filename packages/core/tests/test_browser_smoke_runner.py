"""Tests for offline, isolated browser-smoke orchestration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from scripts import run_browser_smokes as runner


def _suite(directory: Path, name: str, text: str) -> None:
    (directory / f"{name}_smoke.py").write_text(text, encoding="utf-8")


class _FakeBrowser:
    """A stand-in browser: the shared object is passed to every scenario."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakePlaywright:
    """Stands in for `sync_playwright()`: the tests never start Chromium."""

    def __init__(self) -> None:
        self.launches = 0
        self.browser = _FakeBrowser()

    def __enter__(self) -> _FakePlaywright:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def fake_playwright(monkeypatch: pytest.MonkeyPatch) -> _FakePlaywright:
    fake = _FakePlaywright()
    monkeypatch.setattr(runner, "_playwright", lambda: fake)

    def launch(pw: Any) -> Any:
        fake.launches += 1
        fake.browser = _FakeBrowser()  # one shared browser object per worker
        return fake.browser

    monkeypatch.setattr(runner, "_launch", launch)
    return fake


def test_filter_offline_and_suppressed_success_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "SMOKE_DIR", tmp_path)
    monkeypatch.setenv("MOEX_OFFLINE_ONLY", "0")
    _suite(
        tmp_path,
        "first",
        "import os\nassert os.environ['MOEX_OFFLINE_ONLY'] == '1'\nprint('quiet')\n",
    )
    _suite(tmp_path, "second", "raise RuntimeError('should not run')\n")
    assert runner.main(["--filter", "first", "--jobs", "1", "--no-timings"]) == 0
    output = capsys.readouterr().out
    assert "first_smoke.py ... PASS" in output
    assert "quiet" not in output and "second" not in output
    assert os.environ["MOEX_OFFLINE_ONLY"] == "0"


def test_failure_reports_traceback_and_other_suites_still_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "SMOKE_DIR", tmp_path)
    _suite(tmp_path, "broken", "raise RuntimeError('deliberate failure')\n")
    _suite(tmp_path, "working", "print('success')\n")
    assert runner.main(["--jobs", "1", "--no-timings"]) == 1
    output = capsys.readouterr().out
    assert "broken_smoke.py ... FAIL" in output
    assert "Traceback" in output and "deliberate failure" in output
    assert "working_smoke.py ... PASS" in output
    assert "success" not in output


def test_one_browser_per_worker_and_one_scenario_call_per_suite(
    tmp_path, monkeypatch, capsys, fake_playwright
):
    """S4b: Chromium is launched once per worker, each suite exposes a callable entry."""
    monkeypatch.setattr(runner, "SMOKE_DIR", tmp_path)
    marker = tmp_path / "calls.txt"
    scenario = f"""
MESSAGE = "{tmp_path.name}: OK"

def scenario(browser):
    assert browser is not None
    with open({str(marker)!r}, "a", encoding="utf-8") as handle:
        handle.write("scenario\\n")
"""
    for name in ("alpha", "beta", "gamma"):
        _suite(tmp_path, name, scenario)
    assert runner.main(["--jobs", "1", "--no-timings"]) == 0
    output = capsys.readouterr().out
    assert output.count("... PASS") == 3
    assert fake_playwright.launches == 1
    assert fake_playwright.browser.closed  # the worker closes its single browser
    assert marker.read_text(encoding="utf-8").split() == ["scenario"] * 3


def test_plain_script_suites_do_not_launch_a_browser(tmp_path, monkeypatch, fake_playwright):
    monkeypatch.setattr(runner, "SMOKE_DIR", tmp_path)
    monkeypatch.setenv("MOEX_OFFLINE_ONLY", "0")  # importing the harness forces it for suites
    _suite(tmp_path, "plain", "print('script only')\n")
    results = runner.run_worker([tmp_path / "plain_smoke.py"])
    assert [result["code"] for result in results] == [0]
    assert fake_playwright.launches == 0


def test_phase_line_is_aggregated_and_parsed(tmp_path, monkeypatch, capsys, fake_playwright):
    monkeypatch.setattr(runner, "SMOKE_DIR", tmp_path)
    _suite(
        tmp_path,
        "inproc",
        "def scenario(browser):\n    print('MOEX_SMOKE_PHASES=' + '{\"total\": 1.0, \"api_compute\": 0.5}')\n",
    )
    _suite(
        tmp_path,
        "child",
        "print('MOEX_SMOKE_PHASES=' + '{\"total\": 2.0, \"ui_assertions\": 2.0}')\n",
    )
    assert runner.main(["--jobs", "1"]) == 0
    output = capsys.readouterr().out
    assert "Phase timings" in output
    assert "child_smoke.py" in output and "2.0" in output


def test_no_matching_suite_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "SMOKE_DIR", tmp_path)
    assert runner.main(["--filter", "missing"]) == 1
    assert "No browser smoke" in capsys.readouterr().err


def test_cpu_limits_and_job_rejection(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.os, "cpu_count", lambda: 2)
    assert runner.cpu_limit() == 1
    monkeypatch.setattr(runner, "SMOKE_DIR", tmp_path)

    with pytest.raises(SystemExit) as exc:
        runner.main(["--jobs", "2"])
    assert exc.value.code == 2


def test_partition_round_robins():
    paths = [Path(f"{index}_smoke.py") for index in range(5)]
    assert runner._partition(paths, 1) == [paths]
    assert runner._partition(paths, 2) == [paths[0::2], paths[1::2]]


def test_child_suite_exit_code_is_honoured(tmp_path, monkeypatch, capsys, fake_playwright):
    """A self-re-executing suite reports its child's status, not a silent failure."""
    monkeypatch.setattr(runner, "SMOKE_DIR", tmp_path)
    _suite(tmp_path, "childok", "raise SystemExit(0)\n")
    _suite(tmp_path, "childfail", "raise SystemExit(3)\n")
    assert runner.main(["--jobs", "1", "--no-timings"]) == 1
    output = capsys.readouterr().out
    assert "childok_smoke.py ... PASS" in output
    assert "childfail_smoke.py ... FAIL" in output
