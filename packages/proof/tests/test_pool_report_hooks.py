"""Pool facade remains patchable after extracting worker dispatch."""

from unittest.mock import Mock

import pytest

from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.proof import proof_pool


def test_await_futures_cancels_pending_when_check_raises():
    futures = [Mock(), Mock()]
    failure = RuntimeError("cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        proof_pool._await_futures(futures, Mock(side_effect=failure))
    for future in futures:
        future.cancel.assert_called_once()


def test_window_batches_use_facade_worker_hooks(monkeypatch, tmp_path):
    pool = proof_pool.CohortWorkerPool(max_workers=1)
    monkeypatch.setattr(pool, "_ensure_workers", lambda count: ["slot"])
    monkeypatch.setattr(proof_pool, "_estimate_weight", lambda *args, **kwargs: 1)
    worker = Mock()
    submit = Mock(return_value="future")
    monkeypatch.setattr(proof_pool, "_worker_window_summaries", worker)
    monkeypatch.setattr(proof_pool, "submit_worker", submit)
    monkeypatch.setattr(proof_pool, "_await_futures", lambda *args, **kwargs: [[(0, {"ok": True})]])
    try:
        result = pool.window_summaries(
            tmp_path, ResearchSettings(), ([], {}), [(0, {}, {})], lambda: None
        )
        assert result == [{"ok": True}]
        assert submit.call_args.args[1] is worker
    finally:
        pool.shutdown()
