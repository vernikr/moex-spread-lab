"""MOEX ISS candleborders coverage normalization and inventory writer."""

from __future__ import annotations

import csv
import json
import time
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from moex_spread_lab.core.w0 import write_json

from .iss_http import (
    CANDLE_INTERVAL_LABELS,
    DEFAULT_BOARD,
    DEFAULT_ISS_BASE_URL,
    PROJECT_RIGHTS_STATUS,
    IssIntakeError,
    _candleborders_url,
    _rows,
    fetch_json_with_raw,
)

CSV_COLUMNS = [
    "stable_id",
    "secid",
    "security",
    "asset_code",
    "expiry",
    "minimum_timeframe",
    "minimum_interval",
    "available_timeframes",
    "coverage_status",
]


def normalize_candle_borders(
    payload: dict[str, Any],
    *,
    secid: str,
    url: str,
    received_at: str,
    raw_sha256: str,
) -> dict[str, Any]:
    rows = _rows(payload.get("borders"), "borders")
    intervals: list[dict[str, Any]] = []
    for row in rows:
        interval = row.get("interval")
        if not isinstance(interval, int) or interval <= 0:
            raise IssIntakeError(f"invalid candle interval for {secid}: {interval}")
        intervals.append(
            {
                "interval": interval,
                "timeframe": CANDLE_INTERVAL_LABELS.get(interval, f"interval_{interval}"),
                "begin": row.get("begin"),
                "end": row.get("end"),
                "coverage_status": "borders_available",
            }
        )
    intervals.sort(key=lambda item: int(item["interval"]))
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


def _try_load_cached_coverage(
    cache_path: Path | None, secid: str, url: str
) -> dict[str, Any] | None:
    if cache_path is None or not cache_path.exists():
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(cached, dict):
        return None
    if cached.get("secid") != secid or cached.get("source", {}).get("url") != url:
        return None
    for interval_row in cached.get("available_intervals", []):
        raw_interval = interval_row.get("interval")
        interval_row["timeframe"] = (
            CANDLE_INTERVAL_LABELS.get(raw_interval, f"interval_{raw_interval}")
            if isinstance(raw_interval, int)
            else f"interval_{raw_interval}"
        )
    if cached.get("available_intervals"):
        cached["minimum_timeframe"] = cached["available_intervals"][0]["timeframe"]
    return cached


def _fetch_one_instrument_coverage(
    instrument: dict[str, Any],
    *,
    base_url: str,
    board: str,
    timeout: float,
    delay_seconds: float,
    cache_dir: Path | None,
    opener: Callable[..., Any],
) -> dict[str, Any]:
    secid = str(instrument["secid"])
    url = _candleborders_url(base_url, board, secid)
    cache_path = cache_dir / f"{secid}.json" if cache_dir else None
    cached = _try_load_cached_coverage(cache_path, secid, url)
    if cached is not None:
        return {**instrument, "candle_coverage": cached}
    if delay_seconds:
        time.sleep(delay_seconds)
    try:
        payload, received_at, raw_sha256, _ = fetch_json_with_raw(
            url, timeout=timeout, opener=opener
        )
        coverage = normalize_candle_borders(
            payload, secid=secid, url=url, received_at=received_at, raw_sha256=raw_sha256
        )
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(coverage, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
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


def _summarize_coverage_counts(
    results: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int]]:
    status_counts: dict[str, int] = {}
    timeframe_counts: dict[str, int] = {}
    for result in results:
        coverage = result["candle_coverage"]
        status = str(coverage["coverage_status"])
        status_counts[status] = status_counts.get(status, 0) + 1
        timeframe = str(coverage["minimum_timeframe"] or "unavailable")
        timeframe_counts[timeframe] = timeframe_counts.get(timeframe, 0) + 1
    return status_counts, timeframe_counts


def fetch_candle_coverage(
    instruments: list[dict[str, Any]],
    **options: Any,
) -> dict[str, Any]:
    base_url: str = options.get("base_url", DEFAULT_ISS_BASE_URL)
    board: str = options.get("board", DEFAULT_BOARD)
    timeout: float = options.get("timeout", 20.0)
    workers: int = options.get("workers", 2)
    delay_seconds: float = options.get("delay_seconds", 0.1)
    cache_dir: Path | None = options.get("cache_dir")
    opener: Callable[..., Any] = options.get("opener", urllib.request.urlopen)
    unknown = set(options) - {
        "base_url",
        "board",
        "timeout",
        "workers",
        "delay_seconds",
        "cache_dir",
        "opener",
    }
    if unknown:
        raise TypeError(f"unexpected keyword argument(s): {sorted(unknown)}")
    if workers < 1:
        raise IssIntakeError("coverage workers must be positive")
    if delay_seconds < 0:
        raise IssIntakeError("coverage delay must not be negative")
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                _fetch_one_instrument_coverage,
                instrument,
                base_url=base_url,
                board=board,
                timeout=timeout,
                delay_seconds=delay_seconds,
                cache_dir=cache_dir,
                opener=opener,
            )
            for instrument in instruments
        ]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: str(item["stable_id"]))
    status_counts, timeframe_counts = _summarize_coverage_counts(results)
    return {
        "schema_version": "iss-timeframe-inventory.v1",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
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


def write_timeframe_inventory(
    result: dict[str, Any], output: Path, csv_output: Path | None = None
) -> None:
    write_json(output, result)
    if csv_output is None:
        return
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    with csv_output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for instrument in result["instruments"]:
            coverage = instrument["candle_coverage"]
            writer.writerow(
                {
                    "stable_id": instrument["stable_id"],
                    "secid": instrument["secid"],
                    "security": instrument.get("security"),
                    "asset_code": instrument.get("asset_code"),
                    "expiry": instrument.get("expiry"),
                    "minimum_timeframe": coverage.get("minimum_timeframe"),
                    "minimum_interval": coverage.get("minimum_interval"),
                    "available_timeframes": ",".join(
                        item["timeframe"] for item in coverage.get("available_intervals", [])
                    ),
                    "coverage_status": coverage.get("coverage_status"),
                }
            )
