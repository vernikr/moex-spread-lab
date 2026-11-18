"""Explicit, networked FORTS recheck. No replay, HTTP route or optimizer calls this command."""

import argparse
import json
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from .carry import fetch_history
from .proof_funding import published_rates, read_supplement, supplement_path
from .screener_app import PERPETUAL_HISTORY_STARTS
from .w0 import W0Error


def recheck(root, secid, first, last, *, apply=False, fetch=fetch_history):
    if secid not in PERPETUAL_HISTORY_STARTS or date.fromisoformat(
        first
    ) > date.fromisoformat(last):
        raise W0Error(
            "Ожидается известный вечный фьючерс и упорядоченный интервал дат."
        )
    # No cache_dir: force a fresh ISS request, not a replay of the original pages.
    source = fetch(secid, first, last, cache_dir=None, allow_empty=True)
    rates = published_rates(secid, source["rows"])
    if not all(first <= d <= last for d in rates) or source["secid"] != secid:
        raise W0Error("Повторная выгрузка не соответствует запросу.")
    hashes = source["source"]["raw_response_sha256"]
    good = {d: v for d, v in rates.items() if v is not None}
    if apply:
        records = read_supplement(root, secid)
        # Replace this range only. A subsequent check can withdraw an earlier
        # supplemental observation; null is never written as a numerical rate.
        records = {d: r for d, r in records.items() if not first <= d <= last}
        for d, value in good.items():
            records[d] = {
                "rate": value,
                "from": first,
                "till": last,
                "page_hashes": hashes,
            }
        path = supplement_path(root, secid)
        path.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=path.parent) as stage:
            tmp = Path(stage) / path.name
            tmp.write_text(
                json.dumps(
                    {
                        "schema": "iss-swaprate-recheck.v1",
                        "secid": secid,
                        "records": records,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)
    return {
        "secid": secid,
        "from": first,
        "till": last,
        "eod_rows": len(rates),
        "numeric": len(good),
        "null": len(rates) - len(good),
        "first_numeric": min(good, default=None),
        "trade_dates": sorted(rates),
        "page_hashes": hashes,
        "applied": apply,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--secid", choices=sorted(PERPETUAL_HISTORY_STARTS), required=True
    )
    parser.add_argument("--from", dest="first", required=True)
    parser.add_argument("--till", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Save a separate official-rate supplement; never overwrite intake pages",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            recheck(args.root, args.secid, args.first, args.till, apply=args.apply),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
