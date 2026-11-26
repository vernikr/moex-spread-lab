"""MOEX ISS W1 catalog, candle retrieval, and EOD futures history facade."""

from __future__ import annotations

import urllib.request
from collections.abc import Callable
from typing import Any

from .iss_candles import (
    RawSinkCallback,
    analyze_candle_pair,
    fetch_candle_series,
    normalize_candles,
)
from .iss_coverage import (
    CSV_COLUMNS,
    fetch_candle_coverage,
    normalize_candle_borders,
    write_timeframe_inventory,
)
from .iss_history import (
    HISTORY_FIELDS,
    HISTORY_PAGE_SIZE,
    HISTORY_SCHEMA,
    IssHistoryError,
    fetch_history,
)
from .iss_history import (
    HISTORY_JSON_CACHE as _HISTORY_JSON_CACHE,
)
from .iss_history import (
    fetch_history_json as _fetch_history_json,
)
from .iss_history import (
    history_rows as _history_rows,
)
from .iss_history import (
    history_url as _history_url,
)
from .iss_http import (
    CANDLE_FIELDS,
    CANDLE_INTERVAL_LABELS,
    CANDLE_PAGE_SIZE,
    DEFAULT_BOARD,
    DEFAULT_FAR,
    DEFAULT_ISS_BASE_URL,
    DEFAULT_NEAR,
    HISTORICAL_SPECIFICATION_STATUS,
    ISS_MODULE_SCHEMA,
    MARKETDATA_FIELDS,
    PROJECT_RIGHTS_STATUS,
    SECURITY_FIELDS,
    IssIntakeError,
    fetch_json_with_raw,
)
from .iss_http import (
    candleborders_url as _candleborders_url,
)
from .iss_http import (
    candles_url as _candles_url,
)
from .iss_http import (
    fetch_json as _fetch_json,
)
from .iss_http import (
    rows as _rows,
)
from .iss_http import (
    url as _url,
)

__all__ = [
    "CANDLE_FIELDS",
    "CANDLE_INTERVAL_LABELS",
    "CANDLE_PAGE_SIZE",
    "CSV_COLUMNS",
    "DEFAULT_BOARD",
    "DEFAULT_FAR",
    "DEFAULT_ISS_BASE_URL",
    "DEFAULT_NEAR",
    "HISTORICAL_SPECIFICATION_STATUS",
    "HISTORY_FIELDS",
    "HISTORY_PAGE_SIZE",
    "HISTORY_SCHEMA",
    "ISS_MODULE_SCHEMA",
    "MARKETDATA_FIELDS",
    "PROJECT_RIGHTS_STATUS",
    "SECURITY_FIELDS",
    "_HISTORY_JSON_CACHE",
    "IssHistoryError",
    "IssIntakeError",
    "RawSinkCallback",
    "_candidate_status",
    "_candleborders_url",
    "_candles_url",
    "_fetch_history_json",
    "_fetch_json",
    "_history_rows",
    "_history_url",
    "_quote_quality",
    "_rows",
    "_url",
    "analyze_candle_pair",
    "fetch_candle_coverage",
    "fetch_candle_series",
    "fetch_history",
    "fetch_iss_catalog",
    "fetch_json_with_raw",
    "normalize_candle_borders",
    "normalize_candles",
    "normalize_iss_intake",
    "normalize_iss_payload",
    "write_timeframe_inventory",
]


def _quote_quality(bid: Any, ask: Any) -> str:
    if bid is None or ask is None:
        return "missing_quote"
    if bid > ask:
        return "crossed_quote"
    return "two_sided_quote"


def _normalize_single_security(
    security: dict[str, Any], market_by_sec_id: dict[Any, dict[str, Any]]
) -> dict[str, Any]:
    secid = security.get("SECID")
    if not isinstance(secid, str) or not secid:
        raise IssIntakeError("ISS security has no SECID")
    market = market_by_sec_id.get(secid, {})
    expiry = security.get("LASTDELDATE") or security.get("LASTTRADEDATE")
    if not isinstance(expiry, str) or not expiry:
        raise IssIntakeError(f"ISS security has no expiry/date: {secid}")
    board = market.get("BOARDID", "RFUD")
    bid = market.get("BID")
    ask = market.get("OFFER")
    return {
        "stable_id": f"moex:{board}:{secid}:{expiry}",
        "secid": secid,
        "board": board,
        "security": security.get("SHORTNAME"),
        "short_code": secid,
        "asset_code": security.get("ASSETCODE"),
        "expiry": expiry,
        "first_trade": security.get("FRSTTRADE"),
        "lot_size": security.get("LOTVOLUME"),
        "tick_size": security.get("MINSTEP"),
        "tick_value": security.get("STEPPRICE"),
        "initial_margin": security.get("INITIALMARGIN"),
        "settlement": market.get("SETTLEPRICE"),
        "last_trade": market.get("LAST"),
        "analytical_close": None,
        "last_trade_close": None,
        "bid": bid,
        "ask": ask,
        "live_mark": None,
        "quote_quality": _quote_quality(bid, ask),
        "source_times": {
            "event_at": market.get("TIME"),
            "source_published_at": market.get("UPDATETIME"),
            "source_system_at": market.get("SYSTIME"),
            "trade_date": market.get("TRADEDATE"),
        },
        "source_vintage": "current_snapshot",
        "historical_evidence": False,
        "historical_specification_basis": HISTORICAL_SPECIFICATION_STATUS,
    }


