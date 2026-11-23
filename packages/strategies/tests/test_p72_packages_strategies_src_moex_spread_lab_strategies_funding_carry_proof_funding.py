"""Regression coverage for strict typing of ``proof_funding`` (Issue #107)."""

from __future__ import annotations

import io
import json
import math
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from moex_spread_lab.core import universe
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.data.iss.catalog import fetch_history
from moex_spread_lab.strategies.funding_carry import proof_funding
from moex_spread_lab.strategies.funding_carry.proof_funding import (
    NO_CLEARING,
    POLICY,
    FundingEvidence,
    FundingTimeline,
    FundingWindow,
    ProofFunding,
    _default_history_source,
    _default_offline_only,
    _default_positive_volume_dates,
    _leg_daily_volumes,
)


@dataclass(frozen=True)
class _StubBarSeries:
    rows: tuple[dict[str, Any], ...]


class _StubInputs:
    def __init__(
        self,
        root: Path,
        bars_by_leg: dict[tuple[str, str], tuple[dict[str, Any], ...]] | None = None,
    ) -> None:
        self.root = root
        self._bars_by_leg = bars_by_leg or {}

    def history_cache(self, _pair_id: str) -> Path:
        path = self.root / "history"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def bars(
        self,
        pair_id: str,
        secid: str,
        *,
        columns: tuple[str, ...] = ("begin",),
    ) -> _StubBarSeries:
        _ = columns
        if (pair_id, secid) not in self._bars_by_leg:
            raise W0Error(f"missing bars for {pair_id}:{secid}")
        return _StubBarSeries(self._bars_by_leg[(pair_id, secid)])


def _seed_history_cache(
    directory: Path, secid: str, start: str, end: str, rows: list[dict[str, Any]]
) -> None:
    cols = ["SECID", "TRADEDATE", "SETTLEPRICE", "SWAPRATE"]
    data = [
        [r.get("SECID", secid), r["TRADEDATE"], r.get("SETTLEPRICE"), r.get("SWAPRATE")]
        for r in rows
    ]
    raw = json.dumps(
        {
            "history": {"columns": cols, "data": data},
            "history.cursor": {"columns": ["TOTAL"], "data": [[len(data)]]},
        }
    ).encode("utf-8")
    fetch_history(
        secid,
        start,
        end,
        cache_dir=directory,
        delay_seconds=0,
        opener=lambda *_a, **_k: io.BytesIO(raw),
    )


