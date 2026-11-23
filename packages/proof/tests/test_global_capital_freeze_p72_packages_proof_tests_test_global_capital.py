"""Capital is frozen before a table request loads any member, and search keeps it fixed."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moex_spread_lab.core.settings import read_settings, save_settings
from moex_spread_lab.proof_runtime.proof_search import StudyWindow, _tune, research_run
from moex_spread_lab.proof_runtime.proof_windows import window_summaries
from moex_spread_lab.proof_runtime.service import ProofService
from proof_fixtures import two_calendar_windows
from test_global_capital import CAL, PAIR, TARGETS


class CapitalFreezeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        two_calendar_windows(self.root)
        self.service = ProofService(self.root)

    def test_a_table_request_freezes_capital_before_loading_any_member(self):
        save_settings(self.root, {"initial_capital_rub": 20000})

        def change_settings(*args, **kwargs):
            save_settings(self.root, {"initial_capital_rub": 60000})
            return StudyWindow(*args)

        with patch(
            "moex_spread_lab.proof_runtime.proof_windows.StudyWindow", side_effect=change_settings
        ):
            result = window_summaries(self.service, {"requests": [PAIR, CAL]})
        self.assertEqual(result["initial_capital_rub"], 20000)
        self.assertTrue(all(r["selection"]["initial_capital_rub"] == 20000 for r in result["rows"]))
        self.assertEqual(read_settings(self.root)["initial_capital_rub"], 60000)

    def test_search_freezes_capital_and_fill_and_neither_is_an_optimization_axis(self):
        save_settings(self.root, {"initial_capital_rub": 20000})

        def change_settings(*args, **kwargs):
            save_settings(self.root, {"initial_capital_rub": 60000})
            return _tune(*args, **kwargs)

        with patch("moex_spread_lab.proof_runtime.proof_batch._tune", side_effect=change_settings):
            result = research_run(
                self.service,
                {**PAIR, "fill": "next_close", "targets": TARGETS},
                optimize=True,
            )
        self.assertEqual(result["status"], "maximum_found")
        self.assertEqual(result["group"]["initial_capital_rub"], 20000)
        self.assertEqual(set(result["selected_parameters"]), {"lookback", "entry_z", "exit_z"})
        for member in result["group"]["members"]:
            self.assertEqual(member["selection"]["fill"], "next_close")
            self.assertEqual(member["selection"]["initial_capital_rub"], 20000)
            self.assertEqual(member["evaluation"]["initial_capital_rub"], 20000)
        self.assertEqual(read_settings(self.root)["initial_capital_rub"], 60000)
