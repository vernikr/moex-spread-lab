# GitHub Project & Agent Issue Workflow

Status: **Active operational regulation** (2026-11-23).
Project board: [`https://github.com/orgs/vernikr/projects/2`](https://github.com/orgs/vernikr/projects/2) (`moex-spread-lab`, organization `vernikr`, linked to `vernikr/moex-spread-lab`).

## Table of contents

- [1. Source-of-truth boundaries](#1-source-of-truth-boundaries)
- [2. Project v2 schema: statuses, fields, labels, and views](#2-project-v2-schema-statuses-fields-labels-and-views)
- [3. Issue hierarchy for large plans and refactors](#3-issue-hierarchy-for-large-plans-and-refactors)
- [4. Creating issues and managing dependencies (CLI & GraphQL)](#4-creating-issues-and-managing-dependencies-cli--graphql)
- [5. Tracking execution and intermediate progress](#5-tracking-execution-and-intermediate-progress)
- [6. Plan deviations, partial completion, deferrals, and blockers](#6-plan-deviations-partial-completion-deferrals-and-blockers)
- [7. Closing protocol and workstream reconciliation](#7-closing-protocol-and-workstream-reconciliation)
- [8. Bootstrap and UI-only Project workflow settings](#8-bootstrap-and-ui-only-project-workflow-settings)

---

## 1. Source-of-truth boundaries

`moex-spread-lab` separates durable engineering knowledge from project plans/reports and the live operational queue. GitHub is the sole source of truth for project plans and execution history; the repository must not keep a second copy of those records.

| Artifact | Canonical location | Owns | Must NOT duplicate |
|---|---|---|---|
| **Architecture, accounting & proof contracts** | `docs/stages/*`, `packages/proof/docs/execution-proof.md`, `packages/*/docs/*` | Stage-9 invariants (`G1`–`G12`), `run_id` / hash rules, module boundaries | Live task status (`in progress`, checkboxes) |
| **Owner decisions & risk limits** | `docs/decisions.md`, `docs/risk/pilot-policy.md` | Confirmed `A-01..A-14`, `BR-10`, capital/slippage rules | Open engineering backlog |
| **Cross-cutting external blockers** | `docs/blockers/bNN-*.md` | Multi-issue broker/tariff/account facts (`b01`, `b02`) | Per-issue execution status |
| **Module guides** | `packages/<name>/docs/` | Durable guidance owned by one package (contracts, invariants, boundaries, verification) | Cross-cutting governance that belongs in root `docs/development/` |
| **Durable technical references, runbooks, and formal stage artifacts** | `docs/`, `packages/*/docs/*` (only when retained as lasting engineering/research knowledge) | Stable contracts, reusable procedures, approved decisions, enduring research evidence | Project plans, active checklists, temporary execution reports, mutable delivery status |
| **Project plans and work items** | **GitHub Issues** (`vernikr/moex-spread-lab`), including comments | Plan, goal/scope/non-goals, acceptance criteria, verification gate, work-package slices | Tracked Markdown/JSON copies of the plan or live checklist |
| **Execution reports and delivery history** | **GitHub Issue bodies/comments** | Dated verification/bench evidence, completed work, checkpoints, deviations, deferrals and historical reports | Duplicate repository report files or generated result snapshots |
| **Operational queue** | **GitHub Project #2** (`orgs/vernikr/projects/2`) | Live `Status`, `Work Kind`, `Module`, `Effort`, `Business Value`, `Verification Gate`, `Sub-issues progress` | Problem statements (use real Issues, never Project Draft items) |

When a project plan/report contains enduring technical rules, keep only the durable rule in its owning reference and link to the Issue for plan/history; do not copy the project document. Archive existing plans/reports in the relevant Issue(s), update inbound links, then delete the repository copies. Never create a `TODO.md` or parallel master backlog. All repository artifacts, Issue titles, bodies, and comments are written in **English** (translate/reframe Russian user requests into concise English Issues while keeping chat replies in Russian).

The current source-document migration and reference cleanup are tracked in [#25](https://github.com/vernikr/moex-spread-lab/issues/25) and [work package #26](https://github.com/vernikr/moex-spread-lab/issues/26); do not restore the migrated copies.

---

## 2. Project v2 schema: statuses, fields, labels, and views

### 2.1 `Status` (single-select, Project #2)

 aligned with the `vernikr` organization standard plus explicit `Deferred` separation:

| Status | Meaning | Entry rule |
|---|---|---|
| **`Inbox`** | Newly filed, untriaged, or conditional target awaiting triage | Default for newly discovered follow-ups or unconfirmed stretch goals |
| **`Ready`** | Scoped, unblocked, acceptance criteria and verification gate explicit | All `blockedBy` dependencies closed; no pending owner decision |
| **`In Progress`** | Actively worked on in an agent session or open PR | Limit to active work; move here before editing tracked files |
| **`Blocked`** | Waiting on an upstream Issue (`blockedBy`), external `b01`/`b02` fact, or owner decision | Must carry label `blocked` + structured `**Blocked by:**` comment |
| **`Deferred`** | Deliberately postponed (high blast radius, conditional target, or split remainder) | Must carry label `deferred` + structured `**Deferred:**` comment with revisit trigger |
| **`Done`** | Closed (`completed` or `not_planned`) | Acceptance criteria + required verification gate passed, or explicitly retired (`not_planned`) |

There is no separate `Verify` column: verification is a mandatory pre-push gate (`./scripts/verify.py --profile full` + profile-specific checks), not a waiting queue.

### 2.2 Fields

- **Project fields** (local to Project #2):
  - `Work Kind`: `feat` | `fix` | `docs` | `refactor` | `chore` | `perf` | `test` | `research` | `revert` (conventional-commit style; `perf` means code/runtime performance, not agent or tooling latency).
  - `Module`: `core` | `data` | `accounting` | `strategies` | `proof` | `web` | `Cross-module` (single-select; use the owning package module, or `Cross-module` when work spans modules or is tooling/docs-only).
  - `Verification Gate`:
    - `Docs / Quick` — `scripts/docs_scan.py` + `./scripts/check_health.sh --changed` (docs-only edits).
    - `Full Suite` — `./scripts/verify.py --profile full` (standard pre-push gate).
    - `Parity + Bench` — `Full Suite` + bit-for-bit `run_id`/payload SHA-256 parity + workstation real-data probe (`moex-spread-lab remote bench`) when available.
    - `Browser Smokes` — `Full Suite` + `MOEX_OFFLINE_ONLY=1 uv run python scripts/run_browser_smokes.py` (mandatory when `packages/web` UI/routes change).
    - `C99 Sanitizers` — `Full Suite` + `clang-tidy` + `scripts/run_sanitized_pytest.py` (mandatory when `_fastcore.c` or pure-Python mirrors change).
- **Organization Issue fields** (shared across `vernikr` Projects):
  - `Effort`: `High` | `Medium` | `Low`.
  - `Business Value`: numeric `0`–`5` (`5` = core Stage-9 research/accounting outcome; `4` = load-bearing architecture/gate; `3` = meaningful agent velocity/hygiene; `1`–`2` = polish or stretch target).
- **System hierarchy fields**: `Parent issue`, `Sub-issues progress`, `Labels`, `Linked pull requests`.

### 2.3 Repository labels

- **Ownership (`area:*`)** — matches workspace topology for zero-conflict parallel agent routing:
  `area:core`, `area:data`, `area:accounting`, `area:strategies`, `area:proof`, `area:web`, `area:tooling`, `area:docs`.
- **Structure & lifecycle**:
  `refactor` (behavior-preserving restructuring), `bug` (regression / parity failure), `research` (intake / parity audit / R01–R15), `decision` (needs or records an owner choice), `blocked`, `deferred`, `plan-deviation`.

### 2.4 Views

1. **`All work`** (Table, unfiltered) — full inventory showing `Status`, `Work Kind`, `Module`, `Effort`, `Business Value`, `Verification Gate`, `Parent issue`, `Sub-issues progress`, `Labels`, `Linked pull requests`.
2. **`Queue`** (Board, `status:Inbox,Ready,"In Progress",Blocked`) — actionable leaf work packages grouped by `Status`.
3. **`Workstreams & Plans`** (Table, grouped or filtered by parent workstream) — high-level progress across multi-slice initiatives via `Parent issue` / `Sub-issues progress`.
4. **`Blocked & Deferred`** (Table, `status:Blocked,Deferred`) — items waiting on upstream dependencies, owner decisions, or revisit conditions.

---

## 3. Issue hierarchy for large plans and refactors

Use **at most two Issue levels** plus an in-body checklist. Over-decomposing into dozens of 50-LOC child Issues creates API/triage overhead and violates the S3 autonomy rule (`AGENTS.md`).

```text
Level 1: Parent workstream Issue (Type: Task)
  └── Level 2: Work Package Sub-Issue (native GitHub sub-issue, 1 coherent pushable deliverable)
        └── Level 3: "- [ ] Slice ..." markdown task list inside the Work Package body (atomic local commits)
```

### Rules of decomposition

1. **Level 1 — `<Plan or Stage-9 initiative>` parent issue**:
   - Created when work spans multiple sessions or packages. The parent issue body/comments are the canonical plan; link only to durable repository contracts/evidence, not to a plan file (examples: #12 Phase 7 follow-ups, #17 Stage 9 S4 basket, #19 Agent performance plan 2).
   - Body states: `Goal`, durable `References` (if any), `Exit criteria`, `Non-goals`, and execution order. Preserve superseded or completed plans as clearly marked historical Issue comments, not repository copies.
2. **Level 2 — Work Package (native sub-issue)**:
   - Represents **one coherent, independently verifiable deliverable** pushed to `origin/main` after `./scripts/verify.py --profile full` passes (typically 1–5 inner sub-slices, ideally scoped to one `packages/<name>/` subtree or one contract-first cross-package slice).
   - Attached to its parent workstream as a **native GitHub sub-issue** (never just a plain text link) so GitHub computes `Parent issue` and `Sub-issues progress` automatically.
3. **Level 3 — Inner Sub-Slices (checkboxes inside Level 2)**:
   - Listed under `## Slices` in the Work Package body (for example, `- [ ] service.py`, `- [ ] proof_kernel.py`).
   - Executed continuously within one turn using atomic local commits and `--profile affected` / `--profile quick`, without creating separate Issues.
4. **Just-in-time (JIT) child creation**:
   - If a high-risk Work Package (e.g., `#15` F2 unifying 4 built-in strategy families) is too large for a single deliverable when started, promote it to a child workstream or split out one sibling sub-issue per family **when starting work**, not weeks in advance. Never pre-populate speculative sub-issues for gated/unapproved initiatives (like #17 S4 while blocked on an owner decision).

---

## 4. Creating issues and managing dependencies (CLI & GraphQL)

Before modifying tracked repository files, find the open Issue covering the work or create one (`.github/ISSUE_TEMPLATE/{workstream,work-package,bug}.yml` define the expected sections). Trivial typos bundled into an active Issue do not need a separate Issue.

### 4.1 Create an Issue and add it to Project #2

```bash
# 1. Create the issue (returns URL)
gh issue create --repo vernikr/moex-spread-lab \
  --title "F7: hygiene leftovers" \
  --label "refactor,area:tooling" \
  --body-file /tmp/issue.md

# 2. Ensure all open issues are on Project #2 (or add a single item via gh project item-add)
./scripts/github_project_bootstrap.sh
```

### 4.2 Attach native parent / sub-issue hierarchy

Two supported mechanisms exist (both set GitHub's native `parent` / `subIssues` relation):

1. **Direct GraphQL (`addSubIssue`)** — preferred when `gh` has `repo` access:
   ```bash
   gh api graphql -H "GraphQL-Features: sub_issues" -f query='
   mutation($parent: ID!, $child: ID!) {
     addSubIssue(input: {issueId: $parent, subIssueId: $child}) {
       issue { number } subIssue { number }
     }
   }' -F parent="$EPIC_NODE_ID" -F child="$CHILD_NODE_ID"
   ```
2. **Body marker bridge (`.github/workflows/issue-parent-sync.yml`)** — for environments without GraphQL sub-issue mutation support:
   Place `<!-- issue-parent: #12 -->` on its own line in the child Issue body (or `<!-- issue-parent: none -->` to detach). The workflow sets the native parent and strips the marker automatically.

### 4.3 Wire execution order (`blockedBy` / `blocking`)

When Work Package B must not start before Work Package A finishes (for example, `#15` F2 after `#13` F6):

1. Wire the native GitHub dependency via GraphQL `addBlockedBy`:
   ```bash
   gh api graphql -f query='
   mutation($issue: ID!, $blocker: ID!) {
     addBlockedBy(input: {issueId: $issue, blockingIssueId: $blocker}) {
       issue { number } blockingIssue { number }
     }
   }' -F issue="$DOWNSTREAM_ID" -F blocker="$UPSTREAM_ID"
   ```
2. Add label `blocked`, set Project `Status` = `Blocked`, and leave a concise comment:
   ```markdown
   **Blocked by:** #13 (native dependency set). <1-line reason>.
   **Unblock when:** #13 closes as completed; the agent closing #13 removes `blocked` and moves this item to `Ready`.
   ```

---

## 5. Tracking execution and intermediate progress

1. **Claiming work**:
   - Pick an unblocked item in `Ready` (highest `Business Value` / `Effort` fit, disjoint `area:*` from any concurrent session).
   - Set Project `Status` to `In Progress` (and the parent workstream to `In Progress` if it was `Inbox`/`Ready`).
2. **During a turn (S3 cadence)**:
   - Execute inner sub-slices continuously with local Conventional Commits (`refactor(proof): ... (refs #13)`) and `--profile affected` / `--profile quick`. Do **not** spam GitHub comments after every 10-minute inner commit.
3. **Intermediate checkpoint (multi-turn / partial handoff)**:
   - If a Work Package spans multiple sessions (e.g., turn call-budget ~75% reached after pushing a self-contained green prefix, or stopping on a blocker), update the `## Slices` checkboxes in the Issue body and post **one structured Checkpoint comment**:
     ```markdown
     **Checkpoint (`<head_sha>`, YYYY-MM-DD):**
     - **Done:** slices landed and verified (`--profile full`, parity hashes / `run_id`s).
     - **Remaining:** unchecked slices / modules still open.
     - **Next step:** exact file or command for the next agent session.
     ```
   - Record detailed benchmark tables, parity hashes and dated verification evidence in the Issue body or a clearly titled Issue comment. Keep repository documentation only for durable contracts/runbooks; never create a project execution-report file as a mirror of an Issue.

---

## 6. Plan deviations, partial completion, deferrals, and blockers

Real codebase constraints frequently invalidate parts of a written plan (for example: discovery rules, typing scope, or a latency target that conflicts with required browser coverage). Handle every such case **explicitly and structurally**, never silently; keep current measurements and the actual decision in the corresponding Issue(s), such as the Phase 7 work under #12/#13 and the S4b trade-off under #21/#48.

### 6.1 Minor implementation deviation (invariants & goal preserved)

Examples: helper signature adjusted to satisfy Ruff `PLR0913` (`<= 7` args); module split boundary shifted to satisfy `test_architecture_dag.py`.

1. Proceed autonomously — do not stop to ask the owner.
2. Add label `plan-deviation` to the Issue.
3. Post a short comment on the Issue:
   ```markdown
   **Deviation from plan:** <what changed vs written plan>.
   **Why:** <measured constraint / gate rule>.
   **Invariant check:** `run_id` parity, equivalence tiers, and code-health ceilings preserved.
   ```
4. Update the canonical Issue body/comment in the same deliverable. Update affected durable repository references only; do not retain or recreate a tracked plan/report copy.

### 6.2 Material scope / contract / invariant conflict (owner decision required)

Examples: achieving a latency target requires dropping test scenarios; changing `run_id` hashing, benchmark/LQDT comparison, sizing, or Stage-9 gates (`G1`–`G12`).

1. **Stop** on the conflicting part (do not weaken any gate or relax `config/code-health-baseline.json` / `.basedpyright/baseline.json`).
2. Add labels `blocked` and `decision`, set Project `Status` = `Blocked`.
3. Post a `**Blocked by:** owner decision` comment presenting the measured evidence, concrete options with trade-offs, and a recommended default.

### 6.3 Partial completion when full execution is unreasonable right now

When part of a Work Package is complete, green, and valuable on `main`, while the remainder turns out to have disproportionate cost, high blast radius, or a new dependency:

1. **Never close the whole Issue as `completed` while silently skipping acceptance criteria**, and never leave half-done uncommitted code.
2. **Split cleanly**:
   - Narrow the current Issue's scope and acceptance criteria to the delivered, fully verified part (add `plan-deviation` explaining the split), pass `./scripts/verify.py --profile full`, push, and close it as `completed` with evidence.
   - Create a **new follow-up sub-issue** under the same parent workstream for the remaining scope.
3. Classify the remainder honestly:
   - If actionable later without risk: set `Status` = `Ready` (with updated `Effort` / `Business Value` as needed).
   - If intentionally postponed until a condition holds: apply the **Deferral protocol** below.

### 6.4 Deferring work (`Deferred`) vs retiring work (`not_planned`)

- **Defer (`Status` = `Deferred` + label `deferred`, Issue stays OPEN)**:
  Use when the idea remains valid conditionally (e.g., a namespace cleanup deferred until its dependent pipeline is ready, or a warm verification lane deferred until measurements show the cold lane dominates). Record the current trigger in the Issue. Leave a comment:
  ```markdown
  **Deferred:** <measured or architectural reason>.
  **Revisit when:** <concrete trigger condition>.
  ```
  Deferred items stay out of the active `Queue` view (`-status:Deferred`) while remaining visible in `Blocked & Deferred` and `All work` and tracked in the parent workstream sub-issue rollup.
- **Close as `not_planned` (`gh issue close N --reason "not planned"`, `Status` = `Done`)**:
  Use when measurements or an owner decision permanently retire a target (e.g., a speculative optimization proven net-negative). Post the measured rationale in the Issue before closing; do not create a repository plan/report copy.

---

## 7. Closing protocol and workstream reconciliation

Before closing an Issue as `completed`:

1. All acceptance criteria checkboxes are `[x]` (or explicitly split out per §6.3).
2. `./scripts/verify.py --profile full` (plus the Issue's `Verification Gate`: `Browser Smokes`, `Parity + Bench`, or `C99 Sanitizers`) passed on the exact integrated tree pushed to `origin/main`.
3. Update affected durable contracts, runbooks and references in `docs/` or `packages/*/docs/`; temporary execution evidence remains in GitHub Issues only.
4. Close the Issue with `Closes #N` in the final commit/PR or via `gh issue close N --reason completed --comment "..."` citing the pushed `main` SHA and gate summary; set Project `Status` = `Done`.
5. **Unblock downstream work**: check the Issue's `blocking` list (`gh api graphql ...`); for any downstream Issue whose blockers are now all closed, remove its `blocked` label and move its Project `Status` to `Ready`.
6. **Reconcile parent workstream**: when a workstream's sub-issues are all closed (`completed` or `not_planned`) or only `Deferred` items remain that the owner chose not to include in the current milestone, verify exit criteria and update the workstream body/comments (the canonical plan record). Then close the workstream (moving any still-open `Deferred` children to standalone or a next-phase workstream if appropriate, or keeping the workstream open in `Deferred` while deferred children remain).

---

## 8. Bootstrap and UI-only Project workflow settings

Run `./scripts/github_project_bootstrap.sh` (idempotent; requires `gh` with `repo` + `project` scopes) whenever recreating fields/views or bulk-syncing open Issues into Project #2.

GitHub's GraphQL API does not expose write mutations for built-in Project v2 automation workflows. Project #2 already enables **Item closed -> Done**, **Pull request merged -> Done**, **Auto-close issue**, and **Auto-add sub-issues to project** by default. When logged into the GitHub web UI (`https://github.com/orgs/vernikr/projects/2/workflows`), the owner can optionally toggle three UI-only conveniences:

1. **Auto-add to project**: repository `vernikr/moex-spread-lab`, filter `is:issue is:open` (so newly opened Issues enter `Inbox` without running the bootstrap script).
2. **Item added to project**: set `Status` -> `Inbox`.
3. **Item reopened**: set `Status` -> `Inbox`.

Agents must **not** assume UI auto-add is active: when creating an Issue via CLI/API, always add it to Project #2 and set its `Status`, `Work Kind`, `Module`, `Effort`, `Business Value`, and `Verification Gate` explicitly.
