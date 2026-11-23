"""Cached-only EOD history loading for the calendar universe (offline view contract).

`fetch_inputs` is the single place that decides whether EOD history may travel over the network. In
offline mode it intersects the requested range with the files already present in the cache directory,
so a synthetic fixture with four cached secids costs one directory scan instead of 1,050 ISS urls,
1,050 rate-limit sleeps and 1,050 HTTP requests.
"""

from __future__ import annotations

import itertools
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from moex_spread_lab.data.iss.catalog import IssHistoryError, fetch_history

from .contracts import ASSETS, FX_SOURCE, HISTORY_FIELDS, HISTORY_FROM, contract_chain, spread_id


def cached_secids(
    cache_dir: Path,
    secids: list[str] | tuple[str, ...],
    till_date: str,
) -> list[str]:
    """Existing cached secids for the requested history range, from one directory scan.

    Offline views must never build 1,050 ISS URLs or probe the filesystem per symbol when only a
    handful of contracts are cached; this keeps a synthetic-fixture catalog call in the sub-second
    range instead of paying a rate-limit sleep or an HTTP round-trip per missing symbol.
    """
    try:
        names = {entry.name for entry in os.scandir(cache_dir) if entry.name.endswith(".json")}
    except OSError:
        return []
    pattern = re.compile(rf"^(?P<secid>.+)-{HISTORY_FROM}-{re.escape(till_date)}-\d+\.json$")
    found = {m.group("secid") for name in names if (m := pattern.match(name))}
    return [secid for secid in secids if secid in found]


def fetch_inputs(
    cache_dir: Path,
    assets: tuple[str, ...],
    till_date: str,
    *,
    delay_seconds: float = 0.05,
    opener: Any = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """EOD history of every leg and consecutive native spread (plus FX sources), keyed by secid then date.

    With ``opener`` (offline mode) only secids already present in ``cache_dir`` are read: no URLs,
    no rate-limit sleep, no thread pool and no outbound sockets.
    """
    needed = set(assets) | {FX_SOURCE[ASSETS[a]["fx"]] for a in assets if ASSETS[a].get("fx")}
    extra: dict[str, Any] = {"opener": opener} if opener is not None else {}
    secids: list[str] = []
    for asset in sorted(needed):
        chain = contract_chain(asset)
        secids += [*chain, *(spread_id(n, f) for n, f in itertools.pairwise(chain))]
    if opener is not None:
        secids = cached_secids(cache_dir, secids, till_date)

    def load(secid: str) -> tuple[str, dict[str, dict[str, Any]] | None]:
        try:
            source = fetch_history(
                secid,
                HISTORY_FROM,
                till_date,
                cache_dir=cache_dir,
                delay_seconds=delay_seconds,
                fields=HISTORY_FIELDS,
                **extra,
            )
        except IssHistoryError:
            return secid, None
        return secid, {str(row["TRADEDATE"]): row for row in source["rows"]}

    if opener is not None:
        return {secid: rows for secid in secids if (rows := load(secid)[1])}
    with ThreadPoolExecutor(max_workers=6) as pool:
        return {secid: rows for secid, rows in pool.map(load, secids) if rows}


def cached_till_date(cache_dir: Path) -> str | None:
    found = {
        m.group(1)
        for path in cache_dir.glob("*.json")
        if (m := re.search(rf"-{HISTORY_FROM}-(\d{{4}}-\d{{2}}-\d{{2}})-\d+\.json$", path.name))
    }
    return max(found) if found else None


def _offline(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError("offline: calendar history is read from the local cache only")


# Shared calendar adapter name; retain the private alias for compatibility.
offline_loader = _offline
