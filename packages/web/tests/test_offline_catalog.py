"""P0 regression guard: the web/proof runtime must never reach MOEX ISS on a catalog request.

`/api/proof/catalog` used to take ~181 s on synthetic fixtures because `CalendarData` allowed live
ISS history fetching whenever `MOEX_OFFLINE_ONLY` / `PYTEST_CURRENT_TEST` were unset, so the very
first catalog call paid a 0.05 s rate-limit sleep plus an HTTP round-trip for ~1,050 uncached
contract secids. These tests simulate a cold, "normal user" process: no offline environment flags
and no network access at all.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
for extra in (_REPO_ROOT / "packages" / "proof" / "tests", _REPO_ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import moex_spread_lab.web.server as web_server  # noqa: E402

_CATALOG_BUDGET_SECONDS = 5.0


class OfflineCalendarDataTests(unittest.TestCase):
    def test_calendar_data_is_offline_by_default(self) -> None:
        from moex_spread_lab.strategies.calendar.calendar_spreads import CalendarData

        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(CalendarData(Path(tmp)).offline)
            self.assertFalse(CalendarData(Path(tmp), offline=False).offline)

    def test_fetch_inputs_in_offline_mode_only_reads_cached_secids(self) -> None:
        from moex_spread_lab.strategies.calendar.loaders import (
            _offline,
            cached_till_date,
            fetch_inputs,
        )
        from proof_fixtures import two_calendar_windows

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            two_calendar_windows(root)
            cache = root / "data/spread-history-cache"
            cached = {path.name.split("-")[0] for path in cache.glob("*.json")}
            till = cached_till_date(cache)
            assert till is not None
            start = time.perf_counter()
            histories = fetch_inputs(cache, ("SR",), till, opener=_offline)
            elapsed = time.perf_counter() - start

        self.assertTrue(histories)
        self.assertTrue(set(histories) <= cached)
        self.assertLess(elapsed, 1.0)


class CatalogOfflineTests(unittest.TestCase):
    def test_catalog_on_synthetic_fixtures_does_not_touch_the_network(self) -> None:
        from proof_fixtures import two_calendar_windows

        saved = os.environ.pop("MOEX_OFFLINE_ONLY", None)
        self.addCleanup(
            lambda: os.environ.__setitem__("MOEX_OFFLINE_ONLY", saved) if saved else None
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            two_calendar_windows(root)
            from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs

            store = web_server.ScreenerStore(ScreenerInputs(root))
            try:
                start = time.perf_counter()
                catalog = store.proof.catalog()
                elapsed = time.perf_counter() - start
            finally:
                store.close()

        self.assertTrue(catalog["assets"])
        self.assertLess(elapsed, _CATALOG_BUDGET_SECONDS)


class CatalogColdProcessTests(unittest.TestCase):
    """The acceptance gate from the plan: a clean subprocess with no offline env flags."""

    def test_catalog_under_budget_in_a_clean_subprocess(self) -> None:
        script = (
            "import json,sys,tempfile,time\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(_REPO_ROOT / 'tests')!r})\n"
            f"sys.path.insert(0, {str(_REPO_ROOT / 'packages' / 'proof' / 'tests')!r})\n"
            "from proof_fixtures import two_calendar_windows\n"
            "from moex_spread_lab.strategies.pair_screener.pair_screener import ScreenerInputs\n"
            "from moex_spread_lab.web.server import ScreenerStore\n"
            "with tempfile.TemporaryDirectory() as tmp:\n"
            "    root = Path(tmp)\n"
            "    two_calendar_windows(root)\n"
            "    store = ScreenerStore(ScreenerInputs(root))\n"
            "    start = time.perf_counter()\n"
            "    catalog = store.proof.catalog()\n"
            "    print(json.dumps({'seconds': time.perf_counter() - start,\n"
            "                      'assets': sorted(catalog['assets'])}))\n"
            "    store.close()\n"
        )
        env = {k: v for k, v in os.environ.items() if k != "MOEX_OFFLINE_ONLY"}
        env.pop("PYTEST_CURRENT_TEST", None)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        payload = __import__("json").loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(payload["assets"])
        self.assertLess(payload["seconds"], _CATALOG_BUDGET_SECONDS)
