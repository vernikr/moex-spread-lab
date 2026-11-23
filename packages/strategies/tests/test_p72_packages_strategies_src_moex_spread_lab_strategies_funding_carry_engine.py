"""Isolated regressions for the strict-typed funding carry engine facade."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from moex_spread_lab.strategies.funding_carry import engine, funding_recheck


def _fetch(secid: str, first: str, last: str, **options: Any) -> dict[str, Any]:
    assert options.get("cache_dir") is None
    assert options.get("allow_empty") is True
    return {
        "secid": secid,
        "rows": [
            {"SECID": secid, "TRADEDATE": "2025-08-18", "SWAPRATE": 0.1},
            {"SECID": secid, "TRADEDATE": "2025-08-19", "SWAPRATE": None},
            {"SECID": secid, "TRADEDATE": "2025-08-20", "SWAPRATE": "-0.05"},
        ],
        "source": {"raw_response_sha256": ["ab" * 32, "cd" * 32]},
    }


def test_p72_packages_strategies_src_moex_spread_lab_strategies_funding_carry_engine_facade_identity() -> (
    None
):
    assert engine.recheck is funding_recheck.recheck
    assert set(engine.__all__) <= set(vars(engine))
    from moex_spread_lab.strategies import funding_carry

    assert funding_carry.recheck is funding_recheck.recheck


def test_p72_packages_strategies_src_moex_spread_lab_strategies_funding_carry_engine_payload_contract() -> (
    None
):
    with tempfile.TemporaryDirectory() as tmp:
        payload = engine.recheck(Path(tmp), "GAZPF", "2025-08-18", "2025-08-20", fetch=_fetch)
    assert payload == {
        "secid": "GAZPF",
        "from": "2025-08-18",
        "till": "2025-08-20",
        "eod_rows": 3,
        "numeric": 2,
        "null": 1,
        "first_numeric": "2025-08-18",
        "trade_dates": ["2025-08-18", "2025-08-19", "2025-08-20"],
        "page_hashes": ["ab" * 32, "cd" * 32],
        "applied": False,
    }


def test_p72_packages_strategies_src_moex_spread_lab_strategies_funding_carry_engine_apply_round_trip() -> (
    None
):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        applied = engine.recheck(
            root, "GAZPF", "2025-08-18", "2025-08-20", fetch=_fetch, apply=True
        )
        assert applied["applied"] is True
        records = engine.read_supplement(root, "GAZPF")
    assert set(records) == {"2025-08-18", "2025-08-20"}
    assert records["2025-08-18"] == {
        "rate": 0.1,
        "from": "2025-08-18",
        "till": "2025-08-20",
        "page_hashes": ["ab" * 32, "cd" * 32],
    }
    assert records["2025-08-20"]["rate"] == -0.05
