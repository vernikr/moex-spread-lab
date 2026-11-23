"""Shared HTTP JSON fetcher, ISS row normalizer, and W1 intake constants."""

from __future__ import annotations

import hashlib
import json
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from moex_spread_lab.core.w0 import W0Error

ISS_MODULE_SCHEMA = "iss-intake.v1"
DEFAULT_ISS_BASE_URL = "https://iss.moex.com"
DEFAULT_BOARD = "RFUD"
DEFAULT_NEAR = "CNYRUBF"
DEFAULT_FAR = "CRZ6"
PROJECT_RIGHTS_STATUS = "accepted_project_assumption_personal_use"
HISTORICAL_SPECIFICATION_STATUS = "current_snapshot_fallback_assumed"
CANDLE_INTERVAL_LABELS: dict[int, str] = {
    1: "1m",
    4: "4h",
    7: "1w",
    10: "10m",
    60: "1h",
    24: "1d",
    31: "1mo",
}
CANDLE_PAGE_SIZE = 500
CANDLE_FIELDS = ("open", "close", "high", "low", "value", "volume", "begin", "end")

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


def _candleborders_url(base_url: str, board: str, secid: str) -> str:
    root = base_url.rstrip("/")
    return (
        f"{root}/iss/engines/futures/markets/forts/boards/{board}"
        f"/securities/{secid}/candleborders.json?iss.meta=off"
    )


def _candles_url(
    base_url: str,
    board: str,
    secid: str,
    interval: int,
    from_date: str,
    till_date: str,
    start: int,
) -> str:
    root = base_url.rstrip("/")
    query = urlencode(
        {
            "iss.meta": "off",
            "interval": interval,
            "from": from_date,
            "till": till_date,
            "start": start,
        }
    )
    return (
        f"{root}/iss/engines/futures/markets/forts/boards/{board}"
        f"/securities/{secid}/candles.json?{query}"
    )


def _rows(block: Any, name: str) -> list[dict[str, Any]]:
    if (
        not isinstance(block, dict)
        or not isinstance(block.get("columns"), list)
        or not isinstance(block.get("data"), list)
    ):
        raise IssIntakeError(f"ISS block is malformed: {name}")
    columns: list[str] = block["columns"]
    rows: list[dict[str, Any]] = []
    for index, values in enumerate(block["data"]):
        if not isinstance(values, list) or len(values) != len(columns):
            raise IssIntakeError(f"ISS row has invalid width: {name}[{index}]")
        rows.append(dict(zip(columns, values, strict=True)))
    return rows


# Public aliases preserve the historical private entry points.
rows = _rows
url = _url
candles_url = _candles_url
candleborders_url = _candleborders_url


def fetch_json_with_raw(
    url: str,
    *,
    timeout: float,
    opener: Callable[..., Any],
    probe: str = "coverage",
) -> tuple[dict[str, Any], str, str, bytes]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": f"moex-spread-lab/0.1 W1 {probe} probe",
        },
        method="GET",
    )
    received_at = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        with opener(request, timeout=timeout) as response:
            raw: bytes = response.read()
            status = getattr(response, "status", 200)
            content_type = response.headers.get("Content-Type", "")
    except OSError as exc:
        raise IssIntakeError(f"ISS request failed: {exc}") from exc
    if status != 200:
        raise IssIntakeError(f"ISS request returned HTTP {status}")
    if b"<html" in raw[:1024].lower() or "json" not in content_type.lower():
        raise IssIntakeError("ISS response is not JSON; possible access/error page")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IssIntakeError("ISS response is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise IssIntakeError("ISS JSON root is not an object")
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    return payload, received_at, raw_sha256, raw


def _fetch_json(
    url: str,
    *,
    timeout: float,
    opener: Callable[..., Any],
    probe: str = "coverage",
    return_raw: bool = False,
) -> tuple[Any, ...]:
    payload, received_at, raw_sha256, raw = fetch_json_with_raw(
        url, timeout=timeout, opener=opener, probe=probe
    )
    if return_raw:
        return payload, received_at, raw_sha256, raw
    return payload, received_at, raw_sha256


# Public alias preserves the historical private entry point.
fetch_json = _fetch_json
