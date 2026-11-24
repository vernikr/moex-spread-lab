"""The MOEX instrument/pair catalog lives in ``msl-data`` (issue #16, F5).

Guards the domain boundary: Layer 0 keeps only generic universe identifiers, the
concrete catalogs have exactly one definition site under ``data/universe``, and
the deprecated ``core.universe`` accessors keep resolving to those same objects
during the migration window.
"""

from __future__ import annotations

import ast
from pathlib import Path

from moex_spread_lab.core import universe as core_universe
from moex_spread_lab.data import universe as data_universe

REPO_ROOT = Path(__file__).resolve().parents[3]

CONCRETE_NAMES = (
    "ASSETS",
    "BASE_PAIRS",
    "CROSS_EXPIRY_PAIRS",
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
    "SYNTHETIC_TRIANGLE_PAIRS",
    "contract_chain",
)

# The subset that historically lived in ``moex_spread_lab.core.universe``.
CORE_CATALOG_NAMES = tuple(
    name
    for name in CONCRETE_NAMES
    if name
    not in {
        "ASSETS",
        "CROSS_EXPIRY_PAIRS",
        "SYNTHETIC_TRIANGLE_PAIRS",
        "contract_chain",
    }
)


def _module_level_assignments(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def test_data_universe_is_the_canonical_public_surface() -> None:
    for name in CONCRETE_NAMES:
        assert name in data_universe.__all__, name
        assert getattr(data_universe, name) is not None, name
    for name in ("PAIR_UNIVERSE", "CALENDAR_UNIVERSE"):
        assert name in data_universe.__all__, name


def test_layer_zero_keeps_only_generic_identifiers() -> None:
    core_source = REPO_ROOT / "packages" / "core" / "src"
    contracts = (
        REPO_ROOT
        / "packages"
        / "strategies"
        / "src"
        / "moex_spread_lab"
        / "strategies"
        / "calendar"
        / "contracts.py"
    )
    defined: set[str] = set()
    for path in sorted(core_source.rglob("*.py")):
        defined |= _module_level_assignments(path)
    defined |= _module_level_assignments(contracts)
    offenders = sorted(defined.intersection(CONCRETE_NAMES))
    assert offenders == [], f"concrete MOEX catalogs defined outside msl-data: {offenders}"


def test_deprecated_core_accessors_resolve_to_the_same_objects() -> None:
    for name in CORE_CATALOG_NAMES:
        assert getattr(core_universe, name) is getattr(data_universe, name), name


def test_star_import_from_core_still_carries_the_catalog() -> None:
    namespace: dict[str, object] = {}
    exec("from moex_spread_lab.core.universe import *", namespace)
    for name in CORE_CATALOG_NAMES:
        assert namespace[name] is getattr(data_universe, name), name
