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

Every inventory result records `price_interpretation=candle_price_proxy_for_medium_term_analysis`: candle values are the project's working price proxy for medium-term research, not a claim of historical bid/ask reconstruction.

## Boundary

This adapter does not collect history, reconstruct fills, infer a live mark, authorize a broker or admit a strategy. The next data step is a dated daily-candle intake for both legs with explicit coverage and a labelled current-parameter fallback where needed.