def normalize_iss_payload(
    payload: dict[str, Any], *, url: str, received_at: str, raw_sha256: str
) -> dict[str, Any]:
    securities = _rows(payload.get("securities"), "securities")
    marketdata = _rows(payload.get("marketdata"), "marketdata")
    market_by_sec_id = {row.get("SECID"): row for row in marketdata}
    instruments = sorted(
        (_normalize_single_security(sec, market_by_sec_id) for sec in securities),
        key=lambda item: str(item["stable_id"]),
    )
    quality_summary: dict[str, int] = {}
    for instrument in instruments:
        quality = str(instrument["quote_quality"])
        quality_summary[quality] = quality_summary.get(quality, 0) + 1
    return {
        "schema_version": ISS_MODULE_SCHEMA,
        "source": {
            "provider": "moex_iss",
            "url": url,
            "received_at": received_at,
            "raw_response_sha256": raw_sha256,
            "rights_status": PROJECT_RIGHTS_STATUS,
            "rights_basis": "user_decision_A-05",
            "network_mode": "read_only_probe",
        },
        "instrument_count": len(instruments),
        "quote_quality_summary": quality_summary,
        "instruments": instruments,
    }


def _evaluate_candidate_pair(
    near: dict[str, Any], far: dict[str, Any]
) -> tuple[list[str], list[str]]:
    reasons: list[str] = []
    if near["asset_code"] not in ("CNYRUBTOM", "CNY") or far["asset_code"] != "CNY":
        reasons.append("asset-code relationship is not the configured CNY/RUB candidate")
    if near["source_vintage"] != "current_snapshot" or far["source_vintage"] != "current_snapshot":
        reasons.append("unexpected source-vintage state")
    assumptions = [
        "historical contract parameters use the current snapshot until a dated specification is available"
    ]
    reasons.append("historical executable bid/ask is unavailable; execution replay remains blocked")
    if any(item["quote_quality"] != "two_sided_quote" for item in (near, far)):
        reasons.append("one or both candidate instruments have invalid or missing current quotes")
    return reasons, assumptions


def _candidate_status(
    instruments: list[dict[str, Any]], near_secid: str, far_secid: str
) -> dict[str, Any]:
    by_sec_id = {item["secid"]: item for item in instruments}
    near = by_sec_id.get(near_secid)
    far = by_sec_id.get(far_secid)
    reasons: list[str] = []
    assumptions: list[str] = []
    if near is None:
        reasons.append(f"near instrument not found: {near_secid}")
    if far is None:
        reasons.append(f"far instrument not found: {far_secid}")
    if near is not None and far is not None:
        pair_reasons, assumptions = _evaluate_candidate_pair(near, far)
        reasons.extend(pair_reasons)
    both_found = near is not None and far is not None
    has_invalid = any("invalid or missing" in reason for reason in reasons)
    return {
        "candidate_id": f"{near_secid}/{far_secid}",
        "near_secid": near_secid,
        "far_secid": far_secid,
        "status": "conditionally_admitted_analytical_only"
        if both_found and not has_invalid
        else "not_admitted",
        "reasons": reasons,
        "assumptions": assumptions,
        "execution_status": "blocked_historical_quote_evidence" if both_found else "not_evaluable",
    }


def normalize_iss_intake(
    payload: dict[str, Any],
    *,
    url: str,
    received_at: str,
    raw_sha256: str,
    near_secid: str = DEFAULT_NEAR,
    far_secid: str = DEFAULT_FAR,
) -> dict[str, Any]:
    normalized = normalize_iss_payload(
        payload, url=url, received_at=received_at, raw_sha256=raw_sha256
    )
    normalized["candidate"] = _candidate_status(normalized["instruments"], near_secid, far_secid)
    normalized["admission_status"] = normalized["candidate"]["status"]
    return normalized


def fetch_iss_catalog(
    *,
    base_url: str = DEFAULT_ISS_BASE_URL,
    board: str = DEFAULT_BOARD,
    timeout: float = 20.0,
    near_secid: str = DEFAULT_NEAR,
    far_secid: str = DEFAULT_FAR,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    url = _url(base_url, board)
    payload, received_at, raw_sha256, _ = fetch_json_with_raw(
        url, timeout=timeout, opener=opener, probe="read-only"
    )
    return normalize_iss_intake(
        payload,
        url=url,
        received_at=received_at,
        raw_sha256=raw_sha256,
        near_secid=near_secid,
        far_secid=far_secid,
    )
