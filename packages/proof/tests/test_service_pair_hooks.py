"""Pair replay resolves facade hooks after request preparation."""

from unittest.mock import Mock

from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.proof import service as facade
from proof_fixtures import funding_winner
from test_global_capital import PAIR


def test_pair_replay_resolves_facade_hooks_after_preparation(monkeypatch, tmp_path):
    funding_winner(tmp_path)
    service = facade.ProofService(tmp_path)
    replay = service._prepare_pair(PAIR, ResearchSettings())
    validate = Mock(wraps=facade._validate_pair_request)
    run = Mock(return_value={"sentinel": True})
    monkeypatch.setattr(facade, "_validate_pair_request", validate)
    monkeypatch.setattr(facade, "run_mean_reversion", run)
    assert replay(PAIR) == {"sentinel": True}
    validate.assert_called_once()
    run.assert_called_once()
