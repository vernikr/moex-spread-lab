"""MOEX ISS end-of-day futures history pagination (`fetch_history`)."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

from moex_spread_lab.core.hashed_json_cache import HashedJSONCache as _HashedJSONCache
from moex_spread_lab.core.w0 import W0Error

from .iss_http import decode_json, decode_rows, read_response

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


_HISTORY_JSON_CACHE: _HashedJSONCache[tuple[str, int, int], tuple[dict[str, Any], str]] = (
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


def _fetch_history_json(
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


def _history_rows(
    block: Any, name: str, *, error: type[W0Error] = IssHistoryError
) -> list[dict[str, Any]]:
    return decode_rows(block, name, error=error, strict=False)


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


HISTORY_JSON_CACHE = _HISTORY_JSON_CACHE
fetch_history_json = _fetch_history_json
history_rows = _history_rows
history_url = _history_url


def _try_load_cached_history_page(
    cache_path: Path | None, url: str
) -> tuple[dict[str, Any], str] | None:
    if cache_path is None:
        return None
    try:
        resolved = cache_path.resolve()
        stat = resolved.stat()
        cache_key = (str(resolved), stat.st_mtime_ns, stat.st_size)
        cached = _HISTORY_JSON_CACHE.get(cache_key)
        if cached is None:
            cached_raw: object = json.loads(resolved.read_text(encoding="utf-8"))
            if isinstance(cached_raw, dict):
                cached_obj = cast(dict[str, Any], cached_raw)
                _HISTORY_JSON_CACHE.put(cache_key, (cached_obj, ""))
                cached = (cached_obj, "")
        if cached is not None and cached[0].get("url") == url:
            payload: object = cached[0].get("payload")
            raw_hash: object = cached[0].get("raw_response_sha256")
            if isinstance(payload, dict):
                return cast(dict[str, Any], payload), str(raw_hash or "")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return None


def _load_or_fetch_history_page(cfg: _HistoryFetchConfig, start: int) -> tuple[dict[str, Any], str]:
    url = _history_url(cfg.secid, cfg.from_date, cfg.till_date, start, cfg.fields)
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
    payload, raw_hash = _fetch_history_json(
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
    cursor_rows = _history_rows(payload.get("history.cursor"), "history.cursor", error=error)
    if cursor_rows and cursor_rows[0].get("TOTAL") is not None:
        return int(cursor_rows[0]["TOTAL"])
    return None


def _paginate_history_pages(
    cfg: _HistoryFetchConfig,
) -> tuple[list[dict[str, Any]], list[str], int]:
    all_rows: list[dict[str, Any]] = []
    hashes: list[str] = []
    page = 0
    total: int | None = None
    while total is None or len(all_rows) < total:
        payload, raw_hash = _load_or_fetch_history_page(cfg, page * HISTORY_PAGE_SIZE)
        block_rows = _history_rows(payload.get("history"), "history", error=cfg.error)
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
