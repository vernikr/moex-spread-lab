# Historical perpetual–quarterly pair intake

Scope: the registered universe of historical perpetual–quarterly pairs, its local data footprint and the standing funding-history rules used by offline screens and proof. All datasets are local-only under `A-05`, git-ignored and never committed.

## Pair universe and admission

- The registry pairs each perpetual family with the expired quarterly contracts of the same asset; the confirmed SECIDs and observation windows are the static catalogs in `moex_spread_lab.data.universe.pairs` (`PERPETUAL_HISTORY_STARTS`, `HISTORICAL_FAR_BY_NEAR`).
- A quarterly is admitted only where its daily candle series shares at least 20 common dates with the perpetual's daily series. Each pair's `from`, `to` and observation count come from those actual common candle dates, not from the broad EOD cache window.
- Securities with missing ISS candles or fewer than 20 common observations are omitted, never fabricated. BTCUSDF currently has no expired-quarter overlap and keeps `BTZ6` as its only listed far leg.
- The unified UI groups by family, then main instrument, then quarterly maturity. Missing inputs or unresolved applicable funding are hard exclusions with reasons; this is a research inventory, not a waiver of liquidity, capital or execution gates.

## Local data

- Screened daily candles live under ignored `data/w1/candles-screen`; FORTS EOD history pages are cached under `data/carry-screen-history-cache` (the base CNYRUBF/CRZ6 pair uses `data/carry-history-cache`).
- History pages with unknown request time, an open Moscow-date range or age of 24 hours are not accepted by offline screens and proofs. Existing legacy pages remain on disk but need online re-intake; a range ending today needs another intake after its Moscow date closes. For a registered pair, from the project root run the following explicitly, with network access and the project's verified TLS configuration:

  ```sh
  .venv/bin/python -c 'from pathlib import Path; from moex_spread_lab.data import universe; from moex_spread_lab.data.iss.catalog import fetch_history; from moex_spread_lab.strategies.pair_data import history_cache_dir; p="CNYRUBF/CRZ6"; a,b=universe.HISTORY_WINDOWS[p]; [fetch_history(s,a,b,cache_dir=history_cache_dir(Path.cwd(),p)) for s in p.split("/")]'
  ```

  Substitute another registered pair for `p`; this writes only its page cache. For calendar history use `calendar-study --root . --till-date YYYY-MM-DD --output ...` with the same end date as the existing calendar cache. These are explicit network operations, not automatic UI downloads. Existing materialized candle/benchmark files are not refreshed by either operation.
- Intake stores the exchange-published `SWAPRATE` exactly as received, including nulls, and never infers funding from candle prices.

## Historical contract terms

Expired quarterly SECIDs are not in the current ISS snapshot, so `data/w1/historical-contracts.json` supplies their offline terms: lot, tick size/value and initial margin copied from the current quarterly snapshot of the same family, the family's tariff group carried over, and expiry proxied by the final daily-candle date. Records are marked `historical_evidence: false` and name their fallbacks. These are labelled assumptions — not point-in-time exchange specifications, historical margin or verified last-trade dates — and the terms file is hashed into proof provenance.

## Funding reconciliation (2026-11-18)

These standing rules govern funding coverage for every pair window:

1. Published daily EOD `SWAPRATE` is the funding input where present. It does not reconstruct intraday funding or funding times, and an empty response is not proof of no clearing.
2. Pre-swap-points FX history: [MOEX n52418](https://www.moex.com/n52418) moved the evening calculation to swap points on 2022-12-05, so neither zero nor the later regime is a historical rate for earlier dates. [Spectra 6.18](https://fs.moex.com/f/16399/release-notes-6.pdf) identifies the historical `f07.csv:SWAP_RATE` clearing feed; a window without a verified dated production extract stays unresolved, and an FX TODTOM market quote is not the final per-contract clearing transfer.
3. Extra sessions: [MOEX n92862](https://www.moex.com/n92862) and the [exchange session guide](https://www.moex.com/s3831) mark 2025-08-16–17 as extra sessions of the next trading day without separate clearing or funding. Treat those sessions as non-clearing for the windows that include them; a weekday or weekend flag is not a clearing calendar.

The default reports dates, row/null counts, first numeric date and response hashes only. `--apply --root .` atomically records supplementary published numbers under ignored `data/proof-funding-verified/`; primary ISS pages always win when numeric. The UI never fetches data, saves computed rates or removes checks on its own. See the [valuation and cutoff rules](../../proof/docs/execution-proof.md#exchange-check-and-short-gaps-2026-11-18).
