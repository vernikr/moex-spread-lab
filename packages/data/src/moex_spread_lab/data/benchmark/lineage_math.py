"""Return-linked LQDT benchmark stitching, rate compounding, and split adjustment."""

from __future__ import annotations

import itertools
from datetime import date
from typing import Any

from moex_spread_lab.core.w0 import W0Error

BENCHMARK_SCHEMA = "lqdt-lineage.v1"
MOEX_BASE = "https://iss.moex.com"
CHAIN = ("CBR_RUONIA", "FXMM", "RUSFAR", "VTBM", "LQDT")
OBSERVED = {"VTBM", "LQDT"}
HISTORY_PAGE_SIZE = 100
RUSFAR_PAGE_SIZE = 500


class BenchmarkError(W0Error):
    """Raised when a benchmark source or lineage cannot be normalized."""


def rate_rows_to_price(rows: list[dict[str, Any]], ticker: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    price = 1000.0
    previous: date | None = None
    for row in sorted(rows, key=lambda item: item["date"]):
        current = date.fromisoformat(row["date"])
        days = max((current - previous).days if previous else 1, 1)
        price *= 1 + float(row["rate"]) / 100 * days / 365
        result.append(
            {
                "date": row["date"],
                "price": price,
                "source_ticker": ticker,
                "source_kind": "synthetic_proxy",
                "price_role": "rate_compounded_proxy",
                "rate": row["rate"],
            }
        )
        previous = current
    return result


def adjust_vtbm_split(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda item: item["date"])
    adjusted = [dict(row) for row in ordered]
    factor = 1.0
    for index in range(len(adjusted) - 1, -1, -1):
        adjusted[index]["price"] *= factor
        if index:
            previous = float(ordered[index - 1]["price"])
            current = float(ordered[index]["price"])
            if previous > 0 and current > 0:
                ratio = current / previous
                if ratio < 0.2 or ratio > 5:
                    factor *= ratio
        adjusted[index]["split_adjustment"] = factor
    return adjusted


def _collect_daily_returns(
    sources: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    returns_by_date: dict[str, dict[str, Any]] = {}
    for ticker in CHAIN:
        rows = sources.get(ticker, [])
        for previous, current in itertools.pairwise(rows):
            if float(previous["price"]) <= 0:
                continue
            ret = float(current["price"]) / float(previous["price"]) - 1
            if abs(ret) >= 0.2:
                continue
            existing = returns_by_date.get(current["date"])
            if existing is None or CHAIN.index(ticker) > CHAIN.index(existing["source_ticker"]):
                returns_by_date[current["date"]] = {"return": ret, "source_ticker": ticker}
    return returns_by_date


def _best_available_by_date(sources: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    available_by_date: dict[str, str] = {}
    for ticker, rows in sources.items():
        for row in rows:
            previous = available_by_date.get(row["date"])
            if previous is None or CHAIN.index(ticker) > CHAIN.index(previous):
                available_by_date[row["date"]] = ticker
    return available_by_date


def _source_kind_for_ticker(source_ticker: str) -> str:
    if source_ticker in OBSERVED:
        return "observed"
    if source_ticker in CHAIN:
        return "synthetic_proxy"
    return "unknown"


def _stitch_backward_prices(
    *,
    all_dates: list[str],
    latest_row: dict[str, Any],
    returns_by_date: dict[str, dict[str, Any]],
    available_by_date: dict[str, str],
) -> list[dict[str, Any]]:
    current_price = float(latest_row["price"])
    latest_ticker = str(latest_row["source_ticker"])
    prices: list[dict[str, Any]] = [
        {
            "date": latest_row["date"],
            "price": current_price,
            "source_ticker": latest_ticker,
            "source_kind": "observed" if latest_ticker in OBSERVED else "synthetic_proxy",
        }
    ]
    for current_date in reversed(all_dates[:-1]):
        newer_date = str(prices[-1]["date"])
        ret_info = returns_by_date.get(newer_date)
        if ret_info is not None:
            current_price /= 1 + float(ret_info["return"])
            source_ticker = str(ret_info["source_ticker"])
        else:
            source_ticker = available_by_date.get(current_date, "holiday_or_missing_return")
        prices.append(
            {
                "date": current_date,
                "price": current_price,
                "source_ticker": source_ticker,
                "source_kind": _source_kind_for_ticker(source_ticker),
            }
        )
    prices.sort(key=lambda row: str(row["date"]))
    return prices


def build_lineage(raw_sources: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    sources = {
        ticker: sorted(rows, key=lambda row: row["date"])
        for ticker, rows in raw_sources.items()
        if rows
    }
    if "VTBM" in sources:
        sources["VTBM"] = adjust_vtbm_split(sources["VTBM"])
    all_dates = sorted({str(row["date"]) for rows in sources.values() for row in rows})
    if not all_dates:
        raise BenchmarkError("benchmark lineage has no rows")
    available_rows = [row for rows in sources.values() for row in rows]
    latest_row = max(
        available_rows,
        key=lambda row: (
            row["date"],
            CHAIN.index(row["source_ticker"]) if row["source_ticker"] in CHAIN else -1,
        ),
    )
    prices = _stitch_backward_prices(
        all_dates=all_dates,
        latest_row=latest_row,
        returns_by_date=_collect_daily_returns(sources),
        available_by_date=_best_available_by_date(sources),
    )
    observed_start = min(
        (str(row["date"]) for ticker in OBSERVED for row in sources.get(ticker, [])),
        default=None,
    )
    return {
        "schema_version": BENCHMARK_SCHEMA,
        "chain": list(CHAIN),
        "observed_ticker_set": sorted(OBSERVED),
        "observed_start": observed_start,
        "extended_start": prices[0]["date"],
        "extended_end": prices[-1]["date"],
        "rows": prices,
        "source_rows": {ticker: len(rows) for ticker, rows in sources.items()},
        "interpretation": {
            "observed": "VTBM/LQDT exchange price observations",
            "synthetic_proxy": "rate-compounded or return-linked extension; not an observed LQDT price",
            "split_handling": "VTBM internal 1000:1 split adjusted before return stitching",
        },
    }
