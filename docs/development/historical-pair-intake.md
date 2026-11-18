# Historical perpetual–quarterly pair intake

Status: 2026-11-17 discovery extended the locally cached MOEX ISS candle set and FORTS EOD history for the registered perpetual families. Local market data is ignored and must not be committed. The catalogue contains the confirmed historical quarterly SECIDs; pair dates and common-observation counts are computed from actual cached daily candles.

## Discovery and coverage

Source: MOEX ISS FORTS daily candles (`interval=24`) and FORTS history (`SETTLEPRICE`, `SWAPRATE` where published), queried 2020-01-01 through 2026-10-27. A historical contract was admitted to the selector only where its daily candle series shares at least 20 dates with the perpetual's daily series. Each pair's displayed `from`, `to` and observation count come from the actual common candle dates, not the broad EOD cache window.

| Perpetual | First candle in current ISS query | Quarterlies with ≥20 common dates | Added to the existing pair |
|---|---:|---:|---:|
| GLDRUBF | 2023-07-11 | 14 total | 13 |
| CNYRUBF | 2022-04-26 | 17 | 16 |
| IMOEXF | 2023-11-14 | 13 | 12 |
| GAZPF | 2024-10-01 | 9 | 8 |
| SBERF | 2024-10-01 | 9 | 8 |
| RGBIF | 2025-12-23 | 4 | 3 |
| USDRUBF | 2022-04-26 | 19 | 18 |
| SLVRUBF | 2026-03-31 | 3 | 2 |
| EURRUBF | 2022-04-26 | 19 | 18 |
| BTCUSDF | 2026-09-22 | 1 | 0 |

This adds 85 historical pairs beyond the former GLDRUBF-only expansion. BTCUSDF has no expired-quarter overlap with the short available perpetual history, so its current BTZ6 reference remains the only listed choice. Securities with missing ISS candles or fewer than 20 common observations are omitted rather than fabricated.

## Local data and selector behavior

- Daily candles are copied from the discovery cache into ignored `data/w1/candles-screen`; EOD raw-response pages are cached under `data/carry-screen-history-cache` (plus the existing CNYRUBF current-pair cache).
- The unified UI groups by perpetual family, then by its main instrument, then shows the specific quarterly maturity. Historical choices without local candles, EOD settlements or complete near-leg `SWAPRATE` coverage for the common candle dates remain disabled; missing rates are never imputed as zero. Pairs failing the existing every-day minimum-volume rule are visible but disabled. This is a research inventory, not a waiver of liquidity, capital, or execution gates.
- EOD cache ranges start at the earliest observed daily candle for each perpetual and end at the latest queried session. `SWAPRATE` is included only where MOEX ISS published it; it is not inferred from candle prices.

## Historical contract terms limitation

Expired quarterly SECIDs are not in the current W1 intake snapshot. `data/w1/historical-contracts.json` adds terms for those futures so offline analysis can load their histories. Lot, tick size/value and initial margin are copied from the current quarterly snapshot for the same product family. The MOEX tariff group is carried over from the current family mapping and is also not independently effective-dated. Expiry is proxied by the final daily candle date. Records are marked `historical_evidence: false` and name their fallbacks. These are assumptions, not point-in-time exchange specifications, historical margin, tariff classification or verified last-trade dates. Proof provenance hashes the terms file and includes the limitation in each output. Do not present these analyses as a historically exact margin or broker reconciliation.

## Interpretation and refresh

These are daily candle/settlement proxies, not observed fills. Perpetual EOD `SWAPRATE` is the exchange-published daily funding input where present; it does not reconstruct intraday funding or funding times. Causal signals, next-open execution proxies, costs, daily volume screens and capital gates remain unchanged.

To refresh: query the listed perpetuals and candidate quarterlies with `moex_spread_lab.cli iss-candles --interval 24`, inspect common dates, then retrieve FORTS histories with `carry.fetch_history`. Do not commit market datasets or generated caches. Do not extend the static registry until both legs' actual overlapping dates and all fallback terms are recorded.
