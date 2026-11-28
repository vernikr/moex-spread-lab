"""Parity and safety tests for the native ``clean_obj`` kernel vs the pure-Python fallback.

Covers the #312 regressions: tuple/namedtuple/dict-subclass container normalization,
and native stack overflow on cyclic or deeply nested structures.
"""

from __future__ import annotations

import math
import os
from collections import OrderedDict, defaultdict, namedtuple
from typing import Any
from unittest.mock import patch

import pytest

from moex_spread_lab.core import _fastcore_loader as loader

_NAN = float("nan")
Point = namedtuple("Point", "x y")


def _native() -> Any:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MOEX_PROOF_DISABLE_C", None)
        loader._CACHE.pop(False, None)
        fc = loader.load_fastcore()
        assert fc is not None, "_fastcore extension must load for native parity tests"
        return fc


def _fallback() -> Any:
    with patch.dict(os.environ, {"MOEX_PROOF_DISABLE_C": "1"}, clear=False):
        loader._CACHE.pop(False, None)
        assert loader.load_fastcore() is None
        return None


def _clean_native(value: Any) -> Any:
    fc = _native()
    return fc.clean_obj(value)


def _clean_fallback(value: Any) -> Any:
    with patch.dict(os.environ, {"MOEX_PROOF_DISABLE_C": "1"}, clear=False):
        loader._CACHE.pop(False, None)
        assert loader.load_fastcore() is None
        from moex_spread_lab.core._fastcore_loader import clean_obj as _fallback_clean

        return _fallback_clean(value)


def _assert_same_shape(native: Any, fallback: Any, path: str = "$") -> None:
    assert type(native) is type(fallback), (
        f"type mismatch at {path}: {type(native)!r} != {type(fallback)!r}"
    )
    if isinstance(native, dict):
        assert list(native) == list(fallback), f"key mismatch at {path}"
        for key in native:
            _assert_same_shape(native[key], fallback[key], f"{path}.{key}")
    elif isinstance(native, list):
        assert len(native) == len(fallback), f"length mismatch at {path}"
        for index, (a, b) in enumerate(zip(native, fallback, strict=True)):
            _assert_same_shape(a, b, f"{path}[{index}]")
    elif isinstance(native, float):
        if math.isnan(native):
            assert math.isnan(fallback)
        else:
            assert native == fallback
    else:
        assert native == fallback, f"value mismatch at {path}"


@pytest.mark.parametrize(
    "payload",
    [
        {"t": (1.0, _NAN, 3.0), "l": [1, _NAN, "x"], "n": Point(1.0, float("inf"))},
        OrderedDict([("a", 1.0), ("b", _NAN)]),
        defaultdict(int, {"a": 1, "b": _NAN}),
        [{"deep": [Point(_NAN, 2.0), (3.0, (4.0, _NAN))]}],
        {"boolish": [True, False, 1, None]},
    ],
)
def test_clean_obj_native_matches_fallback_shape(payload: Any) -> None:
    try:
        native = _clean_native(payload)
    finally:
        loader._CACHE.pop(False, None)
    try:
        fallback = _clean_fallback(payload)
    finally:
        loader._CACHE.pop(False, None)
    _assert_same_shape(native, fallback)


def test_tuple_becomes_list_in_both_implementations() -> None:
    native = _clean_native((1.0, _NAN))
    fallback = _clean_fallback((1.0, _NAN))
    assert isinstance(native, list) and isinstance(fallback, list)
    assert native == fallback == [1.0, None]


def test_namedtuple_loses_type_in_both_implementations() -> None:
    native = _clean_native(Point(1.0, _NAN))
    fallback = _clean_fallback(Point(1.0, _NAN))
    assert type(native) is type(fallback) is list
    assert native == fallback == [1.0, None]


def test_cyclic_structure_raises_recursion_error_not_segfault() -> None:
    native_payload: list[Any] = []
    native_payload.append(native_payload)
    with pytest.raises(RecursionError):
        _clean_native(native_payload)
    fallback_payload: list[Any] = []
    fallback_payload.append(fallback_payload)
    with pytest.raises(RecursionError):
        _clean_fallback(fallback_payload)


def test_deeply_nested_structure_raises_recursion_error_not_segfault() -> None:
    native_payload: Any = 0.0
    for _ in range(10000):
        native_payload = [native_payload]
    with pytest.raises(RecursionError):
        _clean_native(native_payload)
    fallback_payload: Any = 0.0
    for _ in range(10000):
        fallback_payload = [fallback_payload]
    with pytest.raises(RecursionError):
        _clean_fallback(fallback_payload)
