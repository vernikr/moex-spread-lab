"""Shared note wording for the pair-plugin proof routes."""

from __future__ import annotations

from typing import Any

OPEN_POSITION_NOTE = (
    "Открытая на конец выборки позиция — принудительный прокси-выход, не исполнимая сделка."
)
OPEN_POSITION_PREFIX = OPEN_POSITION_NOTE[:40]
PLUGIN_SOURCE_NOTE = "Решения — из проверенных намерений плагина; цены и учёт рассчитаны отдельно."


def merge_pair_series_row(
    template_row: dict[str, Any], actual_row: dict[str, Any], *, builtin: bool
) -> dict[str, Any]:
    """Merge plugin accounting into one native series row.

    A third-party plugin supplies indicator values of its own, so ``signal_z`` is cleared and
    its extra columns are kept. A built-in plugin runs the same engine as the native proof, so
    its row must stay byte-identical to the native one: native columns win and plugin-only
    columns are dropped.
    """
    if builtin:
        row = {key: actual_row.get(key, value) for key, value in template_row.items()}
        row["signal_z"] = template_row.get("signal_z")
        return row
    return {**template_row, **actual_row, "signal_z": None}


def apply_pair_notes(template: dict[str, Any], trades: list[dict[str, Any]]) -> None:
    """Attach drop-in provenance notes; built-in payloads keep their native wording."""
    template["notes"] = [
        note for note in template["notes"] if not note.startswith(OPEN_POSITION_PREFIX)
    ]
    if any(trade["open_at_end"] for trade in trades):
        template["notes"].append(OPEN_POSITION_NOTE)
    template["notes"].append(PLUGIN_SOURCE_NOTE)
