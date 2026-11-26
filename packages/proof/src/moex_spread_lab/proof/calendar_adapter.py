"""Adapter that turns a native calendar payload into an `execution-proof.v1` payload."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from moex_spread_lab.accounting.costs import cost_totals as _cost_totals
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import run_id
from moex_spread_lab.strategies import REFERENCE_WARNING, attach_entry_levels

from .payload_tools import (
    FORCED_EXIT_REASONS,
    PROOF_SCHEMA,
    events,
    load_instruments,
    result_label,
    sha256_bytes,
)


def _label_trades(trades: list[dict[str, Any]]) -> dict[str, str]:
    """Recompute gross points, result labels and forced-exit flags for every trade."""
    for trade in trades:
        direction = 1 if trade["side"] == "long_spread" else -1
        trade["gross_pts"] = round(
            (trade["exit"]["spread_fill"] - trade["entry"]["spread_fill"]) * direction, 3
        )
        trade["result"] = result_label(trade["net_rub"])
        trade["open_at_end"] = trade["reason"] in FORCED_EXIT_REASONS
    return events(trades)


def _label_ledger(ledger_rows: list[dict[str, Any]], labels: dict[str, str]) -> None:
    for row in ledger_rows:
        row["funding_rub"] = None
        row["lqdt_on_margin_rub"] = None
        row["event"] = labels.get(row["date"])


def _summarize(summary: dict[str, Any], trades: list[dict[str, Any]]) -> None:
    summary["cost_totals"] = _cost_totals(trades)
    summary["closed_trades"] = sum(not trade["open_at_end"] for trade in trades)
    summary["open_positions"] = sum(trade["open_at_end"] for trade in trades)
    summary.setdefault("max_excess_drawdown_rub", None)


def _price_role(
    payload: dict[str, Any], selection: dict[str, Any], timeframe: str, fill: str
) -> str:
    window: dict[str, Any] = payload.get("window") or {}
    price_source = window.get("price_source") or selection.get("price_source")
    if price_source == "synthetic_leg_proxy":
        return f"Синтетический H1 proxy far − near по синхронным свечам ног; исполнение {fill}"
    if timeframe == "1h":
        return f"Нативные часовые свечи календарного спреда; исполнение {fill}"
    return f"Нативный календарный спред; исполнение {fill}, отметки по официальному расчёту"


def calendar_proof(
    root: Path, selection: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    payload = dict(payload)
    payload["universe"] = CALENDAR_UNIVERSE
    payload["schema_version"] = PROOF_SCHEMA
    embedded = payload.get("selection")
    selection_out: dict[str, Any]
    if isinstance(embedded, dict):
        selection_out = cast(dict[str, Any], embedded)
        selection_out["universe"] = CALENDAR_UNIVERSE
    else:
        selection_out = {}
    trades: list[dict[str, Any]] = payload.get("trades", [])
    labels = _label_trades(trades)
    ledger: list[dict[str, Any]] = payload.get("ledger") or []
    _label_ledger(ledger, labels)
    series: list[dict[str, Any]] = payload.get("series") or []
    for row in series:
        row.setdefault("funding_rub", None)
        row.setdefault("signal_z", None)
    _summarize(payload["summary"], trades)
    provenance: dict[str, Any] = payload.get("provenance") or {}
    payload["provenance"] = {
        **calendar_provenance(root, selection_out),
        **provenance,
    }
    payload["instrument"] = calendar_instrument(root, selection_out, payload)
    attach_entry_levels(payload)
    if payload["execution_reference"]:
        payload["notes"].append(REFERENCE_WARNING)
    window: dict[str, Any] = payload.get("window") or {}
    timeframe: str = selection_out.get("timeframe") or window.get("timeframe") or "1d"
    selection_out.setdefault("timeframe", timeframe)
    fill = (selection.get("fill") or "next_open").replace("_", " ")
    payload["price_role"] = _price_role(payload, selection_out, timeframe, fill)
    payload["run_id"] = run_id(
        {
            "selection": payload["selection"],
            "daily": payload.get("series"),
            "source_hashes": payload["provenance"],
        }
    )
    return payload

    # ----------------------------------------------------------- internals


def _history_digests(cache: Path, names: dict[str, str | None]) -> dict[str, str]:
    """Hashes of the cached ISS histories for the legs and the spread instrument."""
    provenance: dict[str, str] = {}
    for label, secid in names.items():
        if not secid:
            continue
        matches = sorted(cache.glob(f"{secid}-*.json")) if cache.is_dir() else []
        if matches:
            digest = sha256_bytes(matches[-1])
            if digest:
                provenance[f"{label}_sha256"] = digest
    return provenance


def calendar_provenance(root: Path, selection: dict[str, Any]) -> dict[str, str]:
    legs = str(selection.get("pair") or "").split("/")
    provenance = _history_digests(
        root / "data/spread-history-cache",
        {
            "near_history": legs[0] if legs and legs[0] else None,
            "far_history": legs[1] if len(legs) > 1 and legs[1] else None,
            "spread_history": selection.get("spread_instrument"),
        },
    )
    for label, relative in (
        ("contracts", "data/w1/current-intake.json"),
        ("benchmark", "data/benchmark/lqdt-lineage.json"),
        ("ruonia", "data/benchmark-cache/CBR_RUONIA.json"),
    ):
        digest = sha256_bytes(root / relative)
        if digest:
            provenance[f"{label}_sha256"] = digest
    return provenance


def calendar_instrument(
    root: Path, selection: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    window: dict[str, Any] = payload.get("window") or {}
    contract: dict[str, Any] = payload.get("contract") or {}
    legs = str(selection.get("pair") or "").split("/")
    near = window.get("near") or (legs[0] if legs and legs[0] else None)
    far = window.get("far") or (legs[1] if len(legs) > 1 and legs[1] else None)
    instruments = load_instruments(root / "data/w1/current-intake.json")
    point_value_rub = contract.get("point_value_rub")
    near_instrument: dict[str, Any] = instruments.get(near or "") or {}
    far_instrument: dict[str, Any] = instruments.get(far or "") or {}
    return {
        "near": near,
        "far": far,
        "family": "calendar_spread",
        "near_expiry": near_instrument.get("expiry") or window.get("near_expiry"),
        "far_expiry": far_instrument.get("expiry"),
        "near_point_value_rub": point_value_rub,
        "far_point_value_rub": point_value_rub,
        "spread_instrument": selection.get("spread_instrument"),
    }
