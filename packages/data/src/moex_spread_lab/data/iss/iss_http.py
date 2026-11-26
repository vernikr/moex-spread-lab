"""ISS HTTP, block decoding, and paginated futures history."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

from moex_spread_lab.core.hashed_json_cache import HashedJSONCache as _HashedJSONCache
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


def decode_rows(
    block: Any, name: str, *, error: type[W0Error] = IssIntakeError, strict: bool = True
) -> list[dict[str, Any]]:
    if (
        not isinstance(block, dict)
        or not isinstance(cast(dict[str, Any], block).get("columns"), list)
        or not isinstance(cast(dict[str, Any], block).get("data"), list)
    ):
        message = f"ISS block is malformed: {name}" if strict else f"malformed ISS block: {name}"
        raise error(message)
    columns = cast(list[str], block["columns"])
    result: list[dict[str, Any]] = []
    for index, values in enumerate(cast(list[Any], block["data"])):
        if not isinstance(values, list) or (
            strict and len(cast(list[Any], values)) != len(columns)
        ):
            if strict:
                raise error(f"ISS row has invalid width: {name}[{index}]")
            continue
        result.append(dict(zip(columns, cast(list[Any], values), strict=strict)))
    return result


def _rows(block: Any, name: str) -> list[dict[str, Any]]:
    return decode_rows(block, name)


def decode_json(
    raw: bytes, *, error: type[W0Error], invalid: str, not_object: str
) -> dict[str, Any]:
    try:
        value: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise error(invalid) from exc
    if not isinstance(value, dict):
        raise error(not_object)
    return cast(dict[str, Any], value)


rows = _rows
url = _url
candles_url = _candles_url


def read_response(
    request: str | urllib.request.Request, *, timeout: float, opener: Callable[..., Any]
) -> tuple[bytes, int, str]:
    with opener(request, timeout=timeout) as response:
        raw: bytes = response.read()
        status = getattr(response, "status", 200)
        headers = getattr(response, "headers", {})
        return raw, status, headers.get("Content-Type", "")


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
        raw, status, content_type = read_response(request, timeout=timeout, opener=opener)
    except OSError as exc:
        raise IssIntakeError(f"ISS request failed: {exc}") from exc
    if status != 200:
        raise IssIntakeError(f"ISS request returned HTTP {status}")
    if b"<html" in raw[:1024].lower() or "json" not in content_type.lower():
        raise IssIntakeError("ISS response is not JSON; possible access/error page")
    payload = decode_json(
        raw,
        error=IssIntakeError,
        invalid="ISS response is not valid UTF-8 JSON",
        not_object="ISS JSON root is not an object",
    )
    return payload, received_at, hashlib.sha256(raw).hexdigest(), raw


def _fetch_json(
    url: str,
    *,
    timeout: float,
    opener: Callable[..., Any],
    probe: str = "coverage",
    return_raw: bool = False,
) -> tuple[Any, ...]:
    result = fetch_json_with_raw(url, timeout=timeout, opener=opener, probe=probe)
    return result if return_raw else result[:3]


fetch_json = _fetch_json


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


HISTORY_JSON_CACHE: _HashedJSONCache[tuple[str, int, int], tuple[dict[str, Any], str]] = (
    _HashedJSONCache(4096)
)


@dataclass(frozen=True)
class _HistoryFetchConfig:
    secid: str
    from_date: str
    till_date: str
    cache_dir: Path | None
    delay_seconds: float
    timeout: float
    opener: Callable[..., Any]
    fields: tuple[str, ...]
    allow_empty: bool
    error: type[W0Error]


def fetch_history_json(
    url: str,
    *,
    timeout: float = 30.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
    error: type[W0Error] = IssHistoryError,
) -> tuple[dict[str, Any], str]:
    try:
        raw, _, _ = read_response(url, timeout=timeout, opener=opener)
    except OSError as exc:
        raise error(f"history request failed: {url}: {exc}") from exc
    value = decode_json(
        raw,
        error=error,
        invalid=f"history response is not JSON: {url}",
        not_object=f"history response is not an object: {url}",
    )
    return value, hashlib.sha256(raw).hexdigest()


def history_rows(
    block: Any, name: str, *, error: type[W0Error] = IssHistoryError
) -> list[dict[str, Any]]:
    return decode_rows(block, name, error=error, strict=False)


def history_url(
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


def _try_load_cached_history_page(
    cache_path: Path | None, url: str
) -> tuple[dict[str, Any], str] | None:
    if cache_path is None:
        return None
    try:
        resolved = cache_path.resolve()
        stat = resolved.stat()
        cache_key = (str(resolved), stat.st_mtime_ns, stat.st_size)
        cached = HISTORY_JSON_CACHE.get(cache_key)
        if cached is None:
            cached_raw: object = json.loads(resolved.read_text(encoding="utf-8"))
            if isinstance(cached_raw, dict):
                cached = (cast(dict[str, Any], cached_raw), "")
                HISTORY_JSON_CACHE.put(cache_key, cached)
        if cached is not None and cached[0].get("url") == url:
            payload: object = cached[0].get("payload")
            raw_hash: object = cached[0].get("raw_response_sha256")
            if isinstance(payload, dict):
                return cast(dict[str, Any], payload), str(raw_hash or "")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return None


def _load_or_fetch_history_page(cfg: _HistoryFetchConfig, start: int) -> tuple[dict[str, Any], str]:
    url = history_url(cfg.secid, cfg.from_date, cfg.till_date, start, cfg.fields)
    cache_path = (
        cfg.cache_dir / f"{cfg.secid}-{cfg.from_date}-{cfg.till_date}-{start}.json"
        if cfg.cache_dir
        else None
    )
    cached = _try_load_cached_history_page(cache_path, url)
    if cached is not None:
        return cached
    if cfg.delay_seconds:
        time.sleep(cfg.delay_seconds)
    payload, raw_hash = fetch_history_json(
        url, timeout=cfg.timeout, opener=cfg.opener, error=cfg.error
    )
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {"url": url, "payload": payload, "raw_response_sha256": raw_hash},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return payload, raw_hash


def _parse_history_total(payload: dict[str, Any], error: type[W0Error]) -> int | None:
    if not payload.get("history.cursor"):
        return None
    cursor_rows = history_rows(payload.get("history.cursor"), "history.cursor", error=error)
    return (
        int(cursor_rows[0]["TOTAL"])
        if cursor_rows and cursor_rows[0].get("TOTAL") is not None
        else None
    )


def _paginate_history_pages(
    cfg: _HistoryFetchConfig,
) -> tuple[list[dict[str, Any]], list[str], int]:
    all_rows: list[dict[str, Any]] = []
    hashes: list[str] = []
    page = 0
    total: int | None = None
    while total is None or len(all_rows) < total:
        payload, raw_hash = _load_or_fetch_history_page(cfg, page * HISTORY_PAGE_SIZE)
        block_rows = history_rows(payload.get("history"), "history", error=cfg.error)
        page_total = _parse_history_total(payload, cfg.error)
        if page_total is not None:
            total = page_total
        if not block_rows:
            if cfg.allow_empty and page == 0:
                hashes.append(raw_hash)
            break
        all_rows.extend(block_rows)
        hashes.append(raw_hash)
        page += 1
        if len(block_rows) < HISTORY_PAGE_SIZE:
            break
    if total is not None and len(all_rows) < total:
        raise cfg.error(
            f"Неполная история ISS {cfg.secid}: получено строк {len(all_rows)} из {total}."
        )
    return all_rows, hashes, page


def fetch_history(  # noqa: PLR0913 - explicit keyword arguments preserve the public API
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
    cfg = _HistoryFetchConfig(
        secid,
        from_date,
        till_date,
        cache_dir,
        delay_seconds,
        timeout,
        opener,
        fields,
        allow_empty,
        error,
    )
    all_rows, hashes, page = _paginate_history_pages(cfg)
    deduped = {str(row["TRADEDATE"]): row for row in all_rows if row.get("TRADEDATE")}
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
