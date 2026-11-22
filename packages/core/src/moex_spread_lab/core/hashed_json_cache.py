"""Thread-safe bounded cache for hashed JSON input files."""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

from .cache_metrics import record_cache_lookup


class HashedJSONCache:
    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, capacity)
        self._data: OrderedDict[tuple[str, int, int], tuple[dict[str, Any], str]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[str, int, int]) -> tuple[dict[str, Any], str] | None:
        with self._lock:
            entry = self._data.get(key)
            record_cache_lookup("json", entry is not None)
            if entry is not None:
                self._data.move_to_end(key)
            return entry

    def put(self, key: tuple[str, int, int], value: tuple[dict[str, Any], str]) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._capacity:
                self._data.popitem(last=False)
