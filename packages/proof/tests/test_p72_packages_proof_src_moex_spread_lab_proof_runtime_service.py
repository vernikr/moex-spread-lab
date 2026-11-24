"""Isolated regressions for strict-typed service facade."""

from __future__ import annotations

from pathlib import Path

from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.proof import payload_tools, service
from moex_spread_lab.proof.service import (
    FORCED_EXIT_REASONS,
    PROOF_SCHEMA,
    ProofService,
    reference_eval,
)


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_service_bindings() -> None:
    assert service._benchmark_marks is payload_tools.benchmark_marks
    assert service._load_instruments is payload_tools.load_instruments
    assert service._sha256_bytes is payload_tools.sha256_bytes
    assert service._mark is payload_tools.mark
    assert service._load_contracts_for_side_cost is payload_tools.load_contracts_for_side_cost
    assert service._result is payload_tools.result_label
    assert service._clean is payload_tools.clean
    assert service._events is payload_tools.events
    assert PROOF_SCHEMA == "execution-proof.v1"
    assert "end_of_sample" in FORCED_EXIT_REASONS


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_service_reference_eval(
    monkeypatch,
) -> None:
    monkeypatch.delenv("MOEX_PROOF_REFERENCE_EVAL", raising=False)
    assert reference_eval() is False
    monkeypatch.setenv("MOEX_PROOF_REFERENCE_EVAL", "1")
    assert reference_eval() is True


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_service_init_and_close(
    tmp_path: Path,
) -> None:
    svc = ProofService(tmp_path)
    assert svc.root == tmp_path.resolve()
    assert svc._pool is None
    svc.close()  # no-op when no pool


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_service_prepare_rejects_non_dict(
    tmp_path: Path,
) -> None:
    svc = ProofService(tmp_path)
    try:
        svc.prepare([])
    except Exception as exc:  # W0Error
        assert "объектом" in str(exc)
    else:
        raise AssertionError("expected W0Error")


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_service_proof_uses_strict(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    class FakeSettings:
        validation_percent = 25.0
        allow_failed_volume = True
        allow_failed_capital = True
        slippage_ticks = 2
        initial_capital_rub = 1_000_000.0
        timeframe = "1d"
        price_fill = "reference_same_bar"

        @staticmethod
        def load(_root: Path) -> FakeSettings:
            return FakeSettings()

    monkeypatch.setattr(
        ResearchSettings, "load", staticmethod(lambda _root: FakeSettings.load(_root))
    )

    svc = ProofService(tmp_path)
    sentinel = object()

    def fake_prepare(selection, *, calendar=None, settings=None):
        captured["selection"] = selection
        captured["calendar"] = calendar
        captured["settings"] = settings
        return lambda _sel: {"ok": sentinel}

    monkeypatch.setattr(svc, "prepare", fake_prepare)
    monkeypatch.setattr(ProofService, "calendar_data", property(lambda self: "cal"))
    result = svc.proof({"strategy": "x"})
    assert result == {"ok": sentinel}
    assert captured["selection"] == {"strategy": "x"}
    assert captured["calendar"] == "cal"
    settings = captured["settings"]
    assert isinstance(settings, ResearchSettings)
    assert settings.allow_failed_volume is False
    assert settings.allow_failed_capital is False
    assert settings.validation_percent == 25.0
