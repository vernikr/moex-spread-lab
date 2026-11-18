# Historical pair intake

Status: GLDRUBF quarterly history locally extended through 2026-10-27; local files are ignored market data and are not committed.

## Scope and sources

- Historical base: `GLDRUBF`, from 2023-07-11 (perpetual launch) through the latest common cached date.
- Quarterly references currently registered: `GLU3`, `GLZ3`, `GLH4`, `GLM4`, `GLU4`, `GLZ4`, `GLH5`, `GLM5`, `GLU5`, `GLZ5`, `GLH6`, `GLM6`, `GLU6`, `GLZ6`.
- Daily OHLCV is retrieved from MOEX ISS FORTS candles (`interval=24`); EOD settlement and `SWAPRATE` from MOEX ISS FORTS history. Data stays in ignored `data/w1/candles-screen`, `data/w1/candles-cache` and `data/carry-screen-history-cache`.
- Selection is asset-first (`GLDRUBF`) then a concrete pair (`GLDRUBF/GLZ6`, `GLDRUBF/GLU6`, …). The pair catalogue reports the actual common candle interval, observation count and liquidity/capital status. Existing `MIN_DAILY_VOLUME`/capital gates remain; a thin historical pair may be visible but unavailable to execute in `/proof`.

## Historical contract terms limitation

Expired quarterly SECIDs are not part of the current W1 snapshot. `data/w1/historical-contracts.json` adds terms for them so the offline proof can load those histories. Its lot, tick value/size and margin are copied from the live `GLZ6` contract snapshot; expiry is proxied by the final available candle date. These are explicitly marked fallback assumptions, not effective-dated contract evidence. Proof provenance hashes the historical terms file and each payload notes the fallback. Historical margin, exact last-trade dates and point-in-time specifications remain unaudited.

## Refresh procedure

Fetch daily candles with `moex_spread_lab.cli iss-candles` for `GLDRUBF` and the registered quarterly SECIDs, using `--interval 24 --from-date 2023-07-11 --till-date <latest ISS date>`, output under `data/w1/candles-screen` and page cache under `data/w1/candles-cache`. Fetch the same SECIDs with `carry.fetch_history(..., from_date='2023-07-11', till_date=<latest ISS date>, cache_dir=data/carry-screen-history-cache)`; the endpoint includes `SWAPRATE` where published. Do not commit these market-data files. Update `HISTORY_WINDOWS` and historical terms only after checking actual common dates and the relevant source records.

## Interpretation

These are historical candle/settlement proxies, not observed fills. `GLDRUBF` funding uses the exchange-published daily EOD `SWAPRATE`; dated-leg point value and historical margin/fees are not assumed to be historically exact. Causal strategy signals and execution timing remain separate from the observational contract history.