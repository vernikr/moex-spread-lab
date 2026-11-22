"""CLI commands owned by the data package."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from moex_spread_lab.core.w0 import write_json
from moex_spread_lab.data.iss.catalog import (
    fetch_candle_coverage,
    fetch_iss_catalog,
    write_timeframe_inventory,
)

from .cli_parsers import (
    register_h1_parsers,
    register_iss_and_benchmark_parsers,
    register_lake_parsers,
    register_retention_parsers,
)
from .cli_storage_handlers import STORAGE_DISPATCH


def register(subparsers: Any) -> None:
    register_retention_parsers(subparsers, run)
    register_h1_parsers(subparsers, run)
    register_lake_parsers(subparsers, run)
    register_iss_and_benchmark_parsers(subparsers, run)


def _run_iss_intake(args: Any) -> int:
    result = fetch_iss_catalog(
        base_url=args.base_url,
        board=args.board,
        near_secid=args.near,
        far_secid=args.far,
    )
    write_json(args.output, result)
    print(
        json.dumps(
            {
                "admission_status": result["admission_status"],
                "candidate": result["candidate"]["candidate_id"],
                "instrument_count": result["instrument_count"],
                "raw_response_sha256": result["source"]["raw_response_sha256"],
                "rights_status": result["source"]["rights_status"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _run_iss_timeframe_inventory(args: Any) -> int:
    catalog = fetch_iss_catalog(base_url=args.base_url, board=args.board)
    inventory = fetch_candle_coverage(
        catalog["instruments"],
        base_url=args.base_url,
        board=args.board,
        timeout=args.timeout,
        workers=args.workers,
        delay_seconds=args.delay_seconds,
        cache_dir=args.cache_dir,
    )
    write_timeframe_inventory(inventory, args.output, args.csv_output)
    print(
        json.dumps(
            {
                "instrument_count": inventory["instrument_count"],
                "coverage_status_counts": inventory["coverage_status_counts"],
                "minimum_timeframe_counts": inventory["minimum_timeframe_counts"],
                "output": str(args.output),
                "csv_output": str(args.csv_output) if args.csv_output else None,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _run_iss_candles(args: Any) -> int:
    from moex_spread_lab.data.iss.catalog import fetch_candle_series

    results: list[dict[str, Any]] = []
    for secid in args.secid:
        for interval in args.interval:
            result = fetch_candle_series(
                secid=secid,
                interval=interval,
                from_date=args.from_date,
                till_date=args.till_date,
                base_url=args.base_url,
                board=args.board,
                timeout=args.timeout,
                delay_seconds=args.delay_seconds,
                cache_dir=args.cache_dir,
                max_pages=args.max_pages,
                retries=args.retries,
            )
            output = args.output_dir / f"{secid}_{result['timeframe']}.json"
            write_json(output, result)
            results.append(
                {
                    "secid": secid,
                    "timeframe": result["timeframe"],
                    "row_count": result["row_count"],
                    "page_count": result["page_count"],
                    "duplicate_count": result["duplicate_count"],
                    "output": str(output),
                }
            )
    summary = args.output_dir / "summary.json"
    write_json(
        summary,
        {
            "schema_version": "iss-candle-intake-summary.v1",
            "from": args.from_date,
            "till": args.till_date,
            "price_interpretation": "candle_price_proxy_for_medium_term_analysis",
            "series": results,
        },
    )
    print(
        json.dumps({"series": results, "summary": str(summary)}, ensure_ascii=False, sort_keys=True)
    )
    return 0


def _run_iss_candle_pair_check(args: Any) -> int:
    from moex_spread_lab.data.iss.catalog import analyze_candle_pair

    result = analyze_candle_pair(args.near, args.far)
    write_json(args.output, result)
    print(
        json.dumps(
            {
                "timeframe": result["timeframe"],
                "common_timestamp_count": result["synchronization"]["common_timestamp_count"],
                "near_only_count": result["synchronization"]["near_only_count"],
                "far_only_count": result["synchronization"]["far_only_count"],
                "status": result["synchronization"]["pair_analytical_status"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _load_or_fetch_benchmark_source(
    cache_dir: Path,
    ticker: str,
    from_date: str,
    till_date: str,
    delay_seconds: float,
    fetch: Callable[..., dict[str, Any]],
    *,
    valid: Callable[[dict[str, Any]], bool] = lambda _: True,
) -> dict[str, Any]:
    path = cache_dir / f"{ticker}.json"
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        cached = None
    if (
        isinstance(cached, dict)
        and cached.get("from") == from_date
        and cached.get("till") == till_date
        and valid(cached)
    ):
        return cached
    source = fetch(from_date, till_date, delay_seconds=delay_seconds)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(source, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return source


def _format_benchmark_source_report(source: dict[str, Any], **details: Any) -> dict[str, Any]:
    rows = source["rows"]
    boards = Counter(row.get("board") or "not_applicable" for row in rows)
    return {
        "source": source["source"],
        "row_count": len(rows),
        "raw_response_hashes": source["raw_response_hashes"],
        "first_date": rows[0]["date"] if rows else None,
        "last_date": rows[-1]["date"] if rows else None,
        "board_row_counts": dict(boards),
        **details,
    }


def _collect_benchmark_sources(
    args: Any,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    from moex_spread_lab.data.benchmark.lineage import (
        fetch_cbr_ruonia_rates,
        fetch_moex_history,
        fetch_rusfar_rates,
        rate_rows_to_price,
    )

    raw_sources: dict[str, list[dict[str, Any]]] = {}
    source_reports: dict[str, dict[str, Any]] = {}
    for ticker in ("FXMM", "VTBM", "LQDT"):
        source = _load_or_fetch_benchmark_source(
            args.cache_dir,
            ticker,
            args.from_date,
            args.till_date,
            args.delay_seconds,
            partial(fetch_moex_history, ticker),
        )
        raw_sources[ticker] = source["rows"]
        source_reports[ticker] = _format_benchmark_source_report(
            source, board=source.get("board"), page_count=source["page_count"]
        )
    rusfar = _load_or_fetch_benchmark_source(
        args.cache_dir,
        "RUSFAR",
        args.from_date,
        args.till_date,
        args.delay_seconds,
        fetch_rusfar_rates,
    )
    raw_sources["RUSFAR"] = rate_rows_to_price(rusfar["rows"], "RUSFAR")
    source_reports["RUSFAR"] = _format_benchmark_source_report(
        rusfar, page_count=rusfar["page_count"]
    )
    ruonia = _load_or_fetch_benchmark_source(
        args.cache_dir,
        "CBR_RUONIA",
        args.from_date,
        args.till_date,
        args.delay_seconds,
        fetch_cbr_ruonia_rates,
        valid=lambda value: value.get("status") == "available" and "methods" in value,
    )
    if ruonia["rows"]:
        raw_sources["CBR_RUONIA"] = rate_rows_to_price(ruonia["rows"], "CBR_RUONIA")
    source_reports["CBR_RUONIA"] = _format_benchmark_source_report(
        ruonia,
        request_count=ruonia["request_count"],
        methods=ruonia.get("methods", []),
        status=ruonia["status"],
    )
    return raw_sources, source_reports


def _run_benchmark_lineage(args: Any) -> int:
    from moex_spread_lab.data.benchmark.lineage import build_lineage

    raw_sources, source_reports = _collect_benchmark_sources(args)
    missing_sources = [
        ticker
        for ticker in ("CBR_RUONIA", "RUSFAR", "FXMM", "VTBM", "LQDT")
        if not raw_sources.get(ticker)
    ]
    result = build_lineage(raw_sources)
    result["source_reports"] = source_reports
    result["missing_sources"] = missing_sources
    result["release_state"] = (
        "observed_plus_labelled_proxy" if not missing_sources else "exploratory_proxy_only"
    )
    write_json(args.output, result)
    print(
        json.dumps(
            {
                "extended_start": result["extended_start"],
                "extended_end": result["extended_end"],
                "observed_start": result["observed_start"],
                "row_count": len(result["rows"]),
                "source_rows": result["source_rows"],
                "missing_sources": result["missing_sources"],
                "release_state": result["release_state"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


_ISS_DISPATCH = {
    "iss-intake": _run_iss_intake,
    "iss-timeframe-inventory": _run_iss_timeframe_inventory,
    "iss-candles": _run_iss_candles,
    "iss-candle-pair-check": _run_iss_candle_pair_check,
    "benchmark-lineage": _run_benchmark_lineage,
}


def run(args: Any) -> int:
    handler = STORAGE_DISPATCH.get(args.command) or _ISS_DISPATCH.get(args.command)
    if handler is not None:
        return handler(args)
    return 2
