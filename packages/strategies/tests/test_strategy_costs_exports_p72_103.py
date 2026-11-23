"""Regression coverage for the additive cost export surface (P7.2 leaf #103).

The facade retains its private identities while wildcard import matches the
surface that existed before #103.
"""

from __future__ import annotations

import unittest

_PREVIOUS_WILDCARD_NAMES_P72_103 = frozenset(
    {
        "Any",
        "Callable",
        "CarryStrategyError",
        "Path",
        "W0Error",
        "annotations",
        "bisect_left",
        "cast",
        "make_benchmark_lookup",
        "read_hashed_json",
        "resolve_carry_cost_profile",
        "side_cost",
        "tariff_profile",
    }
)
_PRIVATE_FACADE_EXPORTS_P72_103 = (
    "_BENCHMARK_PARSED_CACHE",
    "_benchmark_rows",
    "_transaction_cost_profile",
)


class StrategyCostsDeclaredExportsP72_103Tests(unittest.TestCase):
    def test_declared_surface_is_previous_wildcard(self) -> None:
        from moex_spread_lab.strategies.funding_carry import strategy_costs

        expected = set(_PREVIOUS_WILDCARD_NAMES_P72_103)
        self.assertEqual(set(strategy_costs.__all__), expected)
        for name in sorted(expected):
            self.assertIn(name, vars(strategy_costs), name)

    def test_wildcard_import_exposes_all_declared_names(self) -> None:
        namespace: dict[str, object] = {}
        exec(
            "from moex_spread_lab.strategies.funding_carry.strategy_costs import *",
            namespace,
        )
        from moex_spread_lab.strategies.funding_carry import strategy_costs

        imported = {name for name in namespace if name != "__builtins__"}
        self.assertEqual(imported, set(strategy_costs.__all__))
        for name in _PRIVATE_FACADE_EXPORTS_P72_103:
            self.assertNotIn(name, namespace)

    def test_facade_keeps_origin_identity_for_private_exports(self) -> None:
        from moex_spread_lab.strategies.funding_carry import carry, strategy_costs

        for name in _PRIVATE_FACADE_EXPORTS_P72_103:
            self.assertIn(name, carry.__all__)
            self.assertIs(getattr(carry, name), getattr(strategy_costs, name))


if __name__ == "__main__":
    unittest.main()
