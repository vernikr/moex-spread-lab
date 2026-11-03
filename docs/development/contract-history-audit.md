# CNYRUBF/CRZ6 contract-history audit

Date: 2026-11-03

The dated public-source follow-up is recorded in [point-in-time evidence audit](point-in-time-evidence-audit.md); this document remains the original scope audit for the price-only baseline.

## Scope

This audit checks whether the contract and market-mechanics inputs used by the analytical baseline are evidenced for its actual overlap: `2025-06-17`–`2026-10-27`. It does not test a new strategy and does not infer fills.

The primary local research input is the received R06 report, based on MOEX/ISS and specification materials dated around `2026-10-07`; the current local ISS intake was received `2026-10-27`. R06 is research intake material and is not silently upgraded into account-specific or complete historical evidence.

## Findings

| Item | Finding for the baseline window | Status |
|---|---|---|
| Pair identity | `CNYRUBF` is the CNY/RUB perpetual; `CRZ6` is CNY-12.26. | Confirmed by R06 and current ISS snapshot |
| Lot and tick | R06 reports 1,000 CNY, `0.001` RUB and RUB 1 per tick for both legs. | Confirmed for the dated R06 snapshot; historical continuity not proven |
| Hedge units | 1:1 in CNY is dimensionally valid for the reported snapshot. | Conditionally usable |
| Settlement reference | `CNYRUBF` follows CNYRUB_TOM; `CRZ6` settles against CNYFIXME with a Bank of Russia fallback. | Confirmed by R06; creates residual fixing-vs-TOM risk |
| Perpetual funding | Funding is a separate economic cash flow for `CNYRUBF`; R06 records positive observations and a parameter change effective `2026-01-19`. | Not included in the draft baseline; carry P&L is incomplete |
| Trading-session convention | MOEX unified session became effective `2026-03-23`; the daily-bar boundary and clearing mechanics changed. | Confirmed by R06; one static daily-bar convention is not historically neutral |
| Expiry | R06 reports CRZ6 expiry `2026-12-17`; the baseline ends before expiry. | Snapshot confirmed; dated specification chain not complete |
| Initial margin | Current intake supplies a current snapshot, not a point-in-time series over the baseline. | Fallback only |
| Exchange/clearing fees | R02 identifies dated MOEX/NCC tariff editions and ISS fee fields. | Historical contract-level application not calculated in baseline |
| Broker fee | R02 reports VTB promotional and tariff conditions, but the user's applicable plan and service status are not confirmed. | Unknown for the account |
| Executable quotes/fills | Candle data contain closes, not historical bid/ask, depth, queue or fills. | Missing |

## Consequences for the existing result

The daily baseline is a valid **price-proxy experiment for a fixed mean-reversion rule**. It is not a historically complete carry or execution P&L because:

1. it used candle closes and did not include the perpetual funding transfer;
2. it used current contract terms as a labelled fallback rather than a versioned specification at every decision date;
3. it spans the `2026-01-19` funding-parameter change and the `2026-03-23` session-convention change;
4. it used unknown or illustrative costs rather than an account-applicable historical schedule;
5. it has no executable historical fill path.

Therefore the result `X = −RUB 7,572.30` remains useful as a falsification result for the draft price-only baseline, but it must not be interpreted as a complete historical result for every CNY/RUB carry or calendar strategy.

## What is now admitted

The pair remains conditionally admitted for offline research under two separate hypotheses:

- **H1 — price-only mean reversion:** already tested as the draft baseline; no promotion result;
- **H2 — funding-aware perpetual-versus-dated carry/basis:** not yet implemented or tested. It requires point-in-time funding rules, fixing-vs-TOM treatment, expiry handling, dated costs and a separate validation record.

Failure of H1 does not reject H2 or the pair. H2 must not reuse H1's result as if it were a carry test.

## Required evidence to close the audit

No user action is required to preserve offline research or to begin the H2 design. To make a historical economic claim, the project still needs:

1. effective-dated CNYRUBF and CRZ6 specifications covering the baseline dates, including fixing, expiry and session rules;
2. point-in-time funding parameters and a reproducible funding series for CNYRUBF;
3. historical initial-margin/risk inputs or an explicit conservative scenario treatment;
4. dated exchange/NCC and account-applicable broker costs;
5. executable quote/fill evidence, or an explicit decision to keep all results proxy-only.

The first four may be researched from public primary documents. Account-applicable broker terms and any private quote/fill evidence require user-provided or broker-provided material; credentials and account identifiers are not needed.

## Sources and limits

- `docs/research/results/received/R06/R06.md`, especially sections 0, 2, 6, 10.2, 11, 14 and 16: received research synthesis with MOEX/ISS/specification references and explicit limitations.
- `docs/research/results/received/R02/R02.md`, sections 2.5 and 3.2: received tariff research with dated MOEX/NCC and VTB findings; account applicability remains unresolved.
- `data/w1/current-intake.json`: current ISS snapshot, explicitly marked `current_snapshot_fallback_assumed`.
- `docs/development/analytical-baseline.md`: the tested price-only baseline and its proxy boundaries.

This document is an evidence audit, not a strategy approval or a trading recommendation.
