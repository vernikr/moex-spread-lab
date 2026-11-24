"""Lightweight local proxies describing remote cohort members."""

from dataclasses import dataclass
from typing import Any


@dataclass
class _TrainingSlice:
    first: str
    last: str
    count: int

    def __len__(self) -> int:
        return self.count

    def __getitem__(self, idx: int) -> str:
        if idx == 0:
            return self.first
        if idx == -1 or idx == self.count - 1:
            return self.last
        raise IndexError(idx)


@dataclass
class _RemoteSplit:
    training: _TrainingSlice


@dataclass
class _RemoteCase:
    request: dict[str, Any]
    strategy_axes: dict[str, Any]
    execution_axes: dict[str, Any]
    axes: dict[str, Any]
    seed: dict[str, Any]
    split: _RemoteSplit
    _lamp_warning: str | None

    def lamp_warning(self):
        return self._lamp_warning
