# Profit audit and S3 reference reproduction

Date: 2026-11-08. Research only: no broker, VPS, Telegram or trading. Prices are candle-close or settlement proxies (A-06/A-08).

## 1. Is the calculation and data path correct?

Reproduce: `audit-pairs --output data/analytics/pair-audit.json` (code: `audit.py`).

| Check | Result |
|---|---|
| Independent re-implementation of the mean-reversion control from raw candle JSON (no engine code) | Gross P&L and trade count identical for 12/12 local pairs. No look-ahead: signal bar `t`, fill bar `t+1`, window excludes the signal bar. |
| Per-leg price units (`tick_value / tick_size`) | Consistent for all pairs; spread values are RUB per pair. |
| Funding units (`SWAPRATE × lot`) | Ad-hoc check on cached EOD history: short-perpetual funding is 13–15 % p.a. of notional for SBERF, GAZPF, IMOEXF, GLDRUBF and about 12 % for CNYRUBF (RUB-rate differential) — economically plausible, so the unit and sign are right. |
| Candle close vs EOD `SETTLEPRICE` | Spread noise is similar (daily spread σ within ~10 % on four of five pairs, 37 % apart on gold); mean-reversion gross P&L is of the same size and sign on both price sources. The candle proxy is not what makes the results negative. |
| LQDT benchmark | Observed layer 2024 ≈ 18.5 %, 2025 ≈ 20.7 %, 2026 YTD ≈ 15 % p.a.; consistent with the rate cycle. |
| Tests | 50 hermetic tests pass. |

No arithmetic or alignment defect was found. The negative table is a modelling-design effect plus weak data, not a bug.

## 2. Why every row is negative

1. **Hurdle design.** `V` earns nothing on idle cash while `B` compounds LQDT on the entire RUB 30,000. One contract pair needs 7–93 % of the allocation as margin, so the strategy must out-earn LQDT on 100 % of the money while using a fraction of it. Over the 155-day screen window the hurdle is about RUB 2,590; gross P&L of 1-contract pairs ranges from −RUB 1,500 to +RUB 2,700. All 12 rows have negative `X` (best −629).
2. **Overlay view** (idle cash parked in LQDT; subtract only LQDT return on pair margin while a position is open): with stress costs 2/12 rows are positive (`SBERF/SRZ6` +585, `USDRUBF/SiZ6` +663), with zero costs 3/12. Both are single dated contracts, 4–7 trades, and the far leg of `SBERF/SRZ6` has no real volume on 27 % of days. Treat as unconfirmed.
3. **Weak data.** Screened pairs have 155 daily rows of one dated contract. Far legs trade thinly for long stretches (`SRZ6` 27 % of days under 100 contracts, `S2Z6` 36 %, `CRZ6` 24 %, `EuZ6` 24 %). Weekend sessions exist in the CNY pair. Stale far-leg prints create fake spread excursions.
4. **Cost scale.** Illustrative costs are flat RUB per leg-side, so the default `stress` profile is far above realistic exchange fees for cheap-notional pairs (CNY, gold, IMOEX) and adds a per-leg-day "funding" cost that double-counts the LQDT opportunity cost.
5. **Non-stationary spread.** A perpetual/dated spread decays toward zero at expiry; a 20-day z-score on the raw spread mixes that drift with mean reversion.

## 3. S3 — SBRF reference reproduction

Reproduce: `s3-reference --reference-dir <calendar-spread checkout> --output data/analytics/s3-sbrf-reference.json` (code: `s3_reference.py`; input is the reference project's MIT-licensed processed data, read in place, not copied).

The reference result is reproduced exactly: convergence strategy 4 trades, net +RUB 1,947; z-score 41 trades, net +RUB 5,367. Then:

| Variant | Convergence (n=4), net RUB | Z-score, trades / net RUB |
|---|---:|---:|
| Reference (same-bar fill, window includes the signal bar, no liquidity filter) | +1,947 | 41 / +5,367 |
| Fill one bar after the signal, window excludes signal bar | −394 | 43 / −757 |
| + both legs ≥100 contracts | +1,700 | 25 / −2,487 |
| + both legs ≥500 contracts | +371 | 16 / −775 |
| + both legs ≥1,000 contracts | +1,452 | 13 / −883 |

Findings:

- The z-score "profit" exists only when the signal and the fill share one candle close and the rolling window contains the signal bar; it disappears with a one-bar delay and turns negative once thin days are removed.
- The convergence "profit" rests on four hold-to-expiry trades. The first entry (`SRH5/SRM5`, spread 2,122) is a stale first-day print: the next day's spread is 600. Entering one bar later gives −1,054 on that trade. Outcomes of the four trades range −394…+1,700 depending only on liquidity thresholds.
- Even the reference result does not beat LQDT: its overlay excess is −RUB 1,448 (four long holds on about RUB 10,000 margin earn less than LQDT on that margin). This matches the reference's own README ("alpha ≈ 0" for convergence).
- Ad-hoc check (not versioned): entering only liquid, no-dividend days and holding to expiry, the signed spread P&L rises with deviation (|dev| ≥ 2 pp: ≈ +440 per pair; 1–2 pp: ≈ +70; < 1 pp: ≈ +20), but the ≥ 2 pp bucket is one episode (`SRH5/SRM5`, Dec 2024–Mar 2025, peak-rate regime). About five independent episodes exist in two years; the statistics cannot support a rule.
- Dividend windows are excluded as in the reference; dated historical margin, dividend tax and the native spread instrument's netting remain unverified.

Decision on b02.6: the reference gain is **not accepted as evidence**. S3 is closed as a reproduction; a different, liquidity-aware calendar design would be a new hypothesis.

## 4. Where profit could come from

Structural carry is priced at the risk-free rate, so it only ties with LQDT:

| Source | Evidence here | Excess over LQDT |
|---|---|---|
| Short perpetual + long dated (existing funding-only) | Funding received ≈ basis loss; gross carry −215…+84 RUB over 155 days | ≈ 0 |
| Long underlying + short perpetual | Funding 13–15 % p.a. of notional vs LQDT ≈ 14.5 % (ad-hoc) | ≈ +0.5…1 pp (stocks, gold), ≈ +3.5 pp (silver), before hedge costs and dividends |
| Long stock + short dated future (cash-and-carry) | Implied rate ≈ RUONIA +0.7…1.3 pp on liquid contracts, 2024–2026 (ad-hoc, approximate ex-dates) | ≈ +1 pp on notional ≈ RUB 300/yr per RUB 30,000; dividend tax and collateral rules unverified |
| Calendar convergence / z-score | Section 3 | None demonstrated |
| Rate-path view (long far / short near in a cutting cycle) | The only profitable episodes above | A directional bet, not arbitrage |

Plausible next steps, in order of evidence value:

1. **Decide the hurdle** (new user decision): keep full-allocation `V − B`, or add the overlay metric (cash left in LQDT, only used margin charged). The current table can only show negative numbers for 1-contract pairs under the first rule.
2. **Fix inputs before more search**: multi-year contract history with a liquidity mask (≥ 500–1,000 contracts on both legs), cost profiles scaled to notional (exchange rate × notional), and no per-leg-day funding cost.
3. **S4 shared-capital basket only after** at least one pair shows a positive overlay result on a liquid sample; a basket of zero-edge pairs only adds cost and margin.
4. Optional research branch: cash-and-carry versus LQDT with explicit dividend/tax and share-collateral facts (needs broker evidence, R-task).

This is exploratory evidence, not strategy approval or permission to fund or trade.
