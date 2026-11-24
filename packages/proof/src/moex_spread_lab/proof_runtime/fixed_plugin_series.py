"""Scale fixed plugin series without changing the original intent rows."""

import math
from typing import Any

from moex_spread_lab.core.w0 import W0Error

from .plugin_notes import merge_pair_series_row


def _scaled_series(
    unit: dict[str, Any], template: dict[str, Any], quantity: int, *, builtin: bool = False
) -> list[dict[str, Any]]:
    """Sizing rows scaled to the quantity and merged over the template rows."""
    originals = {row["date"]: row for row in template["series"]}
    rows: list[dict[str, Any]] = []
    for raw in unit["series"]:
        amount = raw["funding_rub"]
        if amount is not None and not math.isfinite(amount * quantity):
            raise W0Error("Переполнение суммы фондирования.")
        merged = merge_pair_series_row(originals[raw["date"]], raw, builtin=builtin)
        merged["funding_rub"] = round(amount * quantity, 8) if amount is not None else None
        merged["funding_contracts"] = quantity * raw["funding_contracts"]
        rows.append(merged)
    return rows


scaled_series = _scaled_series
