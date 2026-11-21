"""MOEX ISS W1 catalog and current price-role intake.

The adapter is intentionally read-only. It stores provenance and normalizes
the current snapshot without treating it as point-in-time historical evidence.
"""

from __future__ import annotations

import hashlib
import json
import csv
import time
from urllib.parse import urlencode
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .w0 import W0Error, _HashedJSONCache, write_json


ISS_MODULE_SCHEMA = "iss-intake.v1"
DEFAULT_ISS_BASE_URL = "https://iss.moex.com"
DEFAULT_BOARD = "RFUD"
DEFAULT_NEAR = "CNYRUBF"
DEFAULT_FAR = "CRZ6"
PROJECT_RIGHTS_STATUS = "accepted_project_assumption_personal_use"
HISTORICAL_SPECIFICATION_STATUS = "current_snapshot_fallback_assumed"
CANDLE_INTERVAL_LABELS = {
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
    return f"{root}/iss/engines/futures/markets/forts/boards/{board}/securities/{secid}/candleborders.json?iss.meta=off"


def _candles_url(base_url: str, board: str, secid: str, interval: int, from_date: str, till_date: str, start: int) -> str:
    root = base_url.rstrip("/")
    query = urlencode({
        "iss.meta": "off",
        "interval": interval,
        "from": from_date,
        "till": till_date,
        "start": start,
    })
    return f"{root}/iss/engines/futures/markets/forts/boards/{board}/securities/{secid}/candles.json?{query}"


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


def normalize_candle_borders(payload: dict[str, Any], *, secid: str, url: str, received_at: str, raw_sha256: str) -> dict[str, Any]:
    rows = _rows(payload.get("borders"), "borders")
    intervals = []
    for row in rows:
        interval = row.get("interval")
        if not isinstance(interval, int) or interval <= 0:
            raise IssIntakeError(f"invalid candle interval for {secid}: {interval}")
        begin = row.get("begin")
        end = row.get("end")
        intervals.append({
            "interval": interval,
            "timeframe": CANDLE_INTERVAL_LABELS.get(interval, f"interval_{interval}"),
            "begin": begin,
            "end": end,
            "coverage_status": "borders_available",
        })
    intervals.sort(key=lambda item: item["interval"])
    return {
        "secid": secid,
        "source": {
            "url": url,
            "received_at": received_at,
            "raw_response_sha256": raw_sha256,
        },
        "available_intervals": intervals,
        "minimum_interval": intervals[0]["interval"] if intervals else None,
        "minimum_timeframe": intervals[0]["timeframe"] if intervals else None,
        "coverage_status": "available" if intervals else "no_candle_borders",
    }


def normalize_candles(payload: dict[str, Any], *, secid: str, interval: int, url: str, received_at: str, raw_sha256: str) -> list[dict[str, Any]]:
    rows = _rows(payload.get("candles"), "candles")
    columns = set(payload.get("candles", {}).get("columns", []))
    missing = set(CANDLE_FIELDS) - columns
    if missing:
        raise IssIntakeError(f"candle fields missing for {secid}: {sorted(missing)}")
    normalized = []
    for row in rows:
        begin = row.get("begin")
        end = row.get("end")
        if not isinstance(begin, str) or not isinstance(end, str):
            raise IssIntakeError(f"candle timestamps missing for {secid}")
        close = row.get("close")
        if close is None:
            raise IssIntakeError(f"candle close missing for {secid}")
        normalized.append({
            "secid": secid,
            "interval": interval,
            "timeframe": CANDLE_INTERVAL_LABELS.get(interval, f"interval_{interval}"),
            "begin": begin,
            "end": end,
            "open": row.get("open"),
            "close": close,
            "high": row.get("high"),
            "low": row.get("low"),
            "value": row.get("value"),
            "volume": row.get("volume"),
            "price_proxy": close,
            "price_proxy_kind": "candle_close",
            "price_proxy_assumption": "user_A-06_candle_is_usable_medium_term_price_proxy",
            "source": {
                "url": url,
                "received_at": received_at,
                "raw_response_sha256": raw_sha256,
            },
        })
    return normalized


def fetch_candle_series(
    *,
    secid: str,
    interval: int,
    from_date: str,
    till_date: str,
    base_url: str = DEFAULT_ISS_BASE_URL,
    board: str = DEFAULT_BOARD,
    timeout: float = 20.0,
    delay_seconds: float = 0.1,
    cache_dir: Path | None = None,
    max_pages: int | None = None,
    retries: int = 3,
    opener: Callable[..., Any] = urllib.request.urlopen,
    raw_sink: Callable[[str, bytes, str, str], None] | None = None,
) -> dict[str, Any]:
    if interval <= 0 or delay_seconds < 0 or retries < 0:
        raise IssIntakeError("invalid candle interval, delay or retries")
    series_cache = cache_dir / secid / str(interval) if cache_dir else None
    all_rows: list[dict[str, Any]] = []
    page = 0
    while True:
        start = page * CANDLE_PAGE_SIZE
        url = _candles_url(base_url, board, secid, interval, from_date, till_date, start)
        cache_path = series_cache / f"page-{page:06d}.json" if series_cache else None
        page_result: dict[str, Any] | None = None
        if cache_path and cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if cached.get("url") == url and isinstance(cached.get("rows"), list):
                    page_result = cached
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                page_result = None
        if page_result is None:
            if delay_seconds:
                time.sleep(delay_seconds)
            last_error: IssIntakeError | None = None
            for attempt in range(retries + 1):
                try:
                    payload, received_at, raw_sha256, raw = _fetch_json(
                        url, timeout=timeout, opener=opener, return_raw=True,
                    )
                    if raw_sink is not None:
                        raw_sink(url, raw, received_at, raw_sha256)
                    last_error = None
                    break
                except IssIntakeError as exc:
                    last_error = exc
                    if attempt < retries:
                        time.sleep(min(2.0, 0.25 * (attempt + 1)))
            if last_error is not None:
                raise last_error
            rows = normalize_candles(payload, secid=secid, interval=interval, url=url, received_at=received_at, raw_sha256=raw_sha256)
            page_result = {"url": url, "page": page, "start": start, "rows": rows}
            if cache_path:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(page_result, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        rows = page_result["rows"]
        if rows and all_rows and rows[0]["begin"] == all_rows[-1]["begin"]:
            raise IssIntakeError(f"candle pagination did not advance: {secid} interval={interval} start={start}")
        all_rows.extend(rows)
        page += 1
        if not rows or len(rows) < CANDLE_PAGE_SIZE or (max_pages is not None and page >= max_pages):
            break
    deduped = {row["begin"]: row for row in all_rows}
    rows = [deduped[key] for key in sorted(deduped)]
    return {
        "schema_version": "iss-candles.v1",
        "secid": secid,
        "board": board,
        "interval": interval,
        "timeframe": CANDLE_INTERVAL_LABELS.get(interval, f"interval_{interval}"),
        "from": from_date,
        "till": till_date,
        "page_count": page,
        "raw_row_count": len(all_rows),
        "row_count": len(rows),
        "duplicate_count": len(all_rows) - len(rows),
        "coverage_status": "available" if rows else "empty",
        "price_interpretation": "candle_price_proxy_for_medium_term_analysis",
        "rows": rows,
    }


def analyze_candle_pair(near_path: Path, far_path: Path) -> dict[str, Any]:
    try:
        near_series = json.loads(near_path.read_text(encoding="utf-8"))
        far_series = json.loads(far_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IssIntakeError(f"cannot read candle series: {exc}") from exc
    if near_series.get("schema_version") != "iss-candles.v1" or far_series.get("schema_version") != "iss-candles.v1":
        raise IssIntakeError("pair check requires iss-candles.v1 series")
    if near_series.get("interval") != far_series.get("interval"):
        raise IssIntakeError("pair check requires equal intervals")

    def quality(series: dict[str, Any]) -> dict[str, Any]:
        rows = series.get("rows", [])
        begins = [row.get("begin") for row in rows]
        valid_ohlc = 0
        proxy_markers = 0
        for row in rows:
            values = [row.get(field) for field in ("open", "close", "high", "low")]
            if all(isinstance(value, (int, float)) for value in values) and row["low"] <= min(row["open"], row["close"], row["high"]) and row["high"] >= max(row["open"], row["close"], row["low"]):
                valid_ohlc += 1
            if row.get("price_proxy_kind") == "candle_close":
                proxy_markers += 1
        return {
            "secid": series.get("secid"),
            "row_count": len(rows),
            "first_begin": begins[0] if begins else None,
            "last_begin": begins[-1] if begins else None,
            "duplicate_begin_count": len(begins) - len(set(begins)),
            "out_of_order_count": sum(1 for left, right in zip(begins, begins[1:]) if left >= right),
            "valid_ohlc_count": valid_ohlc,
            "invalid_ohlc_count": len(rows) - valid_ohlc,
            "price_proxy_marker_count": proxy_markers,
        }

    near_rows = near_series.get("rows", [])
    far_rows = far_series.get("rows", [])
    near_by_begin = {row["begin"]: row for row in near_rows}
    far_by_begin = {row["begin"]: row for row in far_rows}
    common = sorted(set(near_by_begin) & set(far_by_begin))
    return {
        "schema_version": "iss-candle-pair-quality.v1",
        "interval": near_series.get("interval"),
        "timeframe": near_series.get("timeframe"),
        "near": quality(near_series),
        "far": quality(far_series),
        "synchronization": {
            "common_timestamp_count": len(common),
            "near_only_count": len(set(near_by_begin) - set(far_by_begin)),
            "far_only_count": len(set(far_by_begin) - set(near_by_begin)),
            "first_common_begin": common[0] if common else None,
            "last_common_begin": common[-1] if common else None,
            "pair_analytical_status": "usable_common_proxy_rows" if common else "no_common_rows",
        },
        "price_interpretation": "candle_close_is_A-06_medium_term_price_proxy",
        "execution_evidence_status": "historical_bid_ask_unavailable",
    }


def _fetch_json(
    url: str,
    *,
    timeout: float,
    opener: Callable[..., Any],
    probe: str = "coverage",
    return_raw: bool = False,
) -> tuple[Any, ...]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": f"moex-spread-lab/0.1 W1 {probe} probe"},
        method="GET",
    )
    received_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read()
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
    if return_raw:
        return payload, received_at, raw_sha256, raw
    return payload, received_at, raw_sha256


def fetch_candle_coverage(
    instruments: list[dict[str, Any]],
    *,
    base_url: str = DEFAULT_ISS_BASE_URL,
    board: str = DEFAULT_BOARD,
    timeout: float = 20.0,
    workers: int = 2,
    delay_seconds: float = 0.1,
    cache_dir: Path | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    if workers < 1:
        raise IssIntakeError("coverage workers must be positive")
    if delay_seconds < 0:
        raise IssIntakeError("coverage delay must not be negative")

    def fetch_one(instrument: dict[str, Any]) -> dict[str, Any]:
        secid = instrument["secid"]
        url = _candleborders_url(base_url, board, secid)
        cache_path = cache_dir / f"{secid}.json" if cache_dir else None
        if cache_path and cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if cached.get("secid") == secid and cached.get("source", {}).get("url") == url:
                    for interval_row in cached.get("available_intervals", []):
                        interval_row["timeframe"] = CANDLE_INTERVAL_LABELS.get(
                            interval_row.get("interval"), f"interval_{interval_row.get('interval')}"
                        )
                    if cached.get("available_intervals"):
                        cached["minimum_timeframe"] = cached["available_intervals"][0]["timeframe"]
                    return {**instrument, "candle_coverage": cached}
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        if delay_seconds:
            time.sleep(delay_seconds)
        try:
            payload, received_at, raw_sha256 = _fetch_json(url, timeout=timeout, opener=opener)
            coverage = normalize_candle_borders(payload, secid=secid, url=url, received_at=received_at, raw_sha256=raw_sha256)
            if cache_path:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(coverage, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            return {**instrument, "candle_coverage": coverage}
        except IssIntakeError as exc:
            return {
                **instrument,
                "candle_coverage": {
                    "secid": secid,
                    "source": {"url": url},
                    "available_intervals": [],
                    "minimum_interval": None,
                    "minimum_timeframe": None,
                    "coverage_status": "request_failed",
                    "error": str(exc),
                },
            }

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(fetch_one, instrument) for instrument in instruments]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item["stable_id"])
    status_counts: dict[str, int] = {}
    timeframe_counts: dict[str, int] = {}
    for result in results:
        coverage = result["candle_coverage"]
        status = coverage["coverage_status"]
        status_counts[status] = status_counts.get(status, 0) + 1
        timeframe = coverage["minimum_timeframe"] or "unavailable"
        timeframe_counts[timeframe] = timeframe_counts.get(timeframe, 0) + 1
    return {
        "schema_version": "iss-timeframe-inventory.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "board": board,
        "source": {
            "provider": "moex_iss",
            "rights_status": PROJECT_RIGHTS_STATUS,
            "rights_basis": "user_decision_A-05",
            "coverage_endpoint": "candleborders",
            "network_mode": "read_only_probe",
            "price_interpretation": "candle_price_proxy_for_medium_term_analysis",
        },
        "instrument_count": len(results),
        "coverage_status_counts": status_counts,
        "minimum_timeframe_counts": timeframe_counts,
        "instruments": results,
    }


def write_timeframe_inventory(result: dict[str, Any], output: Path, csv_output: Path | None = None) -> None:
    write_json(output, result)
    if csv_output is None:
        return
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    fields = ["stable_id", "secid", "security", "asset_code", "expiry", "minimum_timeframe", "minimum_interval", "available_timeframes", "coverage_status"]
    with csv_output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for instrument in result["instruments"]:
            coverage = instrument["candle_coverage"]
            writer.writerow({
                "stable_id": instrument["stable_id"],
                "secid": instrument["secid"],
                "security": instrument.get("security"),
                "asset_code": instrument.get("asset_code"),
                "expiry": instrument.get("expiry"),
                "minimum_timeframe": coverage.get("minimum_timeframe"),
                "minimum_interval": coverage.get("minimum_interval"),
                "available_timeframes": ",".join(item["timeframe"] for item in coverage.get("available_intervals", [])),
                "coverage_status": coverage.get("coverage_status"),
            })


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
            "historical_specification_basis": HISTORICAL_SPECIFICATION_STATUS,
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
            "rights_status": PROJECT_RIGHTS_STATUS,
            "rights_basis": "user_decision_A-05",
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
    assumptions: list[str] = []
    if near is None:
        reasons.append(f"near instrument not found: {near_secid}")
    if far is None:
        reasons.append(f"far instrument not found: {far_secid}")
    if near is not None and far is not None:
        if near["asset_code"] not in ("CNYRUBTOM", "CNY") or far["asset_code"] != "CNY":
            reasons.append("asset-code relationship is not the configured CNY/RUB candidate")
        if near["source_vintage"] != "current_snapshot" or far["source_vintage"] != "current_snapshot":
            reasons.append("unexpected source-vintage state")
        assumptions.append("historical contract parameters use the current snapshot until a dated specification is available")
        reasons.append("historical executable bid/ask is unavailable; execution replay remains blocked")
    if near is not None and far is not None and any(item["quote_quality"] != "two_sided_quote" for item in (near, far)):
        reasons.append("one or both candidate instruments have invalid or missing current quotes")
    return {
        "candidate_id": f"{near_secid}/{far_secid}",
        "near_secid": near_secid,
        "far_secid": far_secid,
        "status": "conditionally_admitted_analytical_only" if near is not None and far is not None and not any("invalid or missing" in reason for reason in reasons) else "not_admitted",
        "reasons": reasons,
        "assumptions": assumptions,
        "execution_status": "blocked_historical_quote_evidence" if near is not None and far is not None else "not_evaluable",
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
    payload, received_at, raw_sha256 = _fetch_json(
        url, timeout=timeout, opener=opener, probe="read-only",
    )
    return normalize_iss_intake(
        payload,
        url=url,
        received_at=received_at,
        raw_sha256=raw_sha256,
        near_secid=near_secid,
        far_secid=far_secid,
    )


HISTORY_SCHEMA = "iss-history.v1"
HISTORY_PAGE_SIZE = 100
HISTORY_FIELDS = (
    "BOARDID",
    "TRADEDATE",
    "SECID",
    "CLOSE",
    "SETTLEPRICE",
    "SWAPRATE",
    "OPENPOSITION",
    "VALUE",
    "VOLUME",
    "WAPRICE",
    "NUMTRADES",
    "ASSETCODE",
)


class IssHistoryError(W0Error):
    """Raised when an ISS EOD history request or response fails validation."""


_HISTORY_JSON_CACHE = _HashedJSONCache(4096)


def _fetch_history_json(
    url: str,
    *,
    timeout: float = 30.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
    error: type[W0Error] = IssHistoryError,
) -> tuple[dict[str, Any], str]:
    try:
        with opener(url, timeout=timeout) as response:
            raw = response.read()
    except OSError as exc:
        raise error(f"history request failed: {url}: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise error(f"history response is not JSON: {url}") from exc
    if not isinstance(value, dict):
        raise error(f"history response is not an object: {url}")
    return value, hashlib.sha256(raw).hexdigest()


def _history_rows(
    block: Any, name: str, *, error: type[W0Error] = IssHistoryError
) -> list[dict[str, Any]]:
    if (
        not isinstance(block, dict)
        or not isinstance(block.get("columns"), list)
        or not isinstance(block.get("data"), list)
    ):
        raise error(f"malformed ISS block: {name}")
    return [
        dict(zip(block["columns"], values))
        for values in block["data"]
        if isinstance(values, list)
    ]


def _history_url(
    secid: str,
    from_date: str,
    till_date: str,
    start: int,
    fields: tuple[str, ...] = HISTORY_FIELDS,
) -> str:
    query = urlencode(
        {
            "iss.meta": "off",
            "from": from_date,
            "till": till_date,
            "start": start,
            "limit": HISTORY_PAGE_SIZE,
            "history.columns": ",".join(fields),
        }
    )
    return f"https://iss.moex.com/iss/history/engines/futures/markets/forts/securities/{secid}.json?{query}"


def fetch_history(
    secid: str,
    from_date: str,
    till_date: str,
    *,
    cache_dir: Path | None = None,
    delay_seconds: float = 0.1,
    timeout: float = 30.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
    fields: tuple[str, ...] = HISTORY_FIELDS,
    allow_empty: bool = False,
    error: type[W0Error] = IssHistoryError,
) -> dict[str, Any]:
    if not secid or delay_seconds < 0:
        raise error("invalid history arguments")
    all_rows: list[dict[str, Any]] = []
    hashes: list[str] = []
    page = 0
    total: int | None = None
    while total is None or len(all_rows) < total:
        start = page * HISTORY_PAGE_SIZE
        url = _history_url(secid, from_date, till_date, start, fields)
        cache_path = (
            cache_dir / f"{secid}-{from_date}-{till_date}-{start}.json"
            if cache_dir
            else None
        )
        payload = None
        raw_hash = None
        if cache_path:
            try:
                resolved = cache_path.resolve()
                stat = resolved.stat()
                cache_key = (str(resolved), stat.st_mtime_ns, stat.st_size)
                cached = _HISTORY_JSON_CACHE.get(cache_key)
                if cached is None:
                    cached_obj = json.loads(resolved.read_text(encoding="utf-8"))
                    if isinstance(cached_obj, dict):
                        _HISTORY_JSON_CACHE.put(cache_key, (cached_obj, ""))
                        cached = (cached_obj, "")
                if cached is not None and cached[0].get("url") == url:
                    payload = cached[0].get("payload")
                    raw_hash = cached[0].get("raw_response_sha256")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = None
        if payload is None:
            if delay_seconds:
                time.sleep(delay_seconds)
            payload, raw_hash = _fetch_history_json(
                url, timeout=timeout, opener=opener, error=error
            )
            if cache_path:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps(
                        {
                            "url": url,
                            "payload": payload,
                            "raw_response_sha256": raw_hash,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
        block_rows = _history_rows(payload.get("history"), "history", error=error)
        cursor_rows = (
            _history_rows(payload.get("history.cursor"), "history.cursor", error=error)
            if payload.get("history.cursor")
            else []
        )
        if cursor_rows and cursor_rows[0].get("TOTAL") is not None:
            total = int(cursor_rows[0]["TOTAL"])
        if not block_rows:
            if allow_empty and page == 0:
                hashes.append(raw_hash or "")
            break
        all_rows.extend(block_rows)
        hashes.append(raw_hash or "")
        page += 1
        if len(block_rows) < HISTORY_PAGE_SIZE:
            break
    if total is not None and len(all_rows) < total:
        raise error(
            f"Неполная история ISS {secid}: получено строк {len(all_rows)} из {total}."
        )
    deduped = {row.get("TRADEDATE"): row for row in all_rows if row.get("TRADEDATE")}
    rows = [deduped[key] for key in sorted(deduped)]
    if not rows and (not allow_empty or all_rows):
        raise error(f"empty EOD history for {secid}")
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
