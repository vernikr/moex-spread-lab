"""Entry-point extension of the CLI must not change legacy command behavior."""

import argparse
from unittest.mock import patch

import pytest

from moex_spread_lab import cli
from moex_spread_lab.w0 import W0Error


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
        assert "sample-plugin" in cli.build_parser().format_help()
    assert capsys.readouterr().out == "hello\n"


def test_plugin_w0_error_keeps_legacy_exit_code(capsys):
    def register(subparsers):
        def fail(args):
            raise W0Error("bad input")
        subparsers.add_parser("sample-plugin").set_defaults(_msl_cli_handler=fail)

    with patch.object(cli, "entry_points", return_value=[EntryPoint("sample", register)]):
        with patch("sys.argv", ["moex-spread-lab", "sample-plugin"]):
            assert cli.main() == 2
    assert "W0 validation failed: bad input" in capsys.readouterr().out


def test_duplicate_command_is_rejected():
    def register(subparsers):
        subparsers.add_parser("replay")

    builtins = list(cli.entry_points(group="moex_spread_lab.cli"))
    with patch.object(cli, "entry_points", return_value=[*builtins, EntryPoint("duplicate", register)]):
        with pytest.raises(argparse.ArgumentError, match="conflicting subparser"):
            cli.build_parser()


def test_existing_command_help_is_byte_identical():
    import hashlib
    import json
    from pathlib import Path
    import subprocess
    import sys

    baseline = json.loads((Path(__file__).parent / "cli_help_hashes.json").read_text())
    for command, expected in baseline.items():
        result = subprocess.run(
            [sys.executable, "-m", "moex_spread_lab.cli", command, "--help"],
            capture_output=True,
            check=True,
        )
        assert hashlib.sha256(result.stdout).hexdigest() == expected, command
        assert not result.stderr, command
