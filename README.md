# MOEX Spread Lab

Private, research-first MOEX futures-spread screener. Select a pair, strategy and parameters, compare the resulting finite-capital wealth/risk path with matched LQDT, and search parameters for a pair or basket with later-period validation. Manual VTB signals and BCS automation are later gated options; no validated profitable strategy or trading service exists yet.

## Current status

Stage 9 development is in progress. The offline W0–W7 mechanics, ISS inputs, LQDT benchmark, CNYRUBF/CRZ6 exploratory strategies, six-pair price screen, four-pair funding screen and working VTB EBS/КПУР risk scenarios are delivered. None of the frozen tested rules beat matched LQDT under its stated assumptions; none is promoted to an executable/funded pilot. The user-confirmed 2026-11-08 priority is a **visible local screener**, then bounded automatic parameter search for a selected pair and basket; the reference SBRF calendar-spread strategy will be reproduced separately. Historical candle/settlement prices remain labelled analytical proxies under A-08, not actual fills. No screener UI, broker service or project VPS deployment exists yet, and no orders were submitted.

- **[Research pack: what to run first](docs/research/README.md)**
- [Focused research intake and architecture direction](docs/research/accepted/2026-10-26-critical-gates.md)
- [Stage 3: environment preflight](docs/stages/03-environment.md)
- [Stage 4: functional and business requirements](docs/stages/04-requirements.md)
- [Stage 5: architecture](docs/stages/05-architecture.md)
- [Stage 6: stack and data sources](docs/stages/06-stack-and-data.md)
- [Stage 7: reuse analysis](docs/stages/07-reuse-analysis.md)
- [Stage 8: delivery and validation plan](docs/stages/08-planning.md)
- [W0: reproducible run and evidence manifest](docs/development/w0.md)
- [W1–W7: offline research waves](docs/development/w1-w7.md)
- [W1: MOEX ISS catalog and price-role intake](docs/development/w1-intake.md)
- [First analytical baseline](docs/development/analytical-baseline.md)
- [W4/W5 analytical execution ledger](docs/development/execution-ledger.md)
- [W7 chronological validation](docs/development/w7-validation.md)
- [Stage-9 research gate decision](docs/development/research-gate.md)
- [CNYRUBF/CRZ6 contract-history audit](docs/development/contract-history-audit.md)
- [H2 funding-aware carry decomposition](docs/development/carry-study.md)
- [H2-S1 frozen carry strategy](docs/development/carry-strategy.md)
- [H3-F1 funding-only carry hypothesis](docs/development/funding-only-strategy.md)
- [Flagship futures-pair screen](docs/development/pair-screen.md)
- [Funding-aware flagship-pair screen](docs/development/funding-screen.md)
- [CNYRUBF/CRZ6 point-in-time evidence audit](docs/development/point-in-time-evidence-audit.md)
- [VTB account risk profile](docs/development/vtb-account-risk-profile.md)
- [VTB EBS/KПУР account-risk screen](docs/development/vtb-account-risk-check.md)
- [Stage 2 artifact and changes](docs/stages/02-research.md)
- [Provisional delegated risk rules](docs/risk/pilot-policy.md)
- [User decisions](docs/decisions.md)
- [Stage 1: unpacking, objections and questions](docs/stages/01-unpacking.md)
- [Stage sequence](docs/process.md)
- [Inputs and evidence handling](docs/inputs/README.md)
- [Account/risk blockers](docs/blockers/b01-account-and-risk.md)
- [Workflow/research blockers](docs/blockers/b02-research-and-workflow.md)

## How to continue

Follow the revised [Stage-8 plan](docs/stages/08-planning.md): S1 local pair/strategy/parameter workbench and LQDT charts; S2 bounded pair optimizer; S3 independent SBRF reference-strategy reproduction; S4 shared-capital basket evaluation and optimizer. Each slice has its own acceptance gate. The detailed `СКРИНЕР_epub.md` is [preserved as a product hypothesis](docs/research/reuse/СКРИНЕР_epub.md), not verified market evidence. The received R01–R15 reports and [research pack](docs/research/README.md) remain source material for evidence gaps, not a prerequisite to showing the first local view.

Delegated numeric risk rules are provisional controls, not evidence of safe or profitable trading. Internal funding ceilings do not ring-fence the core brokerage portfolio.
