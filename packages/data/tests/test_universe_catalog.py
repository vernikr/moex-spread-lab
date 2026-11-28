"""Catalog boundary: Layer 0 keeps only generic identifiers and the concrete MOEX
catalogs have exactly one definition site under ``data/universe``."""

from __future__ import annotations

import ast
from pathlib import Path

from moex_spread_lab.core import universe as core_universe
from moex_spread_lab.data import universe as data_universe

REPO_ROOT = Path(__file__).resolve().parents[3]
CALENDAR_NAMES = {"ASSETS", "CROSS_EXPIRY_PAIRS", "SYNTHETIC_TRIANGLE_PAIRS", "contract_chain"}
CONCRETE_NAMES = (
    *sorted(CALENDAR_NAMES),
    "BASE_PAIRS",
    "EXTRA_PAIRS",
    "GLD_HISTORICAL_FAR",
    "GLD_HISTORICAL_PAIRS",
    "HISTORICAL_FAR_BY_NEAR",
    "HISTORICAL_PERPETUAL_PAIRS",
    "HISTORY_WINDOWS",
    "PAIRS",
    "PERPETUAL_HISTORY_STARTS",
    "PERPETUAL_HISTORY_TILL",
    "SCREEN_PAIRS",
)
CORE_CATALOG_NAMES = tuple(name for name in CONCRETE_NAMES if name not in CALENDAR_NAMES)


def _module_level_assignments(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        names.update(target.id for target in targets if isinstance(target, ast.Name))
    return names


def test_expired_quarter_grid_has_no_interior_gaps() -> None:
    # Listed endpoints bound the admissible expired maturities, not issue-date estimates.
    families = {
        "CNYRUBF": ("CR", "CRM2", "CRU6"),
        "GLDRUBF": ("GL", "GLU3", "GLU6"),
        "IMOEXF": ("MM", "MMZ3", "MMU6"),
        "GAZPF": ("GZ", "GZZ4", "GZU6"),
        "SBERF": ("SR", "SRZ4", "SRU6"),
        "RGBIF": ("RB", "RBH6", "RBU6"),
        "USDRUBF": ("Si", "SiM2", "SiU6"),
        "SLVRUBF": ("S2", "S2M6", "S2U6"),
        "EURRUBF": ("Eu", "EuM2", "EuU6"),
    }
    for near, (prefix, first, last) in families.items():
        grid = [f"{prefix}{quarter}{year}" for year in range(2, 7) for quarter in "HMUZ"]
        expected = set(grid[grid.index(first) : grid.index(last) + 1])
        actual = set(
            data_universe.GLD_HISTORICAL_FAR
            if near == "GLDRUBF"
            else data_universe.HISTORICAL_FAR_BY_NEAR[near]
        )
        assert actual == expected, (near, sorted(expected - actual), sorted(actual - expected))
    assert "BTCUSDF" not in data_universe.HISTORICAL_FAR_BY_NEAR


def test_data_universe_is_the_canonical_public_surface() -> None:
    for name in (*CONCRETE_NAMES, "PAIR_UNIVERSE", "CALENDAR_UNIVERSE"):
        assert name in data_universe.__all__, name
        assert getattr(data_universe, name) is not None, name


def test_layer_zero_keeps_only_generic_identifiers() -> None:
    calendar_contracts = "packages/strategies/src/moex_spread_lab/strategies/calendar/contracts.py"
    paths = [
        *sorted((REPO_ROOT / "packages/core/src").rglob("*.py")),
        REPO_ROOT / calendar_contracts,
    ]
    defined = set[str]().union(*(_module_level_assignments(path) for path in paths))
    offenders = sorted(defined & set(CONCRETE_NAMES))
    assert offenders == [], f"concrete MOEX catalogs defined outside msl-data: {offenders}"


def test_core_universe_exposes_no_concrete_catalogs() -> None:
    for name in (*CORE_CATALOG_NAMES, "STRATEGIES"):
        assert not hasattr(core_universe, name), name
        assert name not in core_universe.__all__, name
    namespace: dict[str, object] = {}
    exec("from moex_spread_lab.core.universe import *", namespace)
    for name in (*CONCRETE_NAMES, "STRATEGIES"):
        assert name not in namespace, name
