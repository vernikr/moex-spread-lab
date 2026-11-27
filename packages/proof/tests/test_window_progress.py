"""Window-summary progress contract across execution paths."""

from concurrent.futures import Future
from unittest.mock import patch

from moex_spread_lab.proof import worker_cohort
from moex_spread_lab.proof.proof_windows import window_summaries
from test_proof_windows import CAL, PAIR, FixtureCase


class _InlineExecutor:
    def submit(self, fn, *args):
        fut = Future()
        fut.set_result(fn(*args))
        return fut


class WindowProgressTests(FixtureCase):
    def test_progress_is_identical_for_local_and_worker_paths(self):
        requests = {"requests": [PAIR, CAL]}
        local_events = []
        with patch("moex_spread_lab.proof.proof_windows.get_worker_pool") as get_pool:
            get_pool.return_value.should_use.return_value = False
            local = window_summaries(self.service, requests, on_progress=local_events.append)

        worker_events = []
        try:
            with patch("moex_spread_lab.proof.proof_windows.get_worker_pool") as get_pool:
                pool = get_pool.return_value
                pool.max_workers = 2
                pool.should_use.return_value = True
                pool.ensure_workers.side_effect = lambda count: [
                    _InlineExecutor() for _ in range(count)
                ]
                worker = window_summaries(self.service, requests, on_progress=worker_events.append)
        finally:
            worker_cohort._WORKER_SERVICES.pop(str(self.root.resolve()), None)
        self.assertEqual(worker, local)
        self.assertEqual(worker_events, local_events)
        self.assertEqual([event["percent"] for event in local_events], [4.0, 51.0, 98.0])
