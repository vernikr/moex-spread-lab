"""Thread-safe bounded cache for file-derived values."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Hashable

from .cache_metrics import record_cache_lookup


class HashedJSONCache[Key: Hashable, Value]:
    """Small LRU for immutable file snapshots, typed by each cache's key/value."""

    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, capacity)
        self._data: OrderedDict[Key, Value] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Key) -> Value | None:
        with self._lock:
            entry = self._data.get(key)
            record_cache_lookup("json", entry is not None)
            if entry is not None:
                self._data.move_to_end(key)
            return entry

    def put(self, key: Key, value: Value) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._capacity:
                self._data.popitem(last=False)
