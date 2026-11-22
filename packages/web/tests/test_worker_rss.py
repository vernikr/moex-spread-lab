"""Optional pool RSS sampling is explicit about observed and incomplete samples."""

import pytest

from moex_spread_lab.web import dev_worker_rss as sampling


def test_peak_and_incomplete_samples(monkeypatch):
    values = iter(
        [
            {"worker_resources": {"complete": True, "rss_kb": 100, "sampled_workers": 2}},
            {"worker_resources": {"complete": False, "rss_kb": 900, "sampled_workers": 1}},
            {"worker_resources": {"complete": True, "rss_kb": 125, "sampled_workers": 1}},
        ]
    )
    monkeypatch.setattr(sampling, "pool_snapshot", lambda *args, **kwargs: next(values))
    sampler = sampling.WorkerRSSSampler(object())
    for _ in range(3):
        sampler._sample()
    assert sampler.snapshot() == {
        "sample_interval_ms": 100,
        "samples": 2,
        "incomplete_samples": 1,
        "max_workers_sampled": 2,
        "sampler_stopped": True,
        "peak_sampled_pool_rss_kb": 125,
    }


def test_disabled_and_exception_cleanup(monkeypatch):
    monkeypatch.setattr(
        sampling,
        "pool_snapshot",
        lambda *a, **k: {"worker_resources": {"complete": True, "rss_kb": 0, "sampled_workers": 0}},
    )
    with sampling.sample_worker_rss(object(), False) as disabled:
        assert disabled is None
    with pytest.raises(ValueError), sampling.sample_worker_rss(object(), True) as sampler:
        raise ValueError("failure")
    assert sampler is not None and not sampler.thread.is_alive()
    assert sampler.snapshot()["samples"] >= 1
