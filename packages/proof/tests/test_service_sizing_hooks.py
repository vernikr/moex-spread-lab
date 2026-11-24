"""Automatic sizing keeps the service facade's runtime patch points."""

from unittest.mock import Mock

from moex_spread_lab.core.evaluation_periods import STRICT_SCREENS
from moex_spread_lab.proof import service as facade
from moex_spread_lab.proof.strategy_catalog import get_strategy_registry


def test_sized_replay_resolves_facade_hooks_after_preparation(monkeypatch, tmp_path):
    service = facade.ProofService(tmp_path)
    unit = {"series": [{"date": "2026-01-01"}]}
    monkeypatch.setattr(service, "_prepare_fixed", Mock(return_value=Mock(return_value=unit)))
    monkeypatch.setattr(facade, "_intent_request", lambda request, registry, settings: request)
    monkeypatch.setattr(facade, "_benchmark_marks", lambda path: (["2026-01-01"], {}))
    monkeypatch.setattr(facade, "_load_instruments", lambda path: {})
    monkeypatch.setattr(facade, "_calendar_sizing_unit", lambda root, request, value, *args: value)
    replay = service._prepare_automatic(
        {}, None, STRICT_SCREENS, get_strategy_registry(), "zscore_mean_reversion"
    )

    mark = Mock(return_value=42.0)
    size = Mock(return_value={"sentinel": True})
    monkeypatch.setattr(facade, "_mark", mark)
    monkeypatch.setattr(facade, "size_proof", size)
    assert replay({}) == {"sentinel": True}
    mark.assert_called_once()
    assert size.call_args.args[1] == {"2026-01-01": 42.0}
