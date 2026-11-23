"""Regression coverage for ``proof_runtime.cli`` (P7.2 strict, issue #77).

Unique-name suffix: ``p72_packages_proof_src_moex_spread_lab_proof_runtime_cli``

These tests lock in the existing CLI contract (parser wiring, command dispatch,
and return codes) without changing any runtime behavior.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import moex_spread_lab.proof_runtime.cli as cli

__all__ = [
    "test_register_wires_audit_pairs_handler_p72_packages_proof_src_moex_spread_lab_proof_runtime_cli",
    "test_run_audit_pairs_dispatch_p72_packages_proof_src_moex_spread_lab_proof_runtime_cli",
    "test_run_unknown_command_returns_2_p72_packages_proof_src_moex_spread_lab_proof_runtime_cli",
]


def test_register_wires_audit_pairs_handler_p72_packages_proof_src_moex_spread_lab_proof_runtime_cli():
    parser = argparse.ArgumentParser(prog="msl-proof")
    subparsers = parser.add_subparsers(dest="command")
    cli.register(subparsers)

    args = parser.parse_args(["audit-pairs", "--root", "/tmp/root", "--output", "/tmp/out.json"])

    assert args.command == "audit-pairs"
    assert args._msl_cli_handler is cli.run


def test_run_unknown_command_returns_2_p72_packages_proof_src_moex_spread_lab_proof_runtime_cli():
    assert cli.run(SimpleNamespace(command="unknown")) == 2


def test_run_audit_pairs_dispatch_p72_packages_proof_src_moex_spread_lab_proof_runtime_cli(
    monkeypatch,
):
    import moex_spread_lab.proof_runtime.audit as audit_mod
    import moex_spread_lab.strategies.pair_screener.pair_screener as screener_mod

    captured = {}

    def fake_audit_pairs(inputs):
        captured["inputs"] = inputs
        return {"summary": {"pairs": 3}}

    def fake_screener_inputs(root):
        captured["root"] = root
        return root

    monkeypatch.setattr(audit_mod, "audit_pairs", fake_audit_pairs)
    monkeypatch.setattr(screener_mod, "ScreenerInputs", fake_screener_inputs)

    args = SimpleNamespace(
        command="audit-pairs",
        root=SimpleNamespace(resolve=lambda: "/resolved/root"),
        output=Path("/tmp/out.json"),
    )

    assert cli.run(args) == 0
    assert captured["root"] == "/resolved/root"
    assert captured["inputs"] == "/resolved/root"
