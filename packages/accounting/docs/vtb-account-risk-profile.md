# VTB account risk profile for offline calculations

Date: 2026-11-03

## Purpose

This profile replaces the earlier wording that treated every account input as unknown. It combines user-confirmed facts, public VTB/NCC evidence and explicitly labelled working approximations. It is an offline calculation profile, not a broker confirmation or permission to trade.

## Current profile

| Item | Working value | Evidence/status |
|---|---|---|
| Broker | VTB | User project fact |
| Account structure | Единый брокерский счёт (EBS) | User-confirmed under A-10; VTB publicly describes EBS as combining the main, currency and futures markets |
| Risk category | КПУР | User-confirmed under A-10; VTB publicly describes lower risk rates and greater borrowing capacity for КПУР |
| VTB futures broker fee | RUB 0 per contract through 2026-12-31 | User tariff PDF under A-09 |
| VTB futures broker fee after 2026-12-31 | RUB 1 per contract as the working published tariff value | R02/App. 9/public VTB tariff; may be changed by a later tariff or promotion |
| LQDT collateral eligibility | Assume eligible in the EBS pool | VTB list observed 2026-10-07 includes LQDT and VTB describes EBS as a cross-market collateral pool; exact current client treatment is not displayed in this project |
| LQDT risk discount | 8% for КПУР | VTB list observed 2026-10-07; use as a dated snapshot, not a permanent rate |
| LQDT as pilot cash | Excluded | Project risk policy; do not treat collateral as settled RUB for variation-margin debits or as an increase to the RUB 30,000 entry envelope |
| CNYRUBF current initial margin | RUB 970.82 | ISS snapshot dated 2026-10-27; not a historical series |
| CRZ6 current initial margin | RUB 1,153.64 | ISS snapshot dated 2026-10-27; not a historical series |
| Pair gross current margin approximation | RUB 2,124.46 | Sum of the two current snapshot values; the VTB EBS/client calculation may differ and no margin offset is assumed |
| Variation margin | Model as cash obligation | Supported by received R01 analysis; LQDT collateral is not substituted for settled RUB |
| VTB liquidation trigger | `НПР2 < 0` | Public VTB/NCC materials and R01; the agreement may contain stricter provisions |
| VTB liquidation target for КПУР | Work with `НПР2 = 0` | Public category rule; exact client agreement and lot-rounding effects remain unverified |
| VTB restrictive time | 14:00 MSK for futures portfolio; 16:00 MSK for main/EBS portfolio | R01 summary of VTB position-closing rules; not a guaranteed reaction window |
| Isolation from LQDT | Not assumed | EBS and broker discretion mean the core portfolio may support or be sold for obligations unless separately bounded |
| EBS under-covered-margin charge | Apply the published EBS formula as a separate sensitivity: `(ГО − RubCash) × (key rate + 2%) × T/365(366)` when RUB cash is below ГО | R02/App. 9 §16; exact account application, valuation time and client display remain unverified |

## Broker risk mechanics used in the model

The public framework is represented as follows:

- `НПР1 = S − M0 − Sблок`;
- `НПР2 = S − Mx`;
- `Mx = 0.5 × M0`;
- the minimum permitted `НПР2` is zero;
- for КПУР, the working liquidation depth is `НПР2 = 0`, subject to stricter contractual wording;
- if `НПР2 < 0`, VTB may close positions within the applicable deadline and chooses the subject of closure under its procedure; preservation of both spread legs is not guaranteed;
- the relevant restrictive times reported in R01 are 14:00 MSK for the futures portfolio and 16:00 MSK for the EBS/main-market portfolios. These are broker deadlines, not a guaranteed warning or reaction period.

The ordinary offline screen therefore uses the current two-leg margin sum, zero margin offset and cash-only variation-margin reserve. A stress branch must test a single-leg requirement, loss of the pair offset, higher broker risk parameters and the EBS under-covered-margin charge.

## What this changes in research

1. Account-level terminology is no longer completely unknown: the calculations may use EBS + КПУР as the declared account profile.
2. The current pair margin estimate is sufficient for an analytical feasibility check, but it is not used as a safety guarantee. A one-leg failure, changed risk rate, removed spread offset or under-covered-margin charge can require materially more funds.
3. The 8% LQDT discount is recorded for collateral stress calculations, but core LQDT remains outside strategy sizing. Its possible involvement is an adverse shared-account path, not available reserve capital.
4. The VTB broker fee is now included in the current-period cost profile. Exchange/clearing charges, funding, exit costs and the possible cost of negative cash remain separate.
5. After 31.12.2026, use RUB 1 per contract as the working broker-fee scenario until a newer applicable tariff is supplied or found. Do not apply it retroactively.

## Remaining unknowns and chosen approximations

| Unknown | Working approximation | Treatment |
|---|---|---|
| Exact amount of LQDT included in EBS collateral | All eligible LQDT may be included | Adverse account-risk scenario; not pilot capital |
| Current VTB client coefficient and pair offset | Use current ISS margin sum with no offset for first feasibility screen | Re-run with a higher one-leg margin scenario before any real activation |
| Cash available for variation margin | Only the declared RUB 30,000 pilot envelope is available | Do not count LQDT sale proceeds or emergency funding at entry |
| Current individual tariff after the promotion | RUB 1 per contract | Sensitivity only; later bank tariff overrides it |
| Exact notification and account display timing | No guaranteed warning period | Internal controls act earlier; manual reaction delay remains part of the proxy model |

These approximations are intentionally asymmetric: they may make the ordinary path easier to calculate, but they do not turn LQDT into a safety reserve or cap the loss path.

## Sources

- User-confirmed facts recorded in `docs/decisions.md` as A-09 and A-10.
- `docs/inputs/moy_online_praym.pdf` — current VTB tariff supplied by the user.
- `docs/research/results/received/R01/R01.md` — VTB/NCC risk, EBS, LQDT, КПУР and liquidation research; account-specific settings remain separate.
- `docs/research/results/received/R02/R02.md` — VTB tariff and post-2026 fee findings.
- `data/w1/current-intake.json` — current CNYRUBF/CRZ6 margin snapshot.
- `docs/risk/pilot-policy.md` — exclusion of core LQDT from entry funding and treatment of shared-account recourse.

This profile is a research input, not a statement that the broker will apply these values to a particular order or account event.
