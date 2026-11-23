"""Isolated regression for the supported ``axis.ordered`` export and facade identity.

``axis.ordered`` is the public alias of the historical private ``axis._ordered``
predicate that ``landscape`` and the ``proof_kernel`` / ``proof_search`` /
``proof_batch`` / ``request_validation`` facades keep binding and patching under
the old private name. These tests lock the export surface and the object identity
across that chain; wildcard imports retain their pre-change surface.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from moex_spread_lab.proof_runtime import (
    axis,
    landscape,
    proof_batch,
    proof_kernel,
    proof_search,
    request_validation,
)

_BELOW = {"entry_z": 2.5, "exit_z": 0.5}
_ABOVE = {"entry_z": 0.5, "exit_z": 2.5}


def test_ordered_is_a_public_alias_of_the_private_predicate() -> None:
    assert axis.ordered is axis._ordered
    assert axis._ordered.__name__ == "_ordered"
    assert axis.ordered.__name__ == "_ordered"
    assert axis.ordered.__qualname__ == "_ordered"
    assert axis.ordered.__module__ == axis.__name__
    assert axis.ordered.__doc__


def test_ordered_keeps_predicate_results() -> None:
    assert axis.ordered({}) is True
    assert axis.ordered({"window": 10}) is True
    assert axis.ordered({"entry_z": 2.0, "exit_z": 0.5}) is True
    assert axis.ordered({"exit_z": 0.5}) is True
    assert axis.ordered({"entry_z": 2.0}) is True
    assert axis.ordered(_BELOW) is True
    assert axis.ordered(_ABOVE) is False
    assert axis.ordered({"entry_z": 1.0, "exit_z": 1.0}) is False
    assert axis.ordered({"entry_z": 0.5, "exit_z": 2.5, "window": 10}) is False


def test_facade_chain_keeps_one_object_under_the_private_name() -> None:
    assert landscape._ordered is axis._ordered
    assert proof_kernel._ordered is axis._ordered
    assert proof_search._ordered is axis._ordered
    assert proof_batch._ordered is axis._ordered
    assert request_validation._ordered is axis._ordered
    assert landscape._ordered is axis.ordered


def test_star_import_surface_is_unchanged() -> None:
    namespace: dict[str, Any] = {}
    exec("from moex_spread_lab.proof_runtime.axis import *", namespace)
    assert set(namespace) - {"__builtins__"} == {
        "Axis",
        "Mapping",
        "Protocol",
        "annotations",
        "dataclass",
        "math",
    }
    assert namespace["Axis"] is axis.Axis
    assert namespace["Mapping"] is Mapping
    assert axis.ordered is axis._ordered
    assert axis._ordered.__name__ == "_ordered"


@pytest.mark.parametrize("values", [_BELOW, _ABOVE, {"window": 10}, {}])
def test_facades_and_public_alias_agree(values: dict[str, float]) -> None:
    expected = axis.ordered(values)
    assert axis._ordered(values) is expected
    assert landscape._ordered(values) is expected
    assert proof_kernel._ordered(values) is expected
    assert proof_search._ordered(values) is expected
    assert proof_batch._ordered(values) is expected
    assert request_validation._ordered(values) is expected
