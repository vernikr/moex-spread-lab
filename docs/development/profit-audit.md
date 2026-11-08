# Profit audit and S3 reference reproduction

Date: 2026-11-08. Research only: no broker, VPS, Telegram or trading. Prices are candle or settlement proxies (A-06/A-08).

## 1. Calculation and data check

Reproduce: `audit-pairs --output data/analytics/pair-audit.json` (`audit.py`).

| Check | Result |
|---|---|
| Independent re-implementation of the mean-reversion control from raw candles (signal on closes, fill at the next open, no engine code) | Gross P&L and trade count identical for 12/12 local pairs. No look-ahead. |
| Per-leg price units (`tick_value / tick_size`) | Consistent; spreads are RUB per pair. |
| Funding units (`SWAPRATE × lot`) | Ad-hoc check of cached history: short-perpetual funding is 13–15 % p.a. of notional for stock, index and gold perpetuals and about 12 % for CNYRUBF — consistent with the RUB rate, so unit and sign are right. |
| Candle close vs EOD `SETTLEPRICE` | Spread noise within ~10 % on four of five pairs (37 % apart on gold); mean-reversion gross P&L has the same sign and size on both sources. |
| Daily `open` | Inside the day's range on all rows; median gap from the previous close 4–75 bp. |
| LQDT benchmark | Observed layer about 18.5 % (2024), 20.7 % (2025), 15 % (2026 YTD) p.a. |

No arithmetic or alignment defect was found. Defects relative to the user's intent, now fixed:

1. **Execution delay.** Fills were one daily close after the signal close (about 24 hours). The documented reaction is 3 minutes to 1.5 hours, so the daily control now fills at the next open. Funding carry fills at the same-day settlement (its signal uses funding through the previous clearing); a same-day exit forgoes that day's funding.
2. **Accounting (A-11).** Idle cash earned nothing while the benchmark compounded LQDT on all capital. Free cash now sits in LQDT; margin cash earns nothing (LQDT is not usable as collateral, A-12). Contracts per leg is a parameter.
3. **Costs (A-12).** Flat illustrative RUB amounts and a per-leg-day "funding" charge are replaced by the published MOEX taker tariff (`costs.py`, by contract group), zero VTB broker fee to 2026-12-31, and slippage in ticks as the only free parameter. Group assignment for metals, RGBI and BTC contracts is an assumption.
4. **Volume.** Volume thresholds inside strategies are removed. A pair is either tradable on every day of its window (each leg ≥ 100 contracts per day, an assistant-selected threshold, adjustable in `MIN_DAILY_VOLUME`) or excluded.

## 2. Result under the corrected rules

One contract per leg, slippage 1 tick, 30,000 RUB allocation, default `20 / 2.0 / 0.5` control:

- **Eight of 12 pairs are excluded**: a leg traded as few as 1–75 contracts on some day (`SRZ6` 6, `MMZ6` 15, `GZZ6` 21, `EuZ6` 1, `CRZ6` 1, `S2Z6` 3, `GLH7` 4, `RBZ6` 75).
- `USDRUBF/SiZ6` passes volume but fails the working capital screen (pair margin about 83 % of the allocation plus buffers).
- `BTCUSDF/BTZ6` has 26 observations: annualized result withheld.
- Evaluated: `GLDRUBF/GLZ6` −6.5 pp/yr mean reversion (2 trades), −1.3 pp/yr funding carry (one open position); `CRZ6/CRH7` +0.3 pp/yr (4 trades, about +RUB 50 over 218 days). A 45-point parameter grid (lookback 10–40, entry 1.5–2.5σ, exit 0.25–1σ) gives gold positive in 1/45 settings (median −RUB 845) and `CRZ6/CRH7` positive in 22/45 (median ≈ 0, range −277…+128): no edge.
- Unfiltered, five of 12 pairs are positive at one tick (`SBERF/SRZ6` +1,297, `USDRUBF/SiZ6` +1,724, `GLZ6/GLH7` +102, `CRZ6/CRH7` +48, BTC +3 RUB), but the two large ones are a leg with 6 contracts on its thinnest day and a pair the capital screen blocks. The apparent edge sits where prints are stale.

