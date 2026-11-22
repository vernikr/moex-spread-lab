"""Scale fixed plugin series without changing the original intent rows."""

import math
from typing import Any

from moex_spread_lab.core.w0 import W0Error


def _scaled_series(unit: dict[str, Any], template: dict[str, Any], quantity: int) -> list[dict]:
    """Sizing rows scaled to the quantity and merged over the template rows."""
    originals = {row["date"]: row for row in template["series"]}
    rows = []
    for raw in unit["series"]:
        amount = raw["funding_rub"]
        if amount is not None and not math.isfinite(amount * quantity):
            raise W0Error("Переполнение суммы фондирования.")
        rows.append(
            {
                **originals[raw["date"]],
                **raw,
                "funding_rub": round(amount * quantity, 8) if amount is not None else None,
                "funding_contracts": quantity * raw["funding_contracts"],
                "signal_z": None,
            }
        )
    return rows
