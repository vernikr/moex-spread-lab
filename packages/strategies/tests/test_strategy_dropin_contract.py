"""Built-in strategies are discovered from their folder, so two branches never collide.

The manifest deliberately declares no ``moex_spread_lab.strategies`` entry point for a
workspace strategy: ``discover_strategies()`` walks the subpackages instead, and entry
points stay reserved for external wheels installed outside ``msl-strategies``.
"""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

from moex_spread_lab.strategies import discover_strategies

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST = REPO_ROOT / "packages/strategies/pyproject.toml"

BUILTIN_SUBPACKAGES = {
    "mean_reversion": "pair_mean_reversion",
    "funding_only": "funding_carry",
    "zscore_mean_reversion": "calendar_zscore",
    "convergence_to_ruonia": "calendar_ruonia",
}


def test_manifest_registers_no_builtin_strategy_entry_point():
    project = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))["project"]
    assert "moex_spread_lab.strategies" not in project.get("entry-points", {})


def test_builtins_come_from_subpackages_not_from_entry_points():
    installed = {
        entry.name for entry in importlib.metadata.entry_points(group="moex_spread_lab.strategies")
    }
    assert not installed & set(BUILTIN_SUBPACKAGES)
    registry = discover_strategies()
    for strategy_id, subpackage in BUILTIN_SUBPACKAGES.items():
        module = type(registry.get(strategy_id)).__module__
        assert module == f"moex_spread_lab.strategies.{subpackage}.plugin"
