"""Window-summary progress contract across execution paths."""

from unittest.mock import patch

from moex_spread_lab.proof_runtime.proof_windows import window_summaries
from test_proof_windows import CAL, PAIR, FixtureCase


class WindowProgressTests(FixtureCase):
    def test_progress_is_identical_for_local_and_worker_paths(self):
        requests = {"requests": [PAIR, CAL]}
        local_events = []
        with patch("moex_spread_lab.proof_runtime.proof_windows.get_worker_pool") as get_pool:
            get_pool.return_value.should_use.return_value = False
            local = window_summaries(self.service, requests, on_progress=local_events.append)

        worker_events = []
        with patch("moex_spread_lab.proof_runtime.proof_windows.get_worker_pool") as get_pool:
            pool = get_pool.return_value
            pool.should_use.return_value = True

            def run_workers(root, settings, benchmark, payload, check_cancelled, on_progress):
                self.assertEqual([entry[0] for entry in payload], [0, 1])
                on_progress(1, 2)
                on_progress(2, 2)
                return local["rows"]

            pool.window_summaries.side_effect = run_workers
            worker = window_summaries(self.service, requests, on_progress=worker_events.append)
        self.assertEqual(worker, local)
        self.assertEqual(worker_events, local_events)
        self.assertEqual([event["percent"] for event in local_events], [4.0, 51.0, 98.0])
