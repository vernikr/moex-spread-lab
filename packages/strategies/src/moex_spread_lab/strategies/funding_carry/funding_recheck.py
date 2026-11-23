"""Explicit, networked FORTS recheck. No replay, HTTP route or optimizer calls this command."""

import argparse
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from moex_spread_lab.core.universe import PERPETUAL_HISTORY_STARTS
from moex_spread_lab.core.w0 import W0Error

from .carry import fetch_history
from .timeline import DSVD_NOTICE, published_rates, read_supplement, supplement_path


def format_evidence_notes(provenance: dict[str, object]) -> list[str]:
    notes: list[str] = []
    if days := provenance.get("funding_estimated_dates"):
        assert isinstance(days, list)
        days = cast(list[str], days)
        notes.append(
            f"Расчётных ставок: {len(days)}. Для серии из 1–3 пропущенных расчётных сессий на каждый день взято одно среднее двух соседних ставок. Это оценка платежа, не публикация MOEX; для сигнала она доступна только после публикации правой границы."
        )
    if days := provenance.get("funding_no_clearing_dates"):
        assert isinstance(days, list)
        days = cast(list[str], days)
        notes.append(
            "Без отдельного клиринга: "
            + ", ".join(days)
            + ". Дополнительная сессия входит в следующий торговый день. Отдельного платежа нет; расчётная цена не подставляется. MOEX: "
            + DSVD_NOTICE
        )
    if provenance.get("funding_recheck_pages"):
        notes.append(
            "Для пропусков использована также отдельная повторная выгрузка MOEX ISS; исходный кэш сохранён."
        )
    return notes


def recheck(
    root: Path | str,
    secid: str,
    first: str,
    last: str,
    *,
    apply: bool = False,
    fetch: Callable[..., dict[str, Any]] = fetch_history,
) -> dict[str, Any]:
    if secid not in PERPETUAL_HISTORY_STARTS or date.fromisoformat(first) > date.fromisoformat(
        last
    ):
        raise W0Error("Ожидается известный вечный фьючерс и упорядоченный интервал дат.")
    # No cache_dir: force a fresh ISS request, not a replay of the original pages.
    source: dict[str, Any] = fetch(secid, first, last, cache_dir=None, allow_empty=True)
    rates: dict[str, float | None] = published_rates(secid, source["rows"])
    if not all(first <= d <= last for d in rates) or source["secid"] != secid:
        raise W0Error("Повторная выгрузка не соответствует запросу.")
    hashes: list[str] = source["source"]["raw_response_sha256"]
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
    parser.add_argument("--secid", choices=sorted(PERPETUAL_HISTORY_STARTS), required=True)
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
