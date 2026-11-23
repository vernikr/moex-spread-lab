"""Candidate patch option construction and the market-data guard."""

import pytest

from moex_spread_lab.core import remote_cli


def _patch_options(monkeypatch, diff_output):
    class Result:
        stdout = diff_output

    monkeypatch.setattr(remote_cli.subprocess, "run", lambda *args, **kwargs: Result())
    return remote_cli._candidate_options(True)


def test_patch_guard_rejects_market_data(monkeypatch):
    diff = (
        "diff --git a/data/w1/candles/X_1d.json b/data/w1/candles/X_1d.json\n"
        "index 1111111..2222222 100644\n"
        "--- a/data/w1/candles/X_1d.json\n"
        "+++ b/data/w1/candles/X_1d.json\n"
        '@@ -1 +1 @@\n-"a"\n+"b"\n'
    )
    with pytest.raises(ValueError, match="market data"):
        _patch_options(monkeypatch, diff)


def test_patch_guard_rejects_data_lake(monkeypatch):
    diff = "diff --git a/data-lake/h1/seal.json b/data-lake/h1/seal.json\n"
    with pytest.raises(ValueError, match="market data"):
        _patch_options(monkeypatch, diff)


def test_patch_guard_accepts_source_only(monkeypatch):
    diff = (
        "diff --git a/packages/core/src/moex_spread_lab/core/w0.py b/packages/core/src/moex_spread_lab/core/w0.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/packages/core/src/moex_spread_lab/core/w0.py\n"
        "+++ b/packages/core/src/moex_spread_lab/core/w0.py\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    options = _patch_options(monkeypatch, diff)
    assert options["patch"] == diff
