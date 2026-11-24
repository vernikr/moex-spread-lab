"""Landscape, tuning and measurement regression checks (P7.2 leaf #131).

Assertion-rich bodies are extracted verbatim from ``test_proof_search.py``; the
owning test methods keep their names, node IDs and discovery behaviour and only
delegate here. ``zip`` calls state the default ``strict=False`` explicitly so the
new module needs no Ruff exception.
"""

from moex_spread_lab.core.settings import EvaluationSplit, ResearchSettings
from moex_spread_lab.core.w0 import W0Error
from moex_spread_lab.proof.proof_search import (
    EXECUTION_AXES,
    GREEN,
    STRATEGY_AXES,
    TRIALS,
    Axis,
    Landscape,
    _excess_curve,
    _measure,
    _tune,
)


def check_one_axis_probes_with_constraints_missing_and_negative_results(case):
    axes = STRATEGY_AXES["mean_reversion"] | EXECUTION_AXES
    seed = {
        "lookback": 10,
        "entry_z": 1.0,
        "exit_z": 0.9,
        "quantity": 1,
        "slippage_ticks": 1.0,
    }
    seen = []

    def evaluate(p):
        seen.append(p)
        case.assertLess(p["exit_z"], p["entry_z"])
        if p["quantity"] > 1:
            raise W0Error("capital")
        return {"utility": 100 - 20 * abs(p["lookback"] - 10)}

    land = Landscape(axes, evaluate)
    window = land.profile(seed, "lookback")
    case.assertEqual(window["score"], 60)
    case.assertEqual([n["value"] for n in window["neighbors"]], [8, 9, 11, 12])
    case.assertTrue(all(p["entry_z"] == 1.0 for p in seen))
    entry = land.profile(seed, "entry_z")
    case.assertEqual(entry["score"], 100)
    case.assertEqual([n["value"] for n in entry["neighbors"]], [1.1, 1.2])
    blocked = land.profile(seed, "quantity")
    case.assertIsNone(blocked["score"])
    case.assertTrue(all(n["reason"] == "capital" for n in blocked["neighbors"]))
    negative = Landscape({"window": Axis(3, 60, 1)}, lambda _: {"utility": -2})
    case.assertEqual(negative.profile({"window": 20}, "window")["score"], 0)


def check_tpe_selects_the_highest_score_even_at_an_unstable_peak(case):
    axes = STRATEGY_AXES["funding_only"]
    seed = {"lookback": 28, "quantity": 1, "slippage_ticks": 1.5}

    def objective(p):
        case.assertEqual((p["quantity"], p["slippage_ticks"]), (1, 1.5))
        v = p["lookback"]
        return {"utility": 200 if v == 28 else 70 if 8 <= v <= 23 else -1}

    land = Landscape(axes | EXECUTION_AXES, objective)
    chosen, tries, _ = _tune(land, seed, axes, 100)
    case.assertEqual(tries, TRIALS)
    case.assertEqual(chosen["lookback"], 28)
    case.assertLess(land.profile(chosen, "lookback")["score"], GREEN)
    flat = Landscape(axes | EXECUTION_AXES, lambda _: {"utility": 0})
    case.assertEqual(_tune(flat, seed, axes, 100)[0], seed)
    losing = Landscape(axes | EXECUTION_AXES, lambda p: {"utility": -1 - abs(28 - p["lookback"])})
    case.assertEqual(_tune(losing, seed, axes, 100)[0], seed)


def check_missing_exposure_names_each_period_and_counts_a_carried_spread(case):
    dates = [f"2026-11-{day:02}" for day in range(1, 9)]
    split = EvaluationSplit.from_dates(dates, 50)
    marks = dict.fromkeys(dates, 1.0)
    for span, missing in (
        ((5, 6), "только на обучении"),
        ((1, 4), "только на проверке"),
        (None, "ни на обучении, ни на проверке"),
        (
            (4, 5),
            None,
        ),  # Enter on the last training date, exit on the first validation date.
    ):
        with case.subTest(span=span):
            trades = (
                []
                if span is None
                else [
                    {
                        "entry": {"date": dates[span[0] - 1]},
                        "exit": {"date": dates[span[1] - 1]},
                        "margin_rub": 100,
                        "lqdt_on_margin_rub": 0,
                    }
                ]
            )
            proof = {
                "selection": {"strategy": "funding_only"},
                "window": {
                    "passes_capital_screen": True,
                    "tradable_every_day": True,
                },
                "ledger": [{"date": day, "cumulative_net_rub": 0} for day in marks],
                "trades": trades,
            }
            measured = _measure(proof, marks, split, ResearchSettings())
            if missing is None:
                case.assertEqual(
                    measured["utility"], 0
                )  # Measured flat result, not missing evidence.
                case.assertNotIn("reason", measured)
            else:
                case.assertIsNone(measured["utility"])
                case.assertEqual(measured["reason"], f"Открытого спреда не было {missing}.")


