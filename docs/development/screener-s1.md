# Stage-9 S1 — Local research screener

Date: 2026-11-08

This records the first visible slice. The current table-first interface and common-parameter search supersede its single-pair layout; see [S2](screener-s2.md). Input preparation and the local launch command below remain applicable.

## Delivered

A dark-only, loopback-only research view over the existing offline engines. It lists the CNYRUBF/CRZ6 pair and the six previously screened perpetual/quarterly pairs when their ignored local inputs exist. Two compatible strategy choices are available where inputs permit: mean reversion with signed perpetual funding included when full local EOD `SWAPRATE` history is available, and short-perpetual/long-dated funding-only carry. Older H1 artifacts remain price-only historical controls and must not be read as full carry P&L. The user can edit the lookback and, for mean reversion, entry/exit deviations; choose low/base/stress illustrative costs; run and save configurations; and compare aligned wealth curves. Missing local inputs disable the affected choice with an intake reason.

Each result shows dated strategy wealth, matched observed LQDT wealth, excess and excess drawdown, return and capital-screen summaries, trade/open-position counts, source hashes and explicit proxy/cost/account caveats. Saved reports are ignored local artifacts under `data/analytics/screener-runs.jsonl`. No broker, VPS, Telegram or public endpoint is used.

## Run on the research workstation

From the repository root, after `uv sync`:

```bash
uv run python -m moex_spread_lab.cli screener --port 8765
```

Open `http://127.0.0.1:8765/` locally. Stop with Ctrl+C. The server binds only to `127.0.0.1`; it never fetches remote data. A comparable command-line run uses the same calculation path, for example:

```bash
uv run python -m moex_spread_lab.cli screener-run \
  --pair GLDRUBF/GLZ6 --strategy mean_reversion \
  --lookback 20 --entry-z 2 --exit-z 0.5 --cost stress \
  --output data/analytics/screener-check.json
```

The first launch needs the locally ignored `data/benchmark/lqdt-lineage.json`, `data/w1/current-intake.json` and daily pair candles in `data/w1/candles/` or `data/w1/candles-screen/`. The funding rule also requires cached FORTS EOD history in `data/carry-history-cache/` or `data/carry-screen-history-cache/`. Recreate those inputs using the documented commands in [the baseline](../../packages/strategies/docs/analytical-baseline.md), [pair screen](../../packages/strategies/docs/pair-screen.md) and [funding screen](../../packages/strategies/docs/funding-screen.md). Do not commit the downloaded datasets or saved runs.

## Interpretation and limits

- This is S1, not the S2 automatic optimizer or S4 shared-capital basket. Changing a parameter is a new exploratory experiment, not validated selection. There is no sealed out-of-sample verdict per editable run yet.
- The first pair selector includes `SBERF/SRZ6` (perpetual versus dated), **not** the separate near/far SBRF calendar strategy from `calendar-spread`. That reproduction is S3.
- The current 30,000 RUB and one contract per leg are fixed; a one-observation EOD proxy delay is used. Historical H1 results were price-only; the current app run includes signed MOEX ISS EOD `SWAPRATE` for perpetual positions when date coverage is complete, and flags missing/incomplete history rather than treating it as zero. The funding-only rule leaves an open position explicit. Neither represents historical fills.
- Pair margin and buffers use the current working VTB EBS/KПУР screen, not the client's account calculation. The LQDT path uses observed market prices where available, not the user's account statement. The illustrative cost branches are not verified historical charges. A positive result must not be promoted to an executable or funded pilot.
- Runs can be overlaid only when dates and benchmark values match. A differing period or benchmark produces a non-comparability message. Local input absence or a failed capital screen blocks a run rather than creating a synthetic chart.

## Acceptance

The new hermetic tests cover pair and parameter selection, deterministic replay, funding-rule reuse, input rejection and the local HTTP/save boundary. The workstation smoke test covered CNYRUBF/CRZ6 and GLDRUBF/GLZ6 under both available strategies, with the UI rendered in a browser. Next: [S2 bounded parameter search](../stages/08-planning.md), followed by the independent SBRF reproduction and basket ledger in their own slices.
