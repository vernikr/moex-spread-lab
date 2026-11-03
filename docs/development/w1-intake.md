# W1 — MOEX ISS catalog and price-role intake

Date: 2026-10-27

## Delivered

`src/moex_spread_lab/iss.py` adds a read-only MOEX ISS adapter for the futures RFUD board. It:

- requests an explicit catalog and market-data field list;
- preserves the request URL, UTC receipt time and raw-response SHA-256;
- creates a stable identity from board, SECID and expiry;
- keeps settlement, current last trade, bid, ask and unavailable analytical/close/live-mark roles separate;
- records current-snapshot status instead of treating the response as historical evidence;
- preserves malformed market quality rows with a `crossed_quote` or `missing_quote` flag;
- returns an explicit candidate admission decision.

The project now uses the user-approved private-research assumption `A-05`: unresolved ISS-use terms do not block this local pipeline. The assumption is recorded in provenance and is not presented as an external legal conclusion.

## Local probe

Run from the repository root:

```bash
uv run python -m moex_spread_lab.cli iss-intake \
  --near CNYRUBF \
  --far CRZ6 \
  --output data/w1/current-intake.json
```

The output is intentionally under ignored `data/`. It contains current market observations and must not be committed or redistributed.

## Observed outcome

The read-only probe returned a current RFUD catalog and found both configured CNY/RUB instruments. Under the user's fallback rule, the candidate is **`conditionally_admitted_analytical_only`**:

1. current contract parameters may be used as a labelled fallback if dated historical terms are unavailable;
2. the response does not provide historical executable bid/ask, so execution replay remains blocked;
3. the data can support analytical candle research, but not a claim that historical orders could have been filled.

The full catalog is retained in the local report only. A crossed quote is preserved as a quality issue and does not invalidate unrelated instruments.

## Data-resolution boundary

The historical data question has three separate answers:

- **Daily candles:** the primary broad analytical layer for the first pair;
- **Intraday candles:** ISS can provide M1, M10 and H1-style candle intervals for some contracts and periods, with uneven coverage and pagination. Coverage must be checked per SECID; an intraday candle is still an OHLCV bar, not a quote history;
- **Historical executable quotes:** the current ISS intake does not provide a timestamped historical bid/ask or order-book archive. Current delayed bid/ask is a snapshot and cannot reconstruct past fills.

Therefore the next intake can use more detail than daily data for analytical and delay-sensitivity tests, while execution results remain interval/scenario-based until a quote or broker evidence source is collected.

## Instrument timeframe inventory

The repeatable inventory command checks `candleborders` for every instrument in the current RFUD catalog and writes one row per instrument:

```bash
uv run python -m moex_spread_lab.cli iss-timeframe-inventory \
  --output data/w1/instrument-timeframes.json \
  --csv-output data/w1/instrument-timeframes.csv \
  --workers 8 \
  --timeout 8 \
  --cache-dir data/w1/candleborders-cache
```

The JSON is the provenance-rich result; the CSV is the convenient filterable list. The cache makes the run resumable. Delete `data/w1/candleborders-cache/` before a deliberate fresh coverage scan.

The completed 2026-10-27 scan covered 506 instruments: 483 had `1m` as the smallest available candle interval and 23 had no candle borders. No request failures remained after retrying the initial transient failures. For `CNYRUBF` and `CRZ6`, the smallest available interval is `1m`; both also expose `10m`, `1h` and `1d` borders.

Every inventory result records `price_interpretation=candle_price_proxy_for_medium_term_analysis`. Under decision A-06, the candle close is the project's working price proxy for medium-term research, with delay and cost sensitivity. It is not a claim of historical bid/ask reconstruction.

## Historical candle intake

Paginated candle download for a selected instrument set is resumable by SECID, interval and page:

```bash
uv run python -m moex_spread_lab.cli iss-candles \
  --secid CNYRUBF --secid CRZ6 \
  --interval 24 --interval 60 --interval 10 --interval 1 \
  --from-date 2022-12-05 \
  --till-date 2026-10-27 \
  --output-dir data/w1/candles \
  --cache-dir data/w1/candles-cache \
  --delay-seconds 0.1
```

ISS pages are capped at 500 rows. Each normalized row preserves the source hash and timestamps and adds `price_proxy=close`, `price_proxy_kind=candle_close` and the A-06 assumption marker. The JSON outputs and cache remain under ignored `data/`.

## Completed pair intake

For the common CNYRUBF/CRZ6 research window, M1 was collected from 2025-06-16 through 2026-10-27 because CRZ6 has no earlier coverage. The downloaded series contain:

| Timeframe | CNYRUBF rows | CRZ6 rows | Common timestamps | OHLC errors |
|---|---:|---:|---:|---:|
| 1d | 992 | 344 | 344 | 0 / 0 |
| 1h | 15,844 | 4,412 | 4,408 | 0 / 0 |
| 10m | 88,358 | 19,090 | 19,082 | 0 / 0 |
| 1m, common window | 313,178 | 110,333 | 107,386 | 0 / 0 |

Pair-quality reports are generated locally as `data/w1/pair-quality-*.json`. Non-common timestamps are reported rather than forward-filled or discarded silently. The series are analytically usable under A-06; the execution evidence status remains `historical_bid_ask_unavailable`.

## Boundary

This adapter does not reconstruct fills, infer a live mark, authorize a broker or admit a strategy. Spread economics, matched LQDT analytical marking, H1/M10 sensitivity and M1 diagnostics are now implemented over the synchronized candle rows; the execution evidence status remains unchanged.
