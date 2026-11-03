"""Read-only MOEX EOD history and funding-aware CNY carry decomposition."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import urlopen

from .analytics import load_contracts
from .w0 import W0Error


CARRY_SCHEMA = "carry-study.v1"
HISTORY_SCHEMA = "iss-history.v1"
HISTORY_PAGE_SIZE = 100
HISTORY_FIELDS = (
    "BOARDID", "TRADEDATE", "SECID", "CLOSE", "SETTLEPRICE", "SWAPRATE",
    "OPENPOSITION", "VALUE", "VOLUME", "WAPRICE", "NUMTRADES", "ASSETCODE",
)


class CarryError(W0Error):
    """Raised when EOD carry inputs fail the funding-aware study contract."""


def _round(value: float) -> float:
    return round(float(value), 8)


def _fetch_json(url: str, *, timeout: float = 30.0, opener: Callable[..., Any] = urlopen) -> tuple[dict[str, Any], str]:
    try:
        with opener(url, timeout=timeout) as response:
            raw = response.read()
    except OSError as exc:
        raise CarryError(f"history request failed: {url}: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CarryError(f"history response is not JSON: {url}") from exc
    if not isinstance(value, dict):
        raise CarryError(f"history response is not an object: {url}")
    return value, hashlib.sha256(raw).hexdigest()


def _rows(block: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(block, dict) or not isinstance(block.get("columns"), list) or not isinstance(block.get("data"), list):
        raise CarryError(f"malformed ISS block: {name}")
    return [dict(zip(block["columns"], values)) for values in block["data"] if isinstance(values, list)]


def _history_url(secid: str, from_date: str, till_date: str, start: int) -> str:
    query = urlencode({
        "iss.meta": "off",
        "from": from_date,
        "till": till_date,
        "start": start,
        "limit": HISTORY_PAGE_SIZE,
        "history.columns": ",".join(HISTORY_FIELDS),
    })
    return f"https://iss.moex.com/iss/history/engines/futures/markets/forts/securities/{secid}.json?{query}"


def fetch_history(
    secid: str,
    from_date: str,
    till_date: str,
    *,
    cache_dir: Path | None = None,
    delay_seconds: float = 0.1,
    timeout: float = 30.0,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    if not secid or delay_seconds < 0:
        raise CarryError("invalid history arguments")
    all_rows: list[dict[str, Any]] = []
    hashes: list[str] = []
    page = 0
    total: int | None = None
    while total is None or len(all_rows) < total:
        start = page * HISTORY_PAGE_SIZE
        url = _history_url(secid, from_date, till_date, start)
        cache_path = cache_dir / f"{secid}-{from_date}-{till_date}-{start}.json" if cache_dir else None
        payload = None
        raw_hash = None
        if cache_path and cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if cached.get("url") == url:
                    payload = cached.get("payload")
                    raw_hash = cached.get("raw_response_sha256")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = None
        if payload is None:
            if delay_seconds:
                time.sleep(delay_seconds)
            payload, raw_hash = _fetch_json(url, timeout=timeout, opener=opener)
            if cache_path:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps({
                    "url": url,
                    "payload": payload,
                    "raw_response_sha256": raw_hash,
                }, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        block_rows = _rows(payload.get("history"), "history")
        cursor_rows = _rows(payload.get("history.cursor"), "history.cursor") if payload.get("history.cursor") else []
        if cursor_rows and cursor_rows[0].get("TOTAL") is not None:
            total = int(cursor_rows[0]["TOTAL"])
        if not block_rows:
            break
        all_rows.extend(block_rows)
        hashes.append(raw_hash or "")
        page += 1
        if len(block_rows) < HISTORY_PAGE_SIZE:
            break
    deduped = {row.get("TRADEDATE"): row for row in all_rows if row.get("TRADEDATE")}
    rows = [deduped[key] for key in sorted(deduped)]
    if not rows:
        raise CarryError(f"empty EOD history for {secid}")
    return {
        "schema_version": HISTORY_SCHEMA,
        "secid": secid,
        "from": from_date,
        "till": till_date,
        "row_count": len(rows),
        "page_count": page,
        "rows": rows,
        "source": {
            "endpoint": "MOEX ISS futures history",
            "raw_response_sha256": hashes,
            "rights_status": "accepted_project_assumption_personal_use",
        },
    }


def _normalize_history(source: dict[str, Any], secid: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if source.get("schema_version") != HISTORY_SCHEMA or source.get("secid") != secid:
        raise CarryError(f"unsupported history input for {secid}")
    normalized = {}
    for row in source.get("rows", []):
        if row.get("TRADEDATE") is None or row.get("SETTLEPRICE") is None:
            continue
        try:
            settle = float(row["SETTLEPRICE"])
            swaprate = float(row["SWAPRATE"]) if row.get("SWAPRATE") is not None else None
        except (TypeError, ValueError) as exc:
            raise CarryError(f"non-numeric history field for {secid} {row.get('TRADEDATE')}") from exc
        normalized[row["TRADEDATE"]] = {
            "date": row["TRADEDATE"],
            "settle": settle,
            "swaprate": swaprate,
            "open_position": row.get("OPENPOSITION"),
            "volume": row.get("VOLUME"),
            "value": row.get("VALUE"),
        }
    if len(normalized) < 2:
        raise CarryError(f"fewer than two settlement rows for {secid}")
    return normalized, {
        "secid": secid,
        "source": source.get("source", {}),
        "row_count": len(normalized),
        "first_date": min(normalized),
        "last_date": max(normalized),
    }


def build_carry_study(
    near_source: dict[str, Any],
    far_source: dict[str, Any],
    contracts_path: Path,
    *,
    near: str = "CNYRUBF",
    far: str = "CRZ6",
) -> dict[str, Any]:
    near_rows, near_provenance = _normalize_history(near_source, near)
    far_rows, far_provenance = _normalize_history(far_source, far)
    contracts, contracts_hash = load_contracts(contracts_path, near, far)
    near_contract = contracts[near]
    far_contract = contracts[far]
    if near_contract["tick_size"] != far_contract["tick_size"] or near_contract["tick_value"] != far_contract["tick_value"]:
        raise CarryError("carry study requires equal current tick conversion for the two legs")
    common_dates = sorted(set(near_rows) & set(far_rows))
    if len(common_dates) < 2:
        raise CarryError("fewer than two common EOD settlement dates")
    multiplier = near_contract["tick_value"] / near_contract["tick_size"]
    rows = []
    cumulative_mark = 0.0
    cumulative_funding = 0.0
    previous_date = None
    for date_key in common_dates:
        near_row = near_rows[date_key]
        far_row = far_rows[date_key]
        if previous_date is None:
            daily_basis_pnl = 0.0
        else:
            daily_basis_pnl = ((far_row["settle"] - far_rows[previous_date]["settle"]) - (near_row["settle"] - near_rows[previous_date]["settle"])) * multiplier
        swaprate = near_row["swaprate"]
        if swaprate is None:
            raise CarryError(f"missing CNYRUBF SWAPRATE on {date_key}")
        funding_received = 0.0 if previous_date is None else swaprate * near_contract["lot_size"]
        cumulative_mark += daily_basis_pnl
        cumulative_funding += funding_received
        rows.append({
            "date": date_key,
            "near_settle": near_row["settle"],
            "far_settle": far_row["settle"],
            "basis_far_minus_near": _round(far_row["settle"] - near_row["settle"]),
            "daily_basis_pnl_short_near_long_far": _round(daily_basis_pnl),
            "daily_funding_received_short_near": _round(funding_received),
            "daily_gross_carry_pnl": _round(daily_basis_pnl + funding_received),
            "cumulative_basis_pnl": _round(cumulative_mark),
            "cumulative_funding_received": _round(cumulative_funding),
            "cumulative_gross_carry_pnl": _round(cumulative_mark + cumulative_funding),
            "swaprate_source_unit": "RUB_per_contract_day_assumed_from_R06_and_ISS_history",
        })
        previous_date = date_key
    funding_values = [row["daily_funding_received_short_near"] for row in rows]
    return {
        "schema_version": CARRY_SCHEMA,
        "release_state": "exploratory_inconclusive",
        "study_role": "funding_aware_carry_decomposition_not_execution_backtest",
        "near": near,
        "far": far,
        "direction": "short_near_long_far",
        "price_role": "EOD_SETTLEPRICE",
        "funding_role": "CNYRUBF_SWAPRATE_received_by_short_near",
        "rows": rows,
        "summary": {
            "common_row_count": len(rows),
            "first_date": rows[0]["date"],
            "last_date": rows[-1]["date"],
            "total_basis_pnl": rows[-1]["cumulative_basis_pnl"],
            "total_funding_received": rows[-1]["cumulative_funding_received"],
            "total_gross_carry_pnl": rows[-1]["cumulative_gross_carry_pnl"],
            "mean_daily_funding_received": _round(sum(funding_values) / len(funding_values)),
            "positive_funding_rows": sum(value > 0 for value in funding_values),
            "negative_funding_rows": sum(value < 0 for value in funding_values),
            "zero_funding_rows": sum(value == 0 for value in funding_values),
            "transaction_costs_included": False,
            "historical_executable_fills": False,
        },
        "economics": {
            "near_lot_size": near_contract["lot_size"],
            "far_lot_size": far_contract["lot_size"],
            "tick_size": near_contract["tick_size"],
            "tick_value_rub": near_contract["tick_value"],
            "rub_per_price_unit": _round(multiplier),
            "historical_specification_status": "current_snapshot_fallback_assumed",
            "fixing_mismatch_risk": "CNYRUBF_TOM_vs_CRZ6_CNYFIXME",
        },
        "provenance": {
            "near_history": near_provenance,
            "far_history": far_provenance,
            "contracts_sha256": contracts_hash,
            "contracts_path": str(contracts_path),
        },
    }


def write_carry_study(result: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
