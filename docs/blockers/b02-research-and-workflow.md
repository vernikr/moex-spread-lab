# b02 — Workflow and evaluation definitions

Status: open. Stage-2 prompts and Stage-7 reuse intake delivered; account, execution, validation and workflow evidence remain pending.

- b02.1 — A-06/A-15: define trade/intervention quota, availability and response assumptions, including emergencies. Owner: user; R08 supplies a workflow assessment.
- b02.2 — A-11/A-12/A-33: validate the comparison/tax convention and criteria for usefulness beyond LQDT. Loss basis is confirmed; a 90-day formal review is provisionally selected without forced exits/resets. Owner: R05/R07/R10, user for tax/workflow facts.
- b02.3 — **Partly resolved:** public MOEX/NCC evidence now confirms the CNYRUBF funding sign/unit, the 2023 and 2026 `K1` regime boundaries, CR units/fixing and the current CRZ6 expiry; the local run labels the regimes and checks the rounded `K2` cap. See `../development/point-in-time-evidence-audit.md`. Under A-08, historical order-book and actual-fill data are permanently outside this project; the remaining open items are perpetual assignment handling in observed data, point-in-time margin/cost applicability and account-level validation. Owner: user research using R01/R04/R06/R09/R10 (R12 if event-sensitive).
- b02.4 — A-30: **Partly resolved:** read-only SSH access and a current resource snapshot were completed in stage 3. Actual workload benchmark, project deployment and Russian-client path remain open. Owner: stage 4/implementation planning.
- b02.5 — **Resolved:** A-34 prior-project materials were reviewed at stage 7; price/event/data-source revisions are recorded in `../stages/07-reuse-analysis.md` and linked updates to stages 4–6.
- b02.6 — **Partly resolved 2026-11-08:** the reference SBRF calendar backtest was reproduced exactly (4 convergence trades +RUB 1,947; 41 z-score trades +RUB 5,367) and then fails under one-bar delay, own-window z-scores and liquidity filters, and does not beat LQDT even as published; it is not evidence (`../development/profit-audit.md`). The hurdle choice is resolved by A-11/A-12. Still open: a shared-capital basket (S4), liquid multi-year inputs, and whether calendars with a late-liquid far leg may use a late-life volume window. Owner: user decision, then Stage-9 S4.

No implementation should fabricate missing facts to bypass these questions.
