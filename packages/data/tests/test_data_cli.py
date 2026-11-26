"""Smoke tests for the data package CLI command registration and dispatch."""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from moex_spread_lab.data import cli
from moex_spread_lab.data.cli import run

RETAINED = (
    "data-inventory",
    "storage-budget",
    "data-archive",
    "data-h1-seal",
    "data-import-candles",
    "data-refresh",
    "data-verify",
    "data-compact",
    "iss-intake",
    "iss-candles",
    "benchmark-lineage",
)

RETIRED = (
    "data-h1-stage",
    "data-h1-archive",
    "data-archive-candidates",
    "archive-catalog",
    "iss-timeframe-inventory",
    "iss-candle-pair-check",
    "data-pin-raw",
    "data-gc",
)

_NO_ARG_COMMANDS = ("data-inventory", "storage-budget", "data-verify", "data-compact")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    cli.register(subparsers)
    return parser


def test_retained_commands_register():
    parser = _build_parser()
    for command in _NO_ARG_COMMANDS:
        namespace = parser.parse_args([command])
        assert namespace.command == command


def test_retired_commands_are_not_registered():
    parser = _build_parser()
    for command in RETIRED:
        with pytest.raises(SystemExit):
            parser.parse_args([command])


def test_dispatch_table_matches_retained_commands():
    assert set(cli._DISPATCH) == set(RETAINED)


def test_run_dispatches_handler_and_falls_through():
    captured: dict[str, str] = {}

    def handler(args: Any) -> int:
        captured["command"] = args.command
        return 0

    with patch.dict(cli._DISPATCH, {"smoke-probe": handler}):
        assert run(SimpleNamespace(command="smoke-probe")) == 0
        assert captured["command"] == "smoke-probe"
    assert run(SimpleNamespace(command="no-such-command")) == 2
