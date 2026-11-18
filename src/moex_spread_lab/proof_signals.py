"""Signal-date entry boundaries in the raw spread's own price units.

These are strategy thresholds, not executable orders or extra entry signals. Position,
volume, expiry and funded-account gates still apply; delayed fills occur on later bars.
"""

from __future__ import annotations

import math
import statistics

from .analytics import _rolling_stats
from .w0 import W0Error

PRICE_FILLS = {
    "next_open": (1, "open"),
    "next_close": (1, "close"),
    "reference_same_bar": (0, "close"),
}
REFERENCE_WARNING = (
    "Справочный режим: сигнал и цена закрытия одного бара. После расчёта сигнала "
    "эта цена уже известна; результат не является проверкой исполнимой доходности."
)


def pair_fill(request, strategy):
    default = "same_day_settlement" if strategy == "funding_only" else "next_open"
    fill = request.get("fill", default)
    allowed = ("same_day_settlement",) if strategy == "funding_only" else PRICE_FILLS
    if not isinstance(fill, str) or fill not in allowed:
        raise W0Error("Выбранный режим исполнения не поддерживается этой стратегией.")
    return fill


def attach_entry_levels(proof):
    """Attach two causal entry bands without changing any signal, trade or indicator.

    Pair MR uses population deviation and excludes the signal bar. Native-calendar MR
    uses sample deviation and includes it only in the existing same-bar reference mode.
    Native indicators operate on RUB values, so FX-dependent raw point thresholds must
    be converted with the signal date's point value, not a constant multiplier.
    """
    selection = proof["selection"]
    strategy = selection["strategy"]
    native = proof.get("universe") == "calendar_native"
    params = selection["parameters"]
    reference = selection.get("fill") == "reference_same_bar"
    proof["execution_reference"] = selection["execution_reference"] = reference
    note = (
        "Пороги входа по закрытию сигнального бара: покупка спреда при цене не выше "
        "нижней линии, продажа — не ниже верхней. Только при отсутствии позиции и "
        "пройденных ограничениях; выходы используют собственные правила стратегии. "
        "Это не цены фактического исполнения."
    )
    if strategy == "funding_only":
        proof["signal_levels"] = {
            "kind": "not_price_based",
            "note": "У стратегии фондирования нет двух ценовых порогов: вход определяется историей ставок, а не пересечением цены спреда.",
        }
        return
    proof["signal_levels"] = {
        "kind": "entry_bands",
        "basis": "signal_bar_close",
        "note": note,
        "fill_delay_bars": 0 if reference else 1,
    }
    history = []
    for row in proof["series"]:
        for key in ("buy_level", "sell_level", "buy_level_rel", "sell_level_rel"):
            row[key] = None
        if not row.get("volume_eligible", True):
            continue
        point = row.get("point_value_rub", 1.0) if native else 1.0
        value = row["spread"] * point
        levels = None
        if strategy in ("mean_reversion", "zscore_mean_reversion"):
            window = int(params["window" if native else "lookback"])
            sample = (
                (history + [value])[-window:]
                if native and reference
                else history[-window:]
            )
            if len(sample) == window:
                mean, std = (
                    (statistics.mean(sample), statistics.stdev(sample))
                    if native
                    else _rolling_stats(sample)
                )
                if std > 0:
                    width = params["entry_z"] * std
                    levels = ((mean - width) / point, (mean + width) / point)
        elif strategy == "convergence_to_ruonia":
            dev, scale = row.get("dev_pp"), row.get("rate_price_per_pp")
            if dev is not None and scale is not None and math.isfinite(dev):
                threshold = params["threshold_pp"]
                levels = (
                    row["spread"] + (-threshold - dev) * scale,
                    row["spread"] + (threshold - dev) * scale,
                )
        history.append(value)
        if levels is not None:
            row["buy_level"], row["sell_level"] = levels
            far = row["far"] * (
                proof["instrument"]["far_point_value_rub"] if not native else 1.0
            )
            if far:
                row["buy_level_rel"], row["sell_level_rel"] = (
                    level / far * 100 for level in levels
                )
