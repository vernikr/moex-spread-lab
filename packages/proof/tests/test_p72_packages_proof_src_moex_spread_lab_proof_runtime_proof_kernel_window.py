"""Isolated StudyWindow regressions for the strict-typed proof kernel.

Split out of the facade regression module so each artifact stays inside the
repository new-file budget; the suffix keeps both unique to this work package.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from moex_spread_lab.accounting.position_sizing import AUTO_SIZING
from moex_spread_lab.core.evaluation_periods import ResearchSettings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof import proof_kernel
from moex_spread_lab.proof.proof_kernel import StudyWindow


def _window_service(selection: dict[str, Any], **proof_overrides: Any) -> SimpleNamespace:
    """A service whose prepared replay echoes the request and marks the cut through."""
    state: dict[str, Any] = {"calls": []}

    def prepare(request: dict[str, Any], **_kwargs: Any) -> Any:
        def replay(
            req: dict[str, Any],
            through: Any = None,
            **options: Any,
        ) -> dict[str, Any]:
            state["calls"].append({"request": req, "through": through, **options})
            proof: dict[str, Any] = {
                "selection": {**selection, **req},
                "series": [{"date": row["date"]} for row in proof_overrides["series"]],
                "ledger": proof_overrides["ledger"],
                "window": proof_overrides.get(
                    "window", {"passes_capital_screen": True, "tradable_every_day": True}
                ),
                "instrument": proof_overrides.get("instrument", {"family": "pair_same_asset"}),
                "provenance": {},
                "run_id": proof_overrides.get("run_id", "run-1"),
                "execution_reference": proof_overrides.get("execution_reference", True),
            }
            if options.get("metrics_only"):
                proof["_scalar_train_summary"] = (
                    100.0,
                    100.0,
                    1.0,
                    104.0,
                    4.0,
                    False,
                    True,
                )
            return proof

        return replay

    return SimpleNamespace(prepare=prepare, state=state)


def _days(count: int, month: int = 2) -> list[str]:
    return [f"2026-{month:02}-{day:02d}" for day in range(1, count + 1)]


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_kernel_study_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = {
        "strategy": "mean_reversion",
        "timeframe": "1d",
        "parameters": {"lookback": 10, "entry_z": 1.5, "exit_z": 0.5},
        "quantity": 1,
        "slippage_ticks": 1.0,
    }
    days = _days(30, month=1)
    service = _window_service(
        selection,
        series=[{"date": day} for day in days],
        ledger=[{"date": day, "cumulative_net_rub": 0.0} for day in days],
    )
    case = StudyWindow(
        service=service,
        request={"strategy": "mean_reversion", "pair": "A/B"},
        settings=ResearchSettings(validation_percent=20),
        benchmark=(days, dict.fromkeys(days, 1.0)),
    )
    assert set(case.axes) == {"lookback", "entry_z", "exit_z", "quantity", "slippage_ticks"}
    assert case.seed == {
        "lookback": 10,
        "entry_z": 1.5,
        "exit_z": 0.5,
        "quantity": 1,
        "slippage_ticks": 1.0,
    }
    assert len(case.split.training) == 24 and len(case.split.validation) == 6
    assert case.train.training == case.split.training
    assert case.lamp_warning() is None
    assert case.admission() is None

    # A repeated probe reuses the cached seed proof; a new point rebuilds the request.
    assert case.proof(case.seed, training=False) is case.full
    case.proof({**case.seed, "lookback": 11}, training=False)
    assert service.state["calls"][-1]["request"]["parameters"] == {
        "lookback": 11,
        "entry_z": 1.5,
        "exit_z": 0.5,
    }
    assert service.state["calls"][-1]["through"] is None
    trained = case.proof({**case.seed, "lookback": 11}, training=True)
    assert trained["_scalar_train_summary"][-1] is True
    assert service.state["calls"][-1]["through"] == case.split.training[-1]
    assert service.state["calls"][-1]["scalar_summary"] == (
        days[0],
        case.split.training[-1],
    )

    monkeypatch.setattr(proof_kernel, "_measure_provider", None)
    monkeypatch.setattr(
        proof_kernel,
        "measure_full",
        lambda *args: {"utility": 3.0, "annualized_excess_pp": 1.5},
    )
    report = case.report(case.seed)
    # Utility and rank_score are internal scores and never reach the payload.
    assert report["evaluation"]["result"] == {"annualized_excess_pp": 1.5}
    assert report["evaluation"]["failed_screens"] == []
    assert report["evaluation"]["execution_reference"] is True
    assert report["proof_run_id"] == "run-1"
    assert report["run_id"] != "run-1"


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_kernel_window_guards() -> None:
    days = _days(10)
    disjoint = {
        "strategy": "mean_reversion",
        "timeframe": "1d",
        "parameters": {"lookback": 10, "entry_z": 1.0, "exit_z": 1.2},
        "quantity": 1,
        "slippage_ticks": 1.0,
    }
    with pytest.raises(W0Error, match="Порог выхода"):
        StudyWindow(
            service=_window_service(
                disjoint,
                series=[{"date": day} for day in days],
                ledger=[{"date": day, "cumulative_net_rub": 0.0} for day in days],
            ),
            request={"strategy": "mean_reversion", "pair": "A/B"},
            settings=ResearchSettings(),
            benchmark=(days, dict.fromkeys(days, 1.0)),
        )
    late = ["2026-02-01", "2026-02-02"]
    with pytest.raises(W0Error, match="Нет отметки LQDT"):
        StudyWindow(
            service=_window_service(
                {**disjoint, "parameters": {"lookback": 10, "entry_z": 1.5, "exit_z": 0.5}},
                series=[{"date": day} for day in late],
                ledger=[{"date": day, "cumulative_net_rub": 0.0} for day in late],
            ),
            request={"strategy": "mean_reversion", "pair": "A/B"},
            settings=ResearchSettings(),
            # The benchmark line starts after the window, so no opening mark exists.
            benchmark=(late[1:], dict.fromkeys(late[1:], 1.0)),
        )


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_kernel_window_warnings() -> (
    None
):
    selection = {
        "strategy": "mean_reversion",
        "timeframe": "1d",
        "parameters": {"lookback": 10, "entry_z": 1.5, "exit_z": 0.5},
        "quantity": 1,
        "slippage_ticks": 1.0,
    }
    days = _days(5, month=4)
    case = StudyWindow(
        service=_window_service(
            selection,
            series=[{"date": day} for day in days],
            ledger=[{"date": day, "cumulative_net_rub": 0.0} for day in days],
        ),
        request={"strategy": "mean_reversion", "pair": "A/B"},
        settings=ResearchSettings(),
        benchmark=(days, dict.fromkeys(days, 1.0)),
    )
    assert case.lamp_warning() == (
        "Недостаточно истории для оценки устойчивости: 5 из 24 наблюдений на обучении."
    )
    assert case.admission() == (
        "Недостаточно наблюдений на обучении: 5 из 7 для допустимого окна сигнала."
    )


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_kernel_window_admission_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = {
        "strategy": "funding_only",
        "timeframe": "1d",
        "parameters": {"lookback": 20},
        "sizing": AUTO_SIZING,
    }
    days = _days(14, month=5)
    service = _window_service(
        selection,
        series=[{"date": day} for day in days],
        ledger=[{"date": day, "cumulative_net_rub": 0.0} for day in days],
    )
    case = StudyWindow(
        service=service,
        request={"strategy": "funding_only", "pair": "A/B"},
        settings=ResearchSettings(),
        benchmark=(days, dict.fromkeys(days, 1.0)),
    )
    assert case.admission() is None
    probes = [call["request"]["parameters"] for call in service.state["calls"][1:]]
    # The probe leaves two observations for the signal window instead of the full seed.
    assert probes == [{"lookback": 12}]


def test_p72_packages_proof_src_moex_spread_lab_proof_runtime_proof_kernel_report_automatic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = {
        "strategy": "funding_only",
        "timeframe": "1d",
        "parameters": {"lookback": 10},
        "sizing": AUTO_SIZING,
    }
    days = _days(8, month=6)
    case = StudyWindow(
        service=_window_service(
            selection,
            series=[{"date": day} for day in days],
            ledger=[
                {
                    "date": day,
                    "cumulative_net_rub": 0.0,
                    "cash_shortfall": False,
                    "free_cash_rub": 5.0,
                }
                for day in days
            ],
            window={"passes_capital_screen": False, "tradable_every_day": True},
            run_id="auto-1",
            execution_reference=False,
        ),
        request={"strategy": "funding_only", "pair": "A/B"},
        settings=ResearchSettings(),
        benchmark=(days, dict.fromkeys(days, 1.0)),
    )
    monkeypatch.setattr(proof_kernel, "_measure_provider", None)
    monkeypatch.setattr(proof_kernel, "measure_full", lambda *args: {"utility": 1.0})
    report = case.report(case.seed)
    assert report["evaluation"]["account_checks"] == {
        "cash_shortfall_rows": 0,
        "first_cash_shortfall_date": None,
        "minimum_free_cash_rub": 5.0,
    }
    assert report["evaluation"]["failed_screens"] == ["capital"]
    assert report["evaluation"]["execution_reference"] is False
