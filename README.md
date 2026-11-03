# MOEX Spread Lab

Private, research-first project for selective medium-term MOEX futures spread signals, evaluated against an LQDT counterfactual. Initial execution: manual through VTB. Later candidate: BCS automation. No trading software or validated strategy exists yet.

## Current status

Stage 9 development is in progress: **W1 ISS catalog, all-instrument timeframe inventory and CNYRUBF/CRZ6 candle intake are delivered** alongside the W0–W7 offline mechanics. Under decisions A-05/A-06, the pair is conditionally admitted for analytical research; historical executable quotes remain unavailable and execution claims stay blocked. No project service was deployed and no orders were submitted.

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
- [Stage 2 artifact and changes](docs/stages/02-research.md)
- [Provisional delegated risk rules](docs/risk/pilot-policy.md)
- [User decisions](docs/decisions.md)
- [Stage 1: unpacking, objections and questions](docs/stages/01-unpacking.md)
- [Stage sequence](docs/process.md)
- [Inputs and evidence handling](docs/inputs/README.md)
- [Account/risk blockers](docs/blockers/b01-account-and-risk.md)
- [Workflow/research blockers](docs/blockers/b02-research-and-workflow.md)

## How to continue

Start with R01, R02, R04, R05, R06 and R09, using one standalone prompt per investigation. Attach the provided tariff PDF to R02. Return reports in any format with dated sources; incomplete/blocked reports are useful too. R10–R15 are standalone as well: the optional regime/text/neural investigations may run at any time and stay conditional designs until actual tests exist.

Installation/access verification, architecture, technology selection and implementation follow their own stages. Delegated numeric risk rules are provisional controls to test, not evidence of safe or profitable trading. Internal funding ceilings do not ring-fence the core brokerage portfolio.
