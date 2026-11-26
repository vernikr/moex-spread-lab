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


@pytest.fixture(autouse=True)
def _smoke_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runner, "SMOKE_DIR", tmp_path)


@pytest.fixture
def fake_playwright(monkeypatch: pytest.MonkeyPatch) -> _FakePlaywright:
    fake = _FakePlaywright()
    monkeypatch.setattr(runner, "_playwright", lambda: fake)

    def launch(_playwright: Any) -> _FakeBrowser:
        fake.launches += 1
        fake.browser = _FakeBrowser()  # one shared browser object per worker
        return fake.browser

    monkeypatch.setattr(runner, "_launch", launch)
    return fake


def test_filter_offline_env_and_quiet_success_output(tmp_path, monkeypatch, capsys):
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
    assert runner.main(["--filter", "missing"]) == 1
    assert "No browser smoke" in capsys.readouterr().err


def test_failure_reports_traceback_and_other_suites_still_run(tmp_path, capsys):
    _suite(tmp_path, "broken", "raise RuntimeError('deliberate failure')\n")
    _suite(tmp_path, "working", "print('success')\n")
    assert runner.main(["--jobs", "1", "--no-timings"]) == 1
    output = capsys.readouterr().out
    assert "broken_smoke.py ... FAIL" in output
    assert "Traceback" in output and "deliberate failure" in output
    assert "working_smoke.py ... PASS" in output
    assert "success" not in output


def test_one_browser_per_worker_and_none_for_plain_scripts(
    tmp_path, monkeypatch, capsys, fake_playwright
):
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
    assert capsys.readouterr().out.count("... PASS") == 3
    assert fake_playwright.launches == 1
    assert fake_playwright.browser.closed  # the worker closes its single browser
    assert marker.read_text(encoding="utf-8").split() == ["scenario"] * 3

    monkeypatch.setenv("MOEX_OFFLINE_ONLY", "0")  # importing the harness forces it for suites
    _suite(tmp_path, "plain", "print('script only')\n")
    results = runner.run_worker([tmp_path / "plain_smoke.py"])
    assert [result["code"] for result in results] == [0]
    assert fake_playwright.launches == 1  # a plain script never launches Chromium


def test_child_suite_exit_code_is_honoured(tmp_path, capsys, fake_playwright):
    """A self-re-executing suite reports its child's status, not a silent failure."""
    _suite(tmp_path, "childok", "raise SystemExit(0)\n")
    _suite(tmp_path, "childfail", "raise SystemExit(3)\n")
    assert runner.main(["--jobs", "1", "--no-timings"]) == 1
    output = capsys.readouterr().out
    assert "childok_smoke.py ... PASS" in output
    assert "childfail_smoke.py ... FAIL" in output
