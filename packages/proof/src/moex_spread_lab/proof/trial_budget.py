"""Optuna trial budget for a strategy search (plan S4b, work 4).

The production budget is fixed at 80 trials per strategy family. A browser smoke that
asserts only UI behaviour may lower it, but only under the offline contract and only
downwards, so a leaked variable can never raise the budget and a research run can never
be silently shortened by it.
"""

from __future__ import annotations

import os

TRIALS_PER_STRATEGY = 80
UI_TRIALS_ENV = "MOEX_UI_SMOKE_TRIALS"


def budget() -> int:
    """Trials per strategy family for this process: 80, or an offline smoke's bound."""
    if os.environ.get("MOEX_OFFLINE_ONLY") != "1":
        return TRIALS_PER_STRATEGY
    raw = os.environ.get(UI_TRIALS_ENV, "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        return TRIALS_PER_STRATEGY
    return min(int(raw), TRIALS_PER_STRATEGY)
