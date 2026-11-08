import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from moex_spread_lab.screener_app import ScreenerError, ScreenerInputs
from moex_spread_lab.screener_search import optimize


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.inputs = ScreenerInputs(Path(self.temp.name))
        self.pairs = ["CNYRUBF/CRZ6", "GLDRUBF/GLZ6"]
        self.available = {
            "pairs": [
                {
                    "id": pair,
                    "strategies": {"mean_reversion": True, "funding_only": True},
                }
                for pair in self.pairs
            ]
        }

    def evaluator(self, _inputs, selection, *, positive=True):
        parameters = selection["parameters"]
        spike = parameters == {"lookback": 20, "entry_z": 2, "exit_z": 0.5}
        advantage = 0.0015 if spike else (0.0004 if positive else -0.0002)
        if selection["pair"] == "GLDRUBF/GLZ6":
            advantage *= 0.9
        peak = 0
        rows = []
        for index in range(150):
            day = (date(2026, 1, 1) + timedelta(days=index)).isoformat()
            benchmark = 30000 * (1 + index * 0.0001)
            strategy = 30000 * (1 + index * (0.0001 + advantage))
            excess = strategy - benchmark
            peak = max(peak, excess)
            rows.append(
                {
                    "date": day,
                    "V": strategy,
                    "B": benchmark,
                    "X": excess,
                    "D": peak - excess,
                }
            )
        return {"daily": rows}

    def test_isolated_peak_is_not_selected_over_plateau(self):
        request = {
            "pairs": self.pairs,
            "strategy": "mean_reversion",
            "slippage_ticks": 1.0,
            "parameters": {"lookback": 20, "entry_z": 2, "exit_z": 0.5},
        }
        result = optimize(
            self.inputs, request, evaluator=self.evaluator, available=self.available
        )
        self.assertEqual(result["status"], "exploratory_plateau_found")
        self.assertEqual(len(result["trials"]), 27)
        self.assertNotEqual(result["selected_parameters"], request["parameters"])
        self.assertGreaterEqual(len(result["stable_neighbors"]), 3)
        self.assertEqual(set(result["confirmation_by_pair_pp"]), set(self.pairs))
        self.assertEqual(
            result,
            optimize(
                self.inputs, request, evaluator=self.evaluator, available=self.available
            ),
        )
        self.assertEqual(
            result["scope"], "independent_pair_studies_not_a_shared_capital_basket"
        )

    def test_confirmation_change_does_not_select_parameters(self):
        request = {
            "pairs": self.pairs,
            "strategy": "mean_reversion",
            "parameters": {"lookback": 20, "entry_z": 2, "exit_z": 0.5},
        }
        first = optimize(
            self.inputs, request, evaluator=self.evaluator, available=self.available
        )

        def changed_confirmation(inputs, selection):
            result = self.evaluator(inputs, selection)
            for row in result["daily"][120:]:
                row["V"] += 2000 if selection["parameters"]["lookback"] == 20 else -2000
            return result

        second = optimize(
            self.inputs,
            request,
            evaluator=changed_confirmation,
            available=self.available,
        )
        self.assertEqual(first["selected_parameters"], second["selected_parameters"])
        self.assertNotEqual(
            first["confirmation_by_pair_pp"], second["confirmation_by_pair_pp"]
        )
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_no_positive_region_and_incompatible_pair(self):
        request = {
            "pairs": self.pairs,
            "strategy": "funding_only",
            "parameters": {"lookback": 20},
        }
        result = optimize(
            self.inputs,
            request,
            evaluator=lambda inputs, selection: self.evaluator(
                inputs, selection, positive=False
            ),
            available=self.available,
        )
        self.assertEqual(result["status"], "no_stable_positive_plateau")
        self.assertIsNone(result["selected_parameters"])
        self.assertIsNone(result["confirmation_by_pair_pp"])
        self.assertEqual(len(result["trials"]), 5)
        unavailable = {
            "pairs": [
                {"id": self.pairs[0], "strategies": {"funding_only": True}},
                {"id": self.pairs[1], "strategies": {"funding_only": False}},
            ]
        }
        with self.assertRaises(ScreenerError):
            optimize(
                self.inputs, request, evaluator=self.evaluator, available=unavailable
            )

    def test_common_history_gate(self):
        def short(_inputs, selection):
            rows = self.evaluator(_inputs, selection)["daily"][:50]
            return {"daily": rows}

        with self.assertRaisesRegex(ScreenerError, "common dates"):
            optimize(
                self.inputs,
                {"pairs": self.pairs, "strategy": "funding_only"},
                evaluator=short,
                available=self.available,
            )


if __name__ == "__main__":
    unittest.main()
