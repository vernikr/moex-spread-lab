"""Isolated regressions for the strict-typed carry strategy runner (P7.2 leaf #109).

``strategy_runner.build_carry_strategy`` narrows the untrusted ``carry_result``
payload at a single boundary: ``rows`` must be a list (else the historical
``CarryStrategyError`` is raised) and it must be long enough to cover the warmup
window. Typing that boundary must not add runtime validation, copy the rows, or
change the normalized payload, so the tests below pin the guard order, the error
identity and the no-copy/no-mutation contract of the narrowed list.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from moex_spread_lab.strategies.funding_carry import carry, strategy_runner

_WARMUP_ERROR = "carry decomposition does not contain the required warmup"


def _write_contracts(root: Path) -> Path:
    contracts = root / "contracts.json"
    contracts.write_text(
        json.dumps(
            {
                "instruments": [
                    {
                        "secid": "NEAR",
                        "stable_id": "n",
                        "board": "RFUD",
                        "expiry": "2100-01-01",
                        "lot_size": 1000,
                        "tick_size": 0.001,
                        "tick_value": 1,
                        "initial_margin": 900,
                        "source_vintage": "current_snapshot",
                    },
                    {
                        "secid": "FAR",
                        "stable_id": "f",
                        "board": "RFUD",
                        "expiry": "2026-12-17",
                        "lot_size": 1000,
                        "tick_size": 0.001,
                        "tick_value": 1,
                        "initial_margin": 1000,
                        "source_vintage": "current_snapshot",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return contracts


def _write_benchmark(root: Path, dates: list[str]) -> Path:
    benchmark = root / "benchmark.json"
    benchmark.write_text(
        json.dumps(
            {
                "schema_version": "lqdt-lineage.v1",
                "rows": [
                    {
                        "date": date,
                        "price": 1.0,
                        "source_kind": "observed",
                        "source_ticker": "LQDT",
                    }
                    for date in dates
                ],
            }
        ),
        encoding="utf-8",
    )
    return benchmark


def _carry_rows(count: int = 6) -> list[dict[str, Any]]:
    dates = [f"2026-01-{day:02d}" for day in range(1, count + 1)]
    return [
        {
            "date": date,
            "basis_far_minus_near": 1.0,
            "daily_funding_received_short_near": 1.0,
            "daily_gross_carry_pnl": float(index),
        }
        for index, date in enumerate(dates)
    ]


def _carry_payload(rows: Any) -> dict[str, Any]:
    return {
        "schema_version": "carry-study.v1",
        "near": "NEAR",
        "far": "FAR",
        "rows": rows,
    }


def test_p72_packages_strategies_src_moex_spread_lab_strategies_funding_carry_strategy_runner_export_identity() -> (
    None
):
    assert carry.build_carry_strategy is strategy_runner.build_carry_strategy


@pytest.mark.parametrize("rows", [None, "not-a-list", {"0": {}}, 3])
def test_p72_packages_strategies_src_moex_spread_lab_strategies_funding_carry_strategy_runner_rejects_non_list_rows(
    tmp_path: Path, rows: Any
) -> None:
    contracts = _write_contracts(tmp_path)
    benchmark = _write_benchmark(tmp_path, [f"2026-01-{day:02d}" for day in range(1, 7)])
    with pytest.raises(strategy_runner.CarryStrategyError) as excinfo:
        strategy_runner.build_carry_strategy(
            _carry_payload(rows), benchmark, contracts, warmup_observations=2
        )
    assert str(excinfo.value) == _WARMUP_ERROR


def test_p72_packages_strategies_src_moex_spread_lab_strategies_funding_carry_strategy_runner_rejects_missing_rows_key(
    tmp_path: Path,
) -> None:
    contracts = _write_contracts(tmp_path)
    benchmark = _write_benchmark(tmp_path, ["2026-01-01"])
    payload = _carry_payload("unused")
    del payload["rows"]
    with pytest.raises(strategy_runner.CarryStrategyError) as excinfo:
        strategy_runner.build_carry_strategy(payload, benchmark, contracts, warmup_observations=2)
    assert str(excinfo.value) == _WARMUP_ERROR


@pytest.mark.parametrize(("count", "warmup"), [(2, 2), (1, 2), (0, 1)])
def test_p72_packages_strategies_src_moex_spread_lab_strategies_funding_carry_strategy_runner_keeps_the_original_length_guard(
    tmp_path: Path, count: int, warmup: int
) -> None:
    """The narrowing split the guard in two; the length check keeps ``<=`` semantics."""
    contracts = _write_contracts(tmp_path)
    dates = [f"2026-01-{day:02d}" for day in range(1, max(count, 1) + 1)]
    benchmark = _write_benchmark(tmp_path, dates)
    with pytest.raises(strategy_runner.CarryStrategyError) as excinfo:
        strategy_runner.build_carry_strategy(
            _carry_payload(_carry_rows(count)), benchmark, contracts, warmup_observations=warmup
        )
    assert str(excinfo.value) == _WARMUP_ERROR


def test_p72_packages_strategies_src_moex_spread_lab_strategies_funding_carry_strategy_runner_narrowing_adds_no_validation(
    tmp_path: Path,
) -> None:
    """A list of non-mapping rows is not re-validated: the cast copies nothing at runtime."""
    contracts = _write_contracts(tmp_path)
    benchmark = _write_benchmark(tmp_path, ["2026-01-01"])
    with pytest.raises(TypeError):
        strategy_runner.build_carry_strategy(
            _carry_payload([1, 2, 3]), benchmark, contracts, warmup_observations=2
        )


@pytest.mark.parametrize("metrics_only", [False, True])
def test_p72_packages_strategies_src_moex_spread_lab_strategies_funding_carry_strategy_runner_passes_the_original_rows_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metrics_only: bool
) -> None:
    """The narrowed list is the caller's object: same identity, never copied or mutated."""
    contracts = _write_contracts(tmp_path)
    rows = _carry_rows()
    benchmark = _write_benchmark(tmp_path, [row["date"] for row in rows])
    snapshot = json.loads(json.dumps(rows))
    seen: list[Any] = []
    original = strategy_runner._simulate_carry_rows

    def _capture(ctx: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(ctx.rows)
        return original(ctx, *args, **kwargs)

    monkeypatch.setattr(strategy_runner, "_simulate_carry_rows", _capture)
    result = strategy_runner.build_carry_strategy(
        _carry_payload(rows),
        benchmark,
        contracts,
        warmup_observations=2,
        transaction_cost_scenario="low",
        metrics_only=metrics_only,
    )

    assert len(seen) == 1
    assert seen[0] is rows
    assert rows == snapshot
    if metrics_only:
        assert set(result) == {"trades", "signals", "daily"}
        assert result["daily"]
        assert "benchmark_value" not in result["daily"][0]
    else:
        assert result["schema_version"] == "carry-strategy.v1"
        assert result["summary"]["row_count"] == len(rows)
        assert result["daily"][0]["benchmark_source_ticker"] == "LQDT"


def test_p72_packages_strategies_src_moex_spread_lab_strategies_funding_carry_strategy_runner_payload_is_deterministic(
    tmp_path: Path,
) -> None:
    contracts = _write_contracts(tmp_path)
    rows = _carry_rows()
    benchmark = _write_benchmark(tmp_path, [row["date"] for row in rows])
    options: dict[str, Any] = {
        "warmup_observations": 2,
        "transaction_cost_scenario": "low",
    }
    first = strategy_runner.build_carry_strategy(
        _carry_payload(rows), benchmark, contracts, **options
    )
    second = strategy_runner.build_carry_strategy(
        _carry_payload(rows), benchmark, contracts, **options
    )
    assert first == second
    assert first["summary"]["historical_executable_fills"] is False