Leverage multiplies P&L, costs and risk linearly: it cannot turn a negative per-contract excess positive. It is also capped by the working capital stress screen (3× margin shock plus variation-margin and uncertainty buffers, buffers scaled with size): the two evaluated pairs pass up to 2 contracts per leg and fail at 3, even though one contract uses only 7–8 % of the allocation as initial margin. Sizing is therefore limited by the stress buffers, not by the broker's margin.

## 3. S3 — SBRF reference reproduction

Reproduce: `s3-reference --reference-dir <calendar-spread checkout> --output data/analytics/s3-sbrf-reference.json` (`s3_reference.py`; reads the reference project's MIT-licensed processed data in place).

The reference is reproduced exactly (convergence 4 trades +RUB 1,947; z-score 41 trades +RUB 5,367, flat RUB 26 round trip). Net result with the published tariff and 1-tick slippage:

| Fill | Convergence (n=4) net / excess over LQDT on margin | Z-score net / excess |
|---|---:|---:|
| Reference (signal and fill on the same close) | +1,932 / −1,463 | 41 trades +5,288 / +3,991 |
| Next close | −408 / −3,764 | 43 trades −842 / −2,289 |
| Next open | −3,040 / −6,396 | 43 trades −2,163 / −3,610 |

- The z-score gain exists only when the signal and fill share one close and the rolling window contains the signal bar.
- The convergence gain is four hold-to-expiry trades; the first entry (`SRH5/SRM5`, spread 2,122) is a stale first-day print (the next day it is 600). Even as published it does not beat LQDT on the margin it ties up.
- All nine reference pairs fail the volume rule (their far legs traded 1–2 contracts on some day), so none is tradable under A-12 as defined; the strict whole-window rule may be too harsh for calendars whose far leg is liquid only near expiry, and relaxing it to a late-life window is a user decision.
- Ad-hoc (not versioned): holding liquid, no-dividend days to expiry, signed P&L grows with deviation (≥ 2 pp ≈ +440 RUB per pair), but that bucket is one episode (`SRH5/SRM5`, Dec 2024–Mar 2025, peak-rate regime). About five independent episodes exist in two years.

b02.6: the reference gain is **not accepted as evidence**; S3 is closed as a reproduction.

## 4. Where profit could come from

Structural carry is priced near the risk-free rate, so it ties with LQDT:

| Source | Evidence here | Excess over LQDT |
|---|---|---|
| Short perpetual + long dated | Funding received ≈ basis loss; gross carry −215…+84 RUB over 155 days (old accounting) | ≈ 0 |
| Long underlying + short perpetual | Funding 13–15 % p.a. of notional vs LQDT ≈ 14.5 % (ad-hoc) | ≈ +0.5…1 pp (stocks, gold), ≈ +3.5 pp (silver), before hedge costs and dividends |
| Long stock + short dated future | Implied rate ≈ RUONIA +0.7…1.3 pp on liquid contracts 2024–2026 (ad-hoc, approximate ex-dates) | ≈ +1 pp on notional ≈ RUB 300/yr per RUB 30,000; dividend tax and collateral rules unverified |
| Calendar convergence / z-score | Sections 2–3 | None demonstrated |
| Rate-path view (long far / short near in a cutting cycle) | The only profitable episodes | A directional bet, not arbitrage |

Next steps by evidence value:

1. **Longer liquid history.** Six pairs have one dated contract and 155 days; multi-year histories of expired contracts (ISS, when reachable) would give many independent episodes for the pairs that pass the volume rule.
2. **Decide the calendar volume window** (above) before reopening S3.
3. **S4 shared-capital basket** only after a pair shows a positive result on a tradable sample; it adds cost and margin to zero-edge pairs otherwise.
4. Optional branch: cash-and-carry versus LQDT with explicit dividend/tax and share-collateral facts (broker evidence needed).

Earlier records (`analytical-baseline.md`, `pair-screen.md`, `funding-screen.md`, `carry-*.md`, S1/S2 numbers) used idle-cash accounting, close-to-close fills and illustrative costs; they stay as dated history and are superseded for new runs.

This is exploratory evidence, not strategy approval or permission to fund or trade.
