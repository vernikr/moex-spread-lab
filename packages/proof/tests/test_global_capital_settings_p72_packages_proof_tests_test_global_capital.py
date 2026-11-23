"""Persisted capital settings: migration, validation, partial saves and repair."""

import math
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.core.settings import (
    MAX_RESEARCH_CAPITAL_RUB,
    ResearchSettings,
    read_settings,
    save_settings,
    settings_path,
)
from moex_spread_lab.core.w0 import W0Error


class CapitalSettingsTests(unittest.TestCase):
    def test_default_migration_and_partial_saves_preserve_all_other_preferences(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(read_settings(root)["initial_capital_rub"], 30000)
            self.assertFalse(settings_path(root).exists())
            path = settings_path(root)
            path.parent.mkdir(parents=True)
            legacy = '{"validation_percent":12.5,"allow_failed_volume":false,"allow_failed_capital":true,"slippage_ticks":2.5}\n'
            path.write_text(legacy)
            settings = read_settings(root)
            self.assertEqual(settings["initial_capital_rub"], 30000)
            self.assertEqual(path.read_text(), legacy)
            saved = save_settings(root, {"initial_capital_rub": 60000.25})
            self.assertEqual(saved, {**settings, "initial_capital_rub": 60000.25})
            save_settings(root, {"validation_percent": 25})
            save_settings(root, {"slippage_ticks": 3})
            self.assertEqual(read_settings(root)["initial_capital_rub"], 60000.25)
            self.assertFalse(read_settings(root)["allow_failed_volume"])

    def test_invalid_capital_never_replaces_the_saved_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_settings(root, {"initial_capital_rub": 12345.67})
            original = settings_path(root).read_bytes()
            for bad in (
                False,
                True,
                None,
                "30000",
                [],
                {},
                0,
                -1,
                0.001,
                123.456,
                math.nan,
                math.inf,
                -math.inf,
                10**400,
                MAX_RESEARCH_CAPITAL_RUB + 0.01,
            ):
                with self.subTest(value=bad), self.assertRaises(W0Error):
                    save_settings(root, {"initial_capital_rub": bad})
                self.assertEqual(settings_path(root).read_bytes(), original)
            for value in (0.01, 1, 15000, 60000.25, MAX_RESEARCH_CAPITAL_RUB):
                with self.subTest(valid=value):
                    self.assertEqual(
                        save_settings(root, {"initial_capital_rub": value})["initial_capital_rub"],
                        value,
                    )

    def test_complete_new_form_can_repair_corrupt_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = settings_path(root)
            path.parent.mkdir(parents=True)
            path.write_text("broken")
            with self.assertRaises(W0Error):
                read_settings(root)
            expected = ResearchSettings(initial_capital_rub=45000).metadata()
            self.assertEqual(save_settings(root, expected), expected)
            self.assertEqual(read_settings(root), expected)
