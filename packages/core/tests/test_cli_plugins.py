"""Entry-point extension of the CLI must not change supported command behavior."""

import argparse
from unittest.mock import patch

import pytest

from moex_spread_lab import cli
from moex_spread_lab.core.cli import register as register_core_cli
from moex_spread_lab.core.w0 import W0Error


class EntryPoint:
    def __init__(self, name, provider):
        self.name = name
        self.provider = provider

    def load(self):
        return self.provider


def test_plugin_adds_command_and_dispatches_with_arguments(capsys):
    def register(subparsers):
        parser = subparsers.add_parser("sample-plugin")
        parser.add_argument("--value", required=True)
        parser.set_defaults(_msl_cli_handler=lambda args: print(args.value) or 7)

    with patch.object(cli, "entry_points", return_value=[EntryPoint("sample", register)]):
        with patch("sys.argv", ["moex-spread-lab", "sample-plugin", "--value", "hello"]):
            assert cli.main() == 7
    assert capsys.readouterr().out == "hello\n"


def test_discovered_plugin_help_is_usable(capsys):
    def register(subparsers):
        parser = subparsers.add_parser("sample-plugin", help="sample plugin command")
        parser.add_argument("--value", required=True)

    with patch.object(
        cli, "entry_points", return_value=[EntryPoint("sample", register)]
    ) as discover:
        parser = cli.build_parser()
        with pytest.raises(SystemExit) as exit_info:
            parser.parse_args(["sample-plugin", "--help"])

    assert exit_info.value.code == 0
    assert "--value" in capsys.readouterr().out
    discover.assert_called_once_with(group="moex_spread_lab.cli")


def test_plugin_w0_error_keeps_legacy_exit_code(capsys):
    def register(subparsers):
        def fail(args):
            raise W0Error("bad input")

        subparsers.add_parser("sample-plugin").set_defaults(_msl_cli_handler=fail)

    with patch.object(cli, "entry_points", return_value=[EntryPoint("sample", register)]):
        with patch("sys.argv", ["moex-spread-lab", "sample-plugin"]):
            assert cli.main() == 2
    assert "W0 validation failed: bad input" in capsys.readouterr().out


def test_duplicate_plugin_command_is_rejected():
    def register(subparsers):
        subparsers.add_parser("shared-plugin-command")

    plugins = [EntryPoint("first", register), EntryPoint("second", register)]
    with (
        patch.object(cli, "entry_points", return_value=plugins),
        pytest.raises(argparse.ArgumentError, match="conflicting subparser"),
    ):
        cli.build_parser()


def test_core_replay_help_keeps_supported_arguments(capsys):
    with patch.object(
        cli, "entry_points", return_value=[EntryPoint("msl-core", register_core_cli)]
    ):
        parser = cli.build_parser()
        with pytest.raises(SystemExit) as exit_info:
            parser.parse_args(["replay", "--help"])

    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--manifest" in help_text
    assert "--fixture" in help_text
    assert "--output" in help_text
