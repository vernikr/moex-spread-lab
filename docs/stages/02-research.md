# Stage 2 — Research inventory and ready-to-use prompts

Delivered 2026-10-07. **Scope: research design only. The user performs the research. No investigation has been run or report accepted.** No stage-3 environment setup, production access, market data collection, strategy implementation or trading occurred.

## Artifacts

- [Research pack: sequence, dependencies, literature/channel leads](../research/README.md)
- [Fifteen standalone prompts](../research/prompts/)
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

Then use their findings for R07 risk/sizing and R08 delayed execution. R10 deterministic hypotheses, R11 operational feasibility and R12 event/dividend provenance are standalone prompts that may run in any order, embedding their working assumptions. R03 BCS API can run early but is needed before automation, not to assert manual-stage API functionality. R13–R15 regimes/text/neural models are standalone optional extensions with embedded reference baselines.

The full inventory contains deliverables, evidence standards, dependencies and stopping conditions. Books/channels are targeted leads, not claimed consulted sources or instructions to ingest everything.

## Exit and next gate

Stage-2 **prompt delivery** is complete; **research execution/evidence acceptance is pending**. Stage 3 remains a separate turn for authorized tools/access and safe VPS verification; it need not wait for optional research. No live-trading feasibility or stack decision should be declared while its mandatory evidence is missing. Received findings may revise previous artifacts with explicit reasons.

## User-requested refinement — 2026-10-07

Localized R01–R06 and R12 fully into Russian; other prompts remain English with required Russian-source routes. All 15 now distinguish report language from search languages, explicitly identify conditional Chinese/Polish/original-jurisdiction searches where useful, and separate scarce maximum-breadth discovery from conditional specialist public-page retrieval. [The consolidated research README](../research/README.md) ranks scarce runs and substitution rules. Prompt language and agent guidance reflect the language exception. No searches, access tests, risk-policy changes or later-stage work were performed in this refinement.

## External-agent handoff refinement — 2026-10-07

The research README now includes search-resource priorities, language routes and an explicit attachment/dependency guide. Separate search-guide and response-template files were removed at the user's request; report format is unrestricted, while substantive questions and evidence traceability remain. Every prompt now defines its referenced Rxx inputs and behavior when they are absent. R05/R07/R08 embed the relevant detailed policy semantics; R11 embeds the necessary hardware constraints. Only R02 has a mandatory original attachment for its attached-tariff review; downstream project-specific conclusions need supplied predecessor findings. Optional extension plans are not evidence of measured baseline performance. No research, access checks or risk-policy changes occurred.

## User-requested refinement — 2026-10-22

Rebuilt R10–R15 as fully standalone prompts: each now embeds its working assumptions (data class, cost scenarios, validation skeleton, workload ranges or default scope) and lists no other research reports as inputs; the deferral wording in their status sections was removed accordingly. The research README start sequence, dependency inventory, attachment table and unknowns list plus the root README were updated. R02 still requires the tariff PDF; R07/R08 keep predecessor inputs for project-specific refinement. No research, access checks or risk-policy changes occurred.
