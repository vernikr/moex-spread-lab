"""Measure the full continuous-ledger training or validation period."""

from typing import Any, cast

from moex_spread_lab.core.evaluation_periods import EvaluationSplit, ResearchSettings


def measure_full(
    proof: dict[str, Any],
    marks: dict[str, float],
    split: EvaluationSplit,
    settings: ResearchSettings,
    facade: Any,
) -> dict[str, Any]:
    """Measure a continuous ledger; the last training mark is validation's opening balance."""
    if "_scalar_train_summary" in proof and not split.validation:
        return facade._measure_scalar_train(proof, marks, split, settings)
    if reason := facade._admission(proof, settings):
        return {"utility": None, "reason": reason}
    curve = facade._excess_curve(proof, marks)
    train_x = curve[split.training[-1]]
    train_rate = train_x / len(split.training)
    result_x, rate, validation_rate = train_x, train_rate, None
    segments = [("на обучении", split.training)]
    if split.validation:
        result_x = curve[split.validation[-1]] - train_x
        rate = validation_rate = result_x / len(split.validation)
        segments.append(("на проверке", split.validation))
    if not all(facade.math.isfinite(v) for v in (train_rate, rate, result_x)):
        return {"utility": None, "reason": "Нет конечной оценки результата."}
    # Descriptive P&L is separate from eligibility: a flat, unexposed book is not a plateau.
    net = {r["date"]: r["cumulative_net_rub"] for r in cast(list[dict[str, Any]], proof["ledger"])}
    net_change = (
        net[split.validation[-1]] - net[split.training[-1]]
        if split.validation
        else net[split.training[-1]]
    )
    metrics = {
        "net_rub": round(net_change, 2),
        "lqdt_on_margin_rub": round(net_change - result_x, 2),
        "train": round(train_rate, 6),
        "validation": round(validation_rate, 6) if validation_rate is not None else None,
        "excess_rub": round(result_x, 2),
        "rub_per_session": round(rate, 6),
    }
    missing, reasons = facade._missing_exposure(proof, segments, split)
    try:
        annual = facade._annualized_returns(proof, marks, split)
    except facade.W0Error as exc:
        return {
            **metrics,
            "utility": None,
            "rank_score": None,
            "reason": " ".join([*reasons, str(exc)]),
        }
    if annual.pop("cash_shortfall"):
        reasons.append("Свободные средства недостаточны для маржи.")
        return {
            **metrics,
            "rank_score": None,
            "utility": None,
            "reason": " ".join(reasons),
        }
    # An honestly inactive book holds LQDT: include its zero in the fixed denominator.
    # It is not evidence of a stable trading neighborhood. A risk/data failure is NOT zero.
    # Benchmark-relative drawdown/shortfall limits no longer veto a candidate: the
    # measured excess over LQDT is the score, and the risk path stays visible in the
    # proof itself.
    score = annual["annualized_excess_pp"]
    return {
        **metrics,
        **annual,
        "rank_score": score,
        "utility": None if missing else score,
        **({"reason": " ".join(reasons)} if reasons else {}),
    }


def admission(proof: dict[str, Any], settings: ResearchSettings, facade: Any) -> str | None:
    if "min_leg_volume" in proof["window"] and proof["window"]["min_leg_volume"] is None:
        return "Нет наблюдений с положительным объёмом обеих ног."
    if not settings.allow_failed_capital and not proof["window"]["passes_capital_screen"]:
        return "Не пройден стресс-тест капитала."
    if not settings.allow_failed_volume and not proof["window"]["tradable_every_day"]:
        return "Не пройдено правило ликвидности."
    if (
        proof["selection"]["strategy"] == "mean_reversion"
        and proof["instrument"]["family"] == "perpetual_quarterly_same_asset"
        and facade.get_timeframe(
            proof["selection"].get("timeframe", "1d")
        ).supports_funding_strategy
        and not proof["provenance"].get("near_funding_history_sha256")
    ):
        return "Нет полного ряда SWAPRATE."
    return None
