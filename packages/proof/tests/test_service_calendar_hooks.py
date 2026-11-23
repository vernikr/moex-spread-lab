"""Calendar replay retains the facade's late-bound enrichment hook."""

from unittest.mock import Mock

from moex_spread_lab.core.evaluation_periods import STRICT_SCREENS
from moex_spread_lab.proof_runtime import service as facade


def test_calendar_replay_resolves_enrichment_after_preparation(monkeypatch, tmp_path):
    service = facade.ProofService(tmp_path)
    calendar = Mock()
    payload = {"schema_version": "calendar-proof.v1"}
    calendar.proof.return_value = payload
    replay = service._prepare_fixed({}, calendar=calendar, settings=STRICT_SCREENS)
    enrich = Mock(return_value={"sentinel": True})
    monkeypatch.setattr(facade, "calendar_proof", enrich)
    assert replay({}) == {"sentinel": True}
    enrich.assert_called_once()
    assert replay({}, through="2026-01-01") is payload
    enrich.assert_called_once()
