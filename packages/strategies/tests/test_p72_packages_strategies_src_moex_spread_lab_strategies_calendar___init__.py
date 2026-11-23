"""Facade contract of ``strategies.calendar`` under whole-package strict typing.

The package ``__init__`` re-exports ``calendar_spreads`` through its ``__all__``.
This locks the private facade hooks (``_positive_spread_volume_rows``,
``_stress_ok``) and the full public surface so the strict cleanup keeps every
historical dotted-path import resolvable.
"""

import pytest

import moex_spread_lab.strategies.calendar as calendar_pkg
from moex_spread_lab.strategies.calendar import calendar_spreads

PRIVATE_FACADE_NAMES = ("_positive_spread_volume_rows", "_stress_ok")


def test_private_facade_hooks_are_bound_and_identical():
    for name in PRIVATE_FACADE_NAMES:
        facade_value = getattr(calendar_pkg, name)
        assert facade_value is getattr(calendar_spreads, name), name
        assert callable(facade_value), name


def test_package_reexports_every_calendar_spreads_export():
    assert calendar_spreads.__all__
    for name in calendar_spreads.__all__:
        assert getattr(calendar_pkg, name) is getattr(calendar_spreads, name), name


@pytest.mark.parametrize("name", PRIVATE_FACADE_NAMES)
def test_facade_hook_survives_module_reload(name):
    import importlib

    reloaded = importlib.reload(calendar_pkg)
    assert getattr(reloaded, name) is getattr(calendar_spreads, name)