class ProofFundingStrictRegressionTests(unittest.TestCase):
    def test_universe_bindings_and_public_exports_preserved(self) -> None:
        self.assertIs(proof_funding.HISTORY_WINDOWS, universe.HISTORY_WINDOWS)
        self.assertIs(proof_funding.PAIRS, universe.PAIRS)
        self.assertIn("ProofFunding", proof_funding.__all__)
        self.assertIn("FundingWindow", proof_funding.__all__)
        self.assertIn("FundingEvidence", proof_funding.__all__)

    def test_offline_guard_and_default_history_source(self) -> None:
        with self.assertRaisesRegex(OSError, "local screener does not download data"):
            _default_offline_only("https://iss.moex.com")

        pair_id = "CNYRUBF/CRZ6"
        start, end = proof_funding.HISTORY_WINDOWS[pair_id]
        with tempfile.TemporaryDirectory() as tmp:
            inputs = _StubInputs(Path(tmp))
            _seed_history_cache(
                inputs.history_cache(pair_id),
                "CNYRUBF",
                start,
                end,
                [{"TRADEDATE": "2025-07-01", "SETTLEPRICE": 11.5, "SWAPRATE": 0.01}],
            )
            loaded = _default_history_source(inputs, pair_id, "CNYRUBF")
        self.assertEqual(loaded["secid"], "CNYRUBF")
        self.assertEqual(loaded["rows"][0]["TRADEDATE"], "2025-07-01")

    def test_leg_daily_volumes_and_positive_volume_dates(self) -> None:
        pair = {"pair_id": "CNYRUBF/CRZ6", "near": "CNYRUBF", "far": "CRZ6"}
        near_rows = (
            {"begin": "2025-07-01 10:00:00", "volume": 10},
            {"begin": "2025-07-01 11:00:00", "volume": 15},
            {"begin": "2025-07-02 10:00:00", "volume": 5},
            {"begin": "2025-07-02 11:00:00", "volume": True},
            {"begin": "2025-07-03 10:00:00", "volume": 0},
            {"begin": "2025-07-04 10:00:00", "volume": float("nan")},
            {"begin": "2025-07-05 10:00:00", "volume": -1},
            {"begin": "short", "volume": 100},
            {"begin": None, "volume": 100},
        )
        far_rows = tuple({"begin": f"2025-07-0{i} 10:00:00", "volume": 20} for i in (1, 2, 3))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = _StubInputs(
                root,
                {("CNYRUBF/CRZ6", "CNYRUBF"): near_rows, ("CNYRUBF/CRZ6", "CRZ6"): far_rows},
            )
            vols = _leg_daily_volumes(inputs, "CNYRUBF/CRZ6", "CNYRUBF")
            assert vols is not None
            self.assertTrue(math.isclose(vols["2025-07-01"] or 0.0, 25.0))
            self.assertIsNone(vols["2025-07-02"])
            self.assertEqual(vols["2025-07-03"], 0.0)
            self.assertIsNone(vols["2025-07-04"])
            self.assertIsNone(vols["2025-07-05"])
            self.assertEqual(_default_positive_volume_dates(inputs, pair), {"2025-07-01"})
            self.assertIsNone(_leg_daily_volumes(inputs, "CNYRUBF/CRZ6", "MISSING"))
            self.assertEqual(_default_positive_volume_dates(_StubInputs(root), pair), set())

    def test_timeline_uses_market_dates_and_caches_sources(self) -> None:
        pair = {
            "pair_id": "CNYRUBF/CRZ6",
            "near": "CNYRUBF",
            "far": "CRZ6",
            "family": "perpetual_quarterly_same_asset",
        }
        start, end = proof_funding.HISTORY_WINDOWS["CNYRUBF/CRZ6"]
        w_start, w_end = proof_funding.HISTORY_WINDOWS["GAZPF/GZZ6"]
        calls: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            bars = {
                ("CNYRUBF/CRZ6", "CNYRUBF"): tuple(
                    {"begin": f"2025-07-0{d} 10:00:00"} for d in (7, 8, 9)
                )
            }
            inputs = _StubInputs(Path(tmp), bars)
            cache_dir = inputs.history_cache("CNYRUBF/CRZ6")
            _seed_history_cache(
                cache_dir,
                "CNYRUBF",
                start,
                end,
                [
                    {"TRADEDATE": "2025-07-07", "SETTLEPRICE": 11.0, "SWAPRATE": 0.02},
                    {"TRADEDATE": "2025-07-09", "SETTLEPRICE": 11.2, "SWAPRATE": 0.06},
                ],
            )
            _seed_history_cache(
                cache_dir,
                "GAZPF",
                w_start,
                w_end,
                [{"TRADEDATE": "2025-07-08", "SETTLEPRICE": 130.0, "SWAPRATE": 0.0}],
            )

            def tracked(inp: Any, pid: str, secid: str) -> dict[str, Any]:
                calls.append((pid, secid))
                return _default_history_source(inp, pid, secid)

            pf = ProofFunding(inputs, history_fn=tracked)
            first = pf.source("CNYRUBF/CRZ6", "CNYRUBF")
            self.assertIs(pf.source("CNYRUBF/CRZ6", "CNYRUBF"), first)
            self.assertEqual(calls, [("CNYRUBF/CRZ6", "CNYRUBF")])
            marks = pf.timeline(pair).require(("2025-07-07", "2025-07-08", "2025-07-09"))
            self.assertEqual(marks["2025-07-08"].kind, "midpoint")
            self.assertAlmostEqual(marks["2025-07-08"].value or 0.0, 0.04)
            self.assertIn("2025-07-08", pf.market_dates())

    def test_statuses_settlement_days_and_carry_window(self) -> None:
        perp = {
            "pair_id": "GAZPF/GZZ6",
            "near": "GAZPF",
            "far": "GZZ6",
            "family": "perpetual_quarterly_same_asset",
        }
        dated = {
            "pair_id": "GLZ6/GLH7",
            "near": "GLZ6",
            "far": "GLH7",
            "family": "dated_calendar_same_asset",
        }
        days = ("2025-08-15", "2025-08-16", "2025-08-17", "2025-08-18")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ok_file = root / "ok.json"
            ok_file.write_text("{}", encoding="utf-8")
            bars = {
                ("GAZPF/GZZ6", s): tuple({"begin": f"{d} 10:00:00", "volume": 10} for d in days)
                for s in ("GAZPF", "GZZ6")
            }
            sources = {
                "GAZPF": {
                    "secid": "GAZPF",
                    "rows": [
                        {"TRADEDATE": "2025-08-15", "SETTLEPRICE": 120.0, "SWAPRATE": 0.02},
                        {"TRADEDATE": "2025-08-18", "SETTLEPRICE": 121.0, "SWAPRATE": 0.04},
                    ],
                },
                "GZZ6": {
                    "secid": "GZZ6",
                    "rows": [
                        {"TRADEDATE": "2025-08-15", "SETTLEPRICE": 122.0},
                        {"TRADEDATE": "2025-08-16", "SETTLEPRICE": 122.5},
                        {"TRADEDATE": "2025-08-18", "SETTLEPRICE": 123.0},
                    ],
                },
            }
            pf = ProofFunding(_StubInputs(root, bars), history_fn=lambda _i, _p, sec: sources[sec])

            d_flags, d_reasons = pf.statuses(dated, set(days), [ok_file], [ok_file])
            self.assertEqual(d_flags, {"mean_reversion": True, "funding_only": False})
            self.assertIn("вечного фьючерса", d_reasons["funding_only"])

            m_flags, m_reasons = pf.statuses(perp, set(days), [root / "absent.json"], [ok_file])
            self.assertEqual(m_flags, {"mean_reversion": False, "funding_only": True})
            self.assertIn("absent.json", m_reasons["mean_reversion"])

            e_flags, _ = pf.statuses(perp, set(), [ok_file], [ok_file])
            self.assertEqual(e_flags, {"mean_reversion": False, "funding_only": False})

            ok_flags, ok_reasons = pf.statuses(perp, set(days), [ok_file], [ok_file])
            self.assertEqual(
                (ok_flags, ok_reasons), ({"mean_reversion": True, "funding_only": True}, {})
            )
            self.assertEqual(pf.settlement_days(perp), {"2025-08-15", "2025-08-18"})

            win = pf.carry_window(perp, 0)
            self.assertEqual(set(win.omitted), set(NO_CLEARING))
            ev_cut = win.resolve("2025-08-16")
            self.assertEqual(ev_cut.provenance["funding_policy"], POLICY)
            self.assertEqual(ev_cut.provenance["funding_no_clearing_dates"], ["2025-08-16"])
            ev_full = win.resolve_full()
            self.assertIs(win.resolve_full(), ev_full)
            self.assertEqual(ev_full.rates, {"2025-08-15": 0.02, "2025-08-18": 0.04})
            self.assertTrue(any("Без отдельного клиринга" in n for n in ev_full.notes()))

    def test_funding_window_split_and_empty_cut_errors(self) -> None:
        days = ("2025-08-04", "2025-08-05", "2025-08-06", "2025-08-07")
        published: dict[str, float | None] = {
            "2025-08-04": 0.01,
            "2025-08-05": 0.02,
            "2025-08-06": None,
            "2025-08-07": 0.04,
        }
        split_win = FundingWindow(FundingTimeline("CNYRUBF", published, days), days, 25)
        self.assertEqual(split_win.training_end, "2025-08-06")
        with self.assertRaisesRegex(W0Error, "за границей периода"):
            split_win.resolve()
        with self.assertRaisesRegex(W0Error, "Нет расчётных дат"):
            split_win.resolve("2025-08-01")
        evidence = split_win.resolve("2025-08-05")
        self.assertIsInstance(evidence, FundingEvidence)
        self.assertEqual(evidence.notes(), [])


if __name__ == "__main__":
    unittest.main()
