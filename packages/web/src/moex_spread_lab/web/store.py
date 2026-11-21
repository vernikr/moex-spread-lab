"""Thread-safe local research store backing the loopback web server."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import threading
from typing import Any

from moex_spread_lab.proof_runtime.proof_pool import get_worker_pool
from moex_spread_lab.proof_runtime.service import ProofService
from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs


class ScreenerStore:
    def __init__(self, inputs: ScreenerInputs):
        self.inputs = inputs
        self.lock = threading.Lock()
        self.proof_search_lock = threading.Lock()
        self.proof_profile_lock = threading.Lock()
        self.proof_windows_lock = threading.Lock()
        self._calendar: Any = None
        self._proof: ProofService | None = None

    @property
    def proof(self) -> ProofService:
        if self._proof is None:
            self._proof = ProofService(
                self.inputs.root,
                calendar=self.calendar,
                pool_factory=get_worker_pool,
            )
        return self._proof

    @property
    def calendar(self) -> Any:
        if self._calendar is None:
            from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData

            self._calendar = CalendarData(self.inputs.root)
        return self._calendar

    def close(self) -> None:
        self.proof.close()

    @staticmethod
    def _recent(path: Path) -> list[dict[str, Any]]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in lines[-20:] if line]

    def list_runs(self) -> list[dict[str, Any]]:
        return self._recent(self.inputs.history)

    def list_searches(self) -> list[dict[str, Any]]:
        return self._recent(self.inputs.search_history)

    def _save(self, path: Path, result: dict[str, Any]) -> None:
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if any(run["run_id"] == result["run_id"] for run in self._recent(path)):
                return
            record = {**result, "recorded_at": datetime.now(UTC).isoformat()}
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                )

    def save(self, result: dict[str, Any]) -> None:
        self._save(self.inputs.history, result)

    def save_screen(self, result: dict[str, Any]) -> None:
        compact = {
            key: result[key] for key in ("run_id", "schema_version", "selection")
        }
        compact["pairs"] = [
            {key: row[key] for key in ("id", "status", "metrics", "evaluation") if key in row}
            for row in result["pairs"]
        ]
        self._save(self.inputs.screen_history, compact)

    def save_search(self, result: dict[str, Any]) -> None:
        self._save(self.inputs.search_history, result)

    def save_proof_search(self, result: dict[str, Any]) -> None:
        self._save(self.inputs.history.parent / "proof-searches.jsonl", result)
