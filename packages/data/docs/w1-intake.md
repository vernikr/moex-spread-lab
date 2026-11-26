# MOEX ISS catalog and price-role intake

`packages/data` owns a read-only MOEX ISS adapter for the futures board `RFUD` (`iss.moex.com`). It supplies the current instrument catalog and paginated candle history for offline research. Every output is written under git-ignored `data/`, is local-only under rights assumption `A-05` (`accepted_project_assumption_personal_use`), and must never be committed or redistributed.

## Catalog intake (`iss-intake`)

- Requests an explicit security and market-data field list from the board and preserves the request URL, UTC receipt time and raw-response SHA-256.
- Builds a stable instrument identity from board, SECID and expiry; keeps settlement, current last-trade, bid and ask roles separate from the unavailable analytical close and live-mark roles.
- Preserves malformed market rows with a `crossed_quote` or `missing_quote` flag instead of dropping them, and records a current-snapshot status: a catalog response is never historical evidence.
- Returns an explicit candidate admission decision. When dated historical contract terms are unavailable, current contract parameters are used as an explicitly labelled fallback (`conditionally_admitted_analytical_only`, per `A-05`).

## Candle conventions (`iss-candles`, `A-06`)

- ISS candles exist per SECID in 1m, 10m, 1h and 1d intervals (ISS codes 1, 10, 60, 24). Coverage is uneven and must be confirmed per instrument and period from the actual responses; it is never assumed.
- Download is paginated at 500 rows per page, resumable per SECID/interval/page through a cache and rate-limited (`--delay-seconds`). Normalized rows keep the source hash and timestamps and add `price_proxy=close`, `price_proxy_kind=candle_close` and the `A-06` marker.
- Under `A-06` the candle close is the project's working price proxy for medium-term research, used with explicit delay and cost sensitivity. It is not a bid/ask reconstruction or a claim of historical fills.

## Execution boundary (`A-08`)

MOEX ISS provides no timestamped historical bid/ask or order-book archive, and the project does not collect one. Historical executable quotes are a permanent boundary: execution is an interval/scenario model with explicit manual reaction delay and cost sensitivity, current delayed bid/ask stays outside the offline replay, and the recorded evidence status is `historical_bid_ask_unavailable`.

## Command surface

`iss-intake` and `iss-candles` are the active intake commands. The former discovery subcommands `iss-timeframe-inventory` and `iss-candle-pair-check` are retired and are being removed from the CLI (#186); do not build new workflows on them.
