"""Backward-compatibility shim for relocated ``moex_spread_lab.web.cli``."""

from moex_spread_lab.web.cli import add_screener_subcommand, register, run_screener

__all__ = ["add_screener_subcommand", "register", "run_screener"]