def check_benchmark_relative_drawdown_and_shortfall_no_longer_veto_a_candidate(case):
    # A book that falls far behind LQDT stays a candidate: the measured
    # excess is the score, and the risk path stays visible in the proof.
    # The account stays solvent, so the annualization guard still applies.
    days = [f"2026-01-0{i}" for i in range(1, 5)]
    proof = {
        "selection": {"strategy": "funding_only"},
        "window": {"passes_capital_screen": True, "tradable_every_day": True},
        "sizing": {"initial_capital_rub": 100_000.0},
        "ledger": [
            {"date": d, "cumulative_net_rub": x}
            for d, x in zip(days, [0, -12_000, -16_000, -18_000], strict=False)
        ],
        "trades": [
            {
                "entry": {"date": days[0]},
                "exit": {"date": days[-1]},
                "margin_rub": 100,
                "lqdt_on_margin_rub": 4,
            }
        ],
    }
    marks = dict.fromkeys(days, 1.0)
    measured = _measure(proof, marks, EvaluationSplit.from_dates(days, 0), ResearchSettings())
    case.assertIsNotNone(measured["rank_score"])
    case.assertIsNotNone(measured["utility"])
    case.assertNotIn("просадки", str(measured.get("reason", "")))
    case.assertNotIn("отставания", str(measured.get("reason", "")))


def check_mark_change_including_first_validation_session_not_exit_trade_sum(case):
    days = [f"2026-01-0{i}" for i in range(1, 5)]
    proof = {
        "selection": {"strategy": "funding_only"},
        "window": {"passes_capital_screen": True, "tradable_every_day": True},
        "ledger": [
            {"date": d, "cumulative_net_rub": x}
            for d, x in zip(days, [0, 10, 70, 72], strict=False)
        ],
        "trades": [
            {
                "entry": {"date": days[0]},
                "exit": {"date": days[-1]},
                "margin_rub": 100,
                "lqdt_on_margin_rub": 4,
            }
        ],
    }
    marks = dict(zip(days, (1, 1.01, 1.02, 1.04), strict=False))
    for percent, expected in [
        (0, (72, 68, 17)),
        (50, (62, 59, 29.5)),
        (1, (2, 0, 0)),
    ]:
        with case.subTest(percent=percent):
            m = _measure(
                proof,
                marks,
                EvaluationSplit.from_dates(days, percent),
                ResearchSettings(),
            )
            case.assertEqual((m["net_rub"], m["excess_rub"], m["rub_per_session"]), expected)
    proof["trades"] = []
    no_check = _measure(proof, marks, EvaluationSplit.from_dates(days, 0), ResearchSettings())
    case.assertEqual(no_check["reason"], "Открытого спреда не было на обучении.")
    case.assertIsNone(no_check["validation"])


def check_daily_hurdle_marks_positions_across_a_split_and_reconciles_at_exit(case):
    proof = {
        "ledger": [
            {"date": d, "cumulative_net_rub": x}
            for d, x in zip("abcd", [0, 10, 20, 31], strict=False)
        ],
        "trades": [
            {
                "entry": {"date": "a"},
                "exit": {"date": "c"},
                "margin_rub": 100,
                "lqdt_on_margin_rub": 3.33,
            }
        ],
    }
    curve = _excess_curve(proof, {"a": 1, "b": 1.01, "c": 1.0333333, "d": 1.5})
    case.assertAlmostEqual(curve["b"], 9)
    case.assertAlmostEqual(curve["d"], 31 - 3.33)
