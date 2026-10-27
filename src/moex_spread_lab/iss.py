"""MOEX ISS W1 catalog and current price-role intake.

The adapter is intentionally read-only. It stores provenance and normalizes
the current snapshot without treating it as point-in-time historical evidence.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from .w0 import W0Error


ISS_MODULE_SCHEMA = "iss-intake.v1"
DEFAULT_ISS_BASE_URL = "https://iss.moex.com"
DEFAULT_BOARD = "RFUD"
DEFAULT_NEAR = "CNYRUBF"
DEFAULT_FAR = "CRZ6"

SECURITY_FIELDS = (
    "SECID",
    "SHORTNAME",
    "ASSETCODE",
    "FRSTTRADE",
    "LOTVOLUME",
    "MINSTEP",
    "STEPPRICE",
    "INITIALMARGIN",
    "LASTTRADEDATE",
    "LASTDELDATE",
)
MARKETDATA_FIELDS = (
    "SECID",
    "BOARDID",
    "BID",
    "OFFER",
    "SPREAD",
    "OPEN",
    "HIGH",
    "LOW",
    "LAST",
    "SETTLEPRICE",
    "OPENPOSITION",
    "NUMTRADES",
    "VOLTODAY",
    "VALTODAY",
    "UPDATETIME",
    "TIME",
    "SYSTIME",
    "SEQNUM",
    "TRADEDATE",
    "SWAPRATE",
)


class IssIntakeError(W0Error):
    """Raised when an ISS response cannot pass the W1 intake contract."""


def _url(base_url: str, board: str) -> str:
    root = base_url.rstrip("/")
    fields = ",".join(SECURITY_FIELDS)
    market_fields = ",".join(MARKETDATA_FIELDS)
    return (
        f"{root}/iss/engines/futures/markets/forts/boards/{board}/securities.json"
        "?iss.meta=off&iss.only=securities,marketdata"
        f"&securities.columns={fields}&marketdata.columns={market_fields}"
    )


def _rows(block: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(block, dict) or not isinstance(block.get("columns"), list) or not isinstance(block.get("data"), list):
        raise IssIntakeError(f"ISS block is malformed: {name}")
    columns = block["columns"]
    rows = []
    for index, values in enumerate(block["data"]):
        if not isinstance(values, list) or len(values) != len(columns):
            raise IssIntakeError(f"ISS row has invalid width: {name}[{index}]")
        rows.append(dict(zip(columns, values)))
    return rows


def normalize_iss_payload(payload: dict[str, Any], *, url: str, received_at: str, raw_sha256: str) -> dict[str, Any]:
    securities = _rows(payload.get("securities"), "securities")
    marketdata = _rows(payload.get("marketdata"), "marketdata")
    market_by_sec_id = {row.get("SECID"): row for row in marketdata}
    instruments = []
    for security in securities:
        secid = security.get("SECID")
        if not isinstance(secid, str) or not secid:
            raise IssIntakeError("ISS security has no SECID")
        market = market_by_sec_id.get(secid, {})
        expiry = security.get("LASTDELDATE") or security.get("LASTTRADEDATE")
        if not isinstance(expiry, str) or not expiry:
            raise IssIntakeError(f"ISS security has no expiry/date: {secid}")
        stable_id = f"moex:{market.get('BOARDID', 'RFUD')}:{secid}:{expiry}"
        bid = market.get("BID")
        ask = market.get("OFFER")
        if bid is None or ask is None:
            quote_quality = "missing_quote"
        elif bid > ask:
            quote_quality = "crossed_quote"
        else:
            quote_quality = "two_sided_quote"
        instruments.append({
            "stable_id": stable_id,
            "secid": secid,
            "board": market.get("BOARDID", "RFUD"),
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
            "quote_quality": quote_quality,
            "source_times": {
                "event_at": market.get("TIME"),
                "source_published_at": market.get("UPDATETIME"),
                "source_system_at": market.get("SYSTIME"),
                "trade_date": market.get("TRADEDATE"),
            },
            "source_vintage": "current_snapshot",
            "historical_evidence": False,
        })
    instruments.sort(key=lambda item: item["stable_id"])
    quality_summary: dict[str, int] = {}
    for instrument in instruments:
        quality = instrument["quote_quality"]
        quality_summary[quality] = quality_summary.get(quality, 0) + 1
    return {
        "schema_version": ISS_MODULE_SCHEMA,
        "source": {
            "provider": "moex_iss",
            "url": url,
            "received_at": received_at,
            "raw_response_sha256": raw_sha256,
            "rights_status": "unresolved_public_iss_terms",
            "network_mode": "read_only_probe",
        },
        "instrument_count": len(instruments),
        "quote_quality_summary": quality_summary,
        "instruments": instruments,
    }


def _candidate_status(instruments: list[dict[str, Any]], near_secid: str, far_secid: str) -> dict[str, Any]:
    by_sec_id = {item["secid"]: item for item in instruments}
    near = by_sec_id.get(near_secid)
    far = by_sec_id.get(far_secid)
    reasons: list[str] = []
    if near is None:
        reasons.append(f"near instrument not found: {near_secid}")
    if far is None:
        reasons.append(f"far instrument not found: {far_secid}")
    if near is not None and far is not None:
        if near["asset_code"] not in ("CNYRUBTOM", "CNY") or far["asset_code"] != "CNY":
            reasons.append("asset-code relationship is not the configured CNY/RUB candidate")
        if near["source_vintage"] != "current_snapshot" or far["source_vintage"] != "current_snapshot":
            reasons.append("unexpected source-vintage state")
        reasons.extend([
            "current snapshot is not historical point-in-time evidence",
            "historical settlement series and historical executable bid/ask are not present",
            "ISS automated-use and storage rights remain unresolved",
        ])
    return {
        "candidate_id": f"{near_secid}/{far_secid}",
        "near_secid": near_secid,
        "far_secid": far_secid,
        "status": "not_admitted" if reasons else "admitted",
        "reasons": reasons,
    }


def normalize_iss_intake(payload: dict[str, Any], *, url: str, received_at: str, raw_sha256: str, near_secid: str = DEFAULT_NEAR, far_secid: str = DEFAULT_FAR) -> dict[str, Any]:
    normalized = normalize_iss_payload(payload, url=url, received_at=received_at, raw_sha256=raw_sha256)
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
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "moex-spread-lab/0.1 W1 read-only probe"},
        method="GET",
    )
    received_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read()
            status = getattr(response, "status", 200)
            content_type = response.headers.get("Content-Type", "")
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise IssIntakeError(f"ISS request failed: {exc}") from exc
    if status != 200:
        raise IssIntakeError(f"ISS request returned HTTP {status}")
    if b"<html" in raw[:1024].lower() or "json" not in content_type.lower():
        raise IssIntakeError("ISS response is not JSON; possible access/error page")
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IssIntakeError("ISS response is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise IssIntakeError("ISS JSON root is not an object")
    return normalize_iss_intake(
        payload,
        url=url,
        received_at=received_at,
        raw_sha256=raw_sha256,
        near_secid=near_secid,
        far_secid=far_secid,
    )
