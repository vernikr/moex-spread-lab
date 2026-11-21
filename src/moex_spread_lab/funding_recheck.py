"""Backward-compatible shim for ``moex_spread_lab.strategies.funding_carry.funding_recheck``."""

from moex_spread_lab.strategies.funding_carry.funding_recheck import *  # noqa: F401,F403

if __name__ == "__main__":
    from moex_spread_lab.strategies.funding_carry.funding_recheck import main as _main

    _main()
