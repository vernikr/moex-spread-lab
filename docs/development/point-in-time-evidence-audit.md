# CNYRUBF/CRZ6 point-in-time evidence audit

Date: 2026-11-03

## Conclusion

The public primary-source review closes part of the H2 evidence gap. It confirms the funding sign, funding unit, the dated CNYRUBF `K1/K2` regime change, the CR contract unit and fixing, and the current CRZ6 expiry. It does **not** turn the H2-S1 result into an executable or historically complete result.

The next implementation should use the dated funding regimes and explicitly model the perpetual-leg execution/assignment window. The user has now confirmed EBS and КПУР; working account assumptions are recorded in `vtb-account-risk-profile.md`. Historical account terms and point-in-time margin remain open. Under A-08, historical order-book and actual-fill data are a declared unavailable input rather than a future collection task.

## Evidence matrix

| Input | Dated evidence | Result for the baseline window | Status |
|---|---|---|---|
| CNYRUBF funding sign | MOEX perpetual-futures page: positive funding is paid by longs and received by shorts; values are shown per unit and must be multiplied by the 1,000 CNY lot. | The H2 direction `short CNYRUBF` receives positive `SWAPRATE`. | Confirmed |
| CNYRUBF funding field | MOEX states that `SwapRate` is published in contract results and in ISS/market-data fields. The local ISS history contains `SWAPRATE`. | The local daily funding intake is a usable public EOD cash-flow field, subject to its settlement-day interpretation. | Confirmed for analytical use |
| CNYRUBF `K1/K2` before 2023-08-22 | MOEX notice 2023-08-15: CNYRUBF `K1=0.075%` changed to `0.03%`; `K2=0.35%` remained unchanged from 22.08.2023. | Outside the current H2 window start, but establishes the prior regime. | Confirmed |
| CNYRUBF `K1/K2` 2023-08-22 to 2026-01-18 | MOEX perpetual-futures page and 2024-07-22 notice show `K1=0.03%`, `K2=0.35%` for CNYRUBF. | Applies to the first part of the H2 window. | Confirmed |
| CNYRUBF `K1/K2` from 2026-01-19 | MOEX notice 2026-01-15 and current perpetual-futures page: `K1=0%`, `K2=0.35%` from the evening clearing of 19.01.2026. | Applies to the second part of the H2 window. | Confirmed |
| CR contract units | MOEX general FX-futures parameters: 1,000 CNY, tick `0.001` RUB, tick value RUB 1, cash-settled, `CNYFIXME` execution reference. | Confirms the current analytical conversion used by H2. Historical continuity of every specification field is not independently archived. | Confirmed for current specification |
| CRZ6 date | MOEX contract search lists `CRZ6` / `CNY-12.26` with last trading and expiry date 17.12.2026. | Confirms the current far-leg date. | Current snapshot confirmed |
| CNY fixing treatment | MOEX 12.07.2024 notice says CNY/RUB futures execution remains on `CNYFIXME` in the intermediate clearing; the general parameters page identifies `CNYFIXME` as the CR fixing. | The H2 TOM-versus-CNYFIXME residual risk is real and must remain explicit. | Confirmed; historical method version still needed |
| Perpetual execution/assignment | MOEX 15.04.2025 notice describes a 3% one-time payment and a 10-trading-day instruction window for currency perpetual execution into quarterly futures. | H2-S1 did not model this mechanism or any assignment around quarterly dates. | Open implementation gap |
| Exchange and clearing fees | MOEX current futures tariff page gives current FX taker and execution tariff formulas, and says maker commission is zero. | Current exchange-level inputs are available for a new scenario; historical applicability and broker charges remain unresolved. | Current only |
| Historical executable prices | Public EOD history has settlement, close and `SWAPRATE`; the project will not obtain historical bid/ask, depth, queue or fills. | H2 remains a settlement-proxy study with manual delay and cost sensitivity. | Deliberately unavailable by A-08 |
| Historical ГО and account terms | NCC describes current risk-parameter services, but the historical/account-specific series and VTB/BCS terms are not established. | No safe capital or live-pilot conclusion. | Missing |

## Regime map for H2

The H2 interval `2025-06-17`–`2026-10-27` must be labelled at least as:

1. `2025-06-17`–`2026-01-18`: CNYRUBF `K1=0.03%`, `K2=0.35%`;
2. `2026-01-19`–`2026-10-27`: CNYRUBF `K1=0%`, `K2=0.35%`.

The actual ISS `SWAPRATE` values remain the preferred historical cash-flow input. The `K1/K2` series is needed to explain the mechanism and to validate/reconstruct missing values; it must not be used to overwrite observed `SWAPRATE` rows.

The 15.04.2025 perpetual-execution change predates the H2 interval and therefore applies to its whole window. The H2-S1 implementation needs an event layer for this rule before any claim about continuous holding through quarterly dates.

H2-S1 now carries an explicit `exit_before_perpetual_execution_window` policy. Because the local input ends before the CRZ6 ten-trading-day window, the run records `not_observable_in_input_window`; it does not infer a date from a generic weekday calendar and does not add the 3% execution payment.

## Consequences for H2-S1

- The funding direction and lot conversion are supported better than in the previous audit.
- The local run now labels 150 observations under `K1=0.03%` and 199 under `K1=0%`; all 348 holding-day rows pass the rounded `K2=0.35%` consistency check.
- The gross funding decomposition can remain an analytical result, with the regime label added to its provenance.
- H2-S1 remains a proxy-only result because it used current snapshot contract fields, illustrative costs, settlement marks and no assignment/roll handling.
- The open position at data end is not an expiry result: the available history ends on 27.10.2026, while CRZ6 expires on 17.12.2026.
- A future run must distinguish ordinary two-leg offsetting from a perpetual-to-quarterly execution event. These are different economic paths and must not be silently treated as the same exit.

## Sources

Official public pages, accessed 2026-11-03:

- [MOEX perpetual FX futures](https://www.moex.com/a8141) — funding sign, lot conversion, formula, `K1/K2` regime table and ISS/contract-result availability.
- [MOEX CNYRUBF K1 change from 22.08.2023](https://www.moex.com/n63166) — prior regime boundary.
- [MOEX CNYRUBF K1 change from 19.01.2026](https://www.moex.com/n96856) — second regime boundary.
- [MOEX CNYRUBF/CRZ6 execution and fixing notice](https://www.moex.com/n71018) — CNYFIXME execution treatment.
- [MOEX general FX futures parameters](https://www.moex.com/a3873) — CR lot, tick, cash settlement and fixing.
- [MOEX contract search](https://www.moex.com/ru/derivatives/contracts-search) — current CRZ6 last trade and expiry date.
- [MOEX perpetual-futures execution update](https://www.moex.com/n89495) — 3% payment and 10-trading-day execution instruction window.
- [MOEX derivatives tariffs](https://www.moex.com/s93) — current exchange/clearing tariff structure.
- [NCC perpetual-futures execution tariff notice](https://www.nationalclearingcentre.ru/catalog/0701/66032) — historical 2023 execution tariff change.

Local evidence and limits:

- `data/analytics/cnyrubf-crz6-carry-study.json` — local ISS EOD intake and funding decomposition.
- `docs/research/results/received/R06/R06.md` — received research synthesis; used as intake and not upgraded into account-specific evidence.
- `docs/development/carry-strategy.md` — H2-S1 proxy result.

This document records evidence status. It is not a broker approval, legal opinion, execution reconstruction or trading recommendation.
