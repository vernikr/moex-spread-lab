# Stage 2 — Research inventory and ready-to-use prompts

Delivered 2026-10-07. **Scope: research design only. The user performs the research. No investigation has been run or report accepted.** No stage-3 environment setup, production access, market data collection, strategy implementation or trading occurred.

## Artifacts

- [Research pack: sequence, dependencies, literature/channel leads](../research/README.md)
- [Fifteen standalone prompts](../research/prompts/)
- [Research response contract](../research/response-template.md)
- [Report return instructions](../research/results/README.md)
- [Delegated provisional pilot risk policy](../risk/pilot-policy.md)
- [Updated decisions](../decisions.md) and [account/risk blockers](../blockers/b01-account-and-risk.md)

## Revisions to stage 1

- A-04/A-05: user clarified that the tolerated 10–15k final loss is **underperformance vs LQDT** and delegated interim drawdown/top-up selection. It is no longer an open absolute-vs-relative question.
- Selected pilot defaults: 10k peak drawdown on the excess-result curve; action at 10k cumulative LQDT shortfall before the 15k outer boundary; 15k lifetime gross additional funding (45k total contributed envelope), explicit approval for each use, existing-position liquidity/de-risking only. No automatic core-collateral sweep or reset of losses/funding usage.
- These are versioned operational judgments, not validated maximum loss estimates. R01/R05/R07/R08 must verify account, accounting, stress and execution feasibility. The smallest position may be rejected.
- Original source files are unchanged. Stage-1 interpretation, decision register, blockers, process and agent guidance were updated. No strategy, architecture or technology choice has been silently approved.

## Research sequence

First run R01 broker/collateral, R02 costs (attach the provided tariff PDF), R04 data, R05 LQDT accounting, R06 universe/economic links and R09 validation. These can proceed independently with unresolved cross-report assumptions made explicit. Return their reports and contradictions before selecting a model.

Then use their findings for R07 risk/sizing, R08 delayed execution, R10 deterministic hypotheses and R11 operational feasibility. Add R12 event/dividend provenance when relevant to candidate families. R03 BCS API can run early but is needed before automation, not to assert manual-stage API functionality. R13–R15 regimes/text/neural models stay optional until simple baselines justify further work.

The full inventory contains deliverables, evidence standards, dependencies and stopping conditions. Books/channels are targeted leads, not claimed consulted sources or instructions to ingest everything.

## Exit and next gate

Stage-2 **prompt delivery** is complete; **research execution/evidence acceptance is pending**. Stage 3 remains a separate turn for authorized tools/access and safe VPS verification; it need not wait for optional research. No live-trading feasibility or stack decision should be declared while its mandatory evidence is missing. Received findings may revise previous artifacts with explicit reasons.
