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
- The unified UI groups by perpetual family, then by its main instrument, then shows the specific quarterly maturity. Missing inputs or unresolved applicable funding are hard exclusions with reasons. Strict screens require the every-day volume minimum; execution proof has its own explicit volume and capital-stress permissions. This is a research inventory, not a waiver of liquidity, capital, or execution gates.
- EOD cache ranges start at the earliest observed daily candle for each perpetual and end at the latest queried session. Intake stores the original `SWAPRATE`, including nulls. The research overlay below never infers funding from candle prices.

## Historical contract terms limitation

Expired quarterly SECIDs are not in the current W1 intake snapshot. `data/w1/historical-contracts.json` adds terms for those futures so offline analysis can load their histories. Lot, tick size/value and initial margin are copied from the current quarterly snapshot for the same product family. The MOEX tariff group is carried over from the current family mapping and is also not independently effective-dated. Expiry is proxied by the final daily candle date. Records are marked `historical_evidence: false` and name their fallbacks. These are assumptions, not point-in-time exchange specifications, historical margin, tariff classification or verified last-trade dates. Proof provenance hashes the terms file and includes the limitation in each output. Do not present these analyses as a historically exact margin or broker reconciliation.

## Interpretation and refresh

These are daily candle/settlement proxies, not observed fills. Perpetual EOD `SWAPRATE` is the exchange-published daily funding input where present; it does not reconstruct intraday funding or funding times. Causal signals, next-open execution proxies, costs, daily volume screens and capital gates remain unchanged.

To refresh: query the listed perpetuals and candidate quarterlies with `moex_spread_lab.cli iss-candles --interval 24`, inspect common dates, then retrieve FORTS histories with `carry.fetch_history`. Do not commit market datasets or generated caches. Do not extend the static registry until both legs' actual overlapping dates and all fallback terms are recorded.

## Funding reconciliation (2026-11-18)

A fresh, paginated ISS check of all **108 perpetual–quarterly registry pairs** (118 unique candle instruments; respective `HISTORY_WINDOWS` through 2026-10-27) reproduced **38** failures under the previous positive-volume/date containment test. The revised **funding check alone** rejects **25**, restores **13**, and introduces **no new funding rejection**. No midpoint was needed for any restored pair. This is a coverage audit, **not** 459-window training/admission, an Optuna result, or proof that every newly covered pair passes other controls.

| Source case | Verification | Treatment |
|---|---|---|
| `USDRUBF` / `EURRUBF` / `CNYRUBF` | FORTS ISS `history` from 2022-04-26 through 2022-12-09: each has 158 dated rows, the first 153 with null `SWAPRATE` (through Dec 2), first numeric Dec 5. A sparsely traded quarterly can expose fewer of these dates. | 10 / 8 / 7 pair windows remain unresolved. [MOEX n52418](https://www.moex.com/n52418) changed the evening calculation **from swap points** on Dec 5: neither zero nor the later regime is a historical rate. [Spectra 6.18](https://fs.moex.com/f/16399/release-notes-6.pdf) identifies the historical `f07.csv:SWAP_RATE` / clearing feed; no dated production extract for these dates was verified. An FX TODTOM market quote alone is not the final per-contract clearing transfer. |
| `IMOEXF` / `GAZPF` / `GLDRUBF` / `SBERF` | Between 2025-08-14 and Aug 19 inclusive, each has FORTS EOD rows on 14, 15, 18, 19 only. [MOEX launch notice](https://www.moex.com/n92862) and [exchange session guide](https://www.moex.com/s3831) identify Aug 16–17 as extra sessions of the next trading day without separate clearing or funding. | 4 / 4 / 3 / 2 windows pass funding coverage. Apply this documented exception only to these instruments/dates; a weekday or weekend flag is not a clearing calendar. |

Repeat a live check without writing to the original cache:

```sh
uv run --locked python -m moex_spread_lab.funding_recheck --secid CNYRUBF --from 2022-04-26 --till 2022-12-09
uv run --locked python -m moex_spread_lab.funding_recheck --secid GAZPF --from 2025-08-14 --till 2025-08-19
```

The default only reports dates, row/null counts, first numeric date and response hashes. `--apply --root .` atomically records supplementary **published numbers only** under ignored `data/proof-funding-verified/`; the primary ISS pages always win when numeric. An empty response is not itself proof of no clearing. The UI does not fetch data, save computed rates, trim a window or remove checks on its own. See [valuation and cutoff rules](../../../docs/development/execution-proof.md#exchange-check-and-short-gaps-2026-11-18).
