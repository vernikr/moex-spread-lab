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


def _plugins(*providers):
    entries = [EntryPoint(f"plugin{index}", provider) for index, provider in enumerate(providers)]
    return patch.object(cli, "entry_points", return_value=entries)


def test_discovered_plugin_is_helpable_and_dispatches_with_arguments(capsys):
    def register(subparsers):
        parser = subparsers.add_parser("sample-plugin", help="sample plugin command")
        parser.add_argument("--value", required=True)
        parser.set_defaults(_msl_cli_handler=lambda args: print(args.value) or 7)

    with _plugins(register) as discover:
        with pytest.raises(SystemExit) as exit_info:
            cli.build_parser().parse_args(["sample-plugin", "--help"])
        assert exit_info.value.code == 0 and "--value" in capsys.readouterr().out
        discover.assert_called_once_with(group="moex_spread_lab.cli")
        with patch("sys.argv", ["moex-spread-lab", "sample-plugin", "--value", "hello"]):
            assert cli.main() == 7
    assert capsys.readouterr().out == "hello\n"


def test_plugin_w0_error_keeps_legacy_exit_code(capsys):
    def register(subparsers):
        def fail(args):
            raise W0Error("bad input")

        subparsers.add_parser("sample-plugin").set_defaults(_msl_cli_handler=fail)

    with _plugins(register), patch("sys.argv", ["moex-spread-lab", "sample-plugin"]):
        assert cli.main() == 2
    assert "W0 validation failed: bad input" in capsys.readouterr().out


def test_duplicate_plugin_command_is_rejected():
    def register(subparsers):
        subparsers.add_parser("shared-plugin-command")

    with (
        _plugins(register, register),
        pytest.raises(argparse.ArgumentError, match="conflicting subparser"),
    ):
        cli.build_parser()


def test_core_validate_manifest_help_keeps_supported_arguments(capsys):
    with _plugins(register_core_cli), pytest.raises(SystemExit) as exit_info:
        cli.build_parser().parse_args(["validate-manifest", "--help"])
    assert exit_info.value.code == 0
    assert "--manifest" in capsys.readouterr().out
