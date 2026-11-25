#!/usr/bin/env bash
# Idempotently ensures the organization GitHub Project v2 `moex-spread-lab`
# exists in `vernikr`, links it to `vernikr/moex-spread-lab`, configures its
# Status / Work Kind / Module / Verification Gate fields, attaches org Issue fields
# (Effort, Business Value), creates the four standard views, and
# syncs all open repository issues onto the Project.
#
# Requires: `gh` authenticated with `project` and `repo` scopes (e.g. GH_TOKEN).
set -euo pipefail

ORG="${GITHUB_ORG:-vernikr}"
REPO="${GITHUB_REPO:-moex-spread-lab}"
PROJECT_TITLE="${PROJECT_TITLE:-moex-spread-lab}"

gql() {
  gh api graphql -H "GraphQL-Features: sub_issues,issue_types" "$@"
}

META="$(gql -f query='
query($org: String!, $repo: String!) {
  organization(login: $org) {
    id
    projectsV2(first: 30) { nodes { id number title url } }
    issueFields(first: 20) {
      nodes {
        ... on IssueFieldSingleSelect { id name }
        ... on IssueFieldNumber { id name }
      }
    }
  }
  repository(owner: $org, name: $repo) {
    id
    issues(first: 100, states: [OPEN]) { nodes { id number title } }
  }
}' -F org="$ORG" -F repo="$REPO")"

ORG_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["organization"]["id"])' <<<"$META")"
REPO_ID="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["repository"]["id"])' <<<"$META")"
PROJ_ID="$(python3 -c '
import json, sys
nodes = json.load(sys.stdin)["data"]["organization"]["projectsV2"]["nodes"]
print(next((n["id"] for n in nodes if n["title"] == sys.argv[1]), ""))
' "$PROJECT_TITLE" <<<"$META")"

if [[ -z "$PROJ_ID" ]]; then
  PROJ_ID="$(gql -f query='
  mutation($owner: ID!, $title: String!) {
    createProjectV2(input: {ownerId: $owner, title: $title}) {
      projectV2 { id }
    }
  }' -F owner="$ORG_ID" -F title="$PROJECT_TITLE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["createProjectV2"]["projectV2"]["id"])')"
  echo "Created Project $PROJECT_TITLE ($PROJ_ID)"
else
  echo "Found existing Project $PROJECT_TITLE ($PROJ_ID)"
fi

gql -f query='
mutation($proj: ID!, $repo: ID!) {
  linkProjectV2ToRepository(input: {projectId: $proj, repositoryId: $repo}) {
    repository { name }
  }
}' -F proj="$PROJ_ID" -F repo="$REPO_ID" >/dev/null || true

META_FILE="$(mktemp)"
trap 'rm -f "$META_FILE"' EXIT
printf '%s' "$META" >"$META_FILE"

python3 - "$PROJ_ID" "$META_FILE" <<'PY'
import json, subprocess, sys

proj_id = sys.argv[1]
with open(sys.argv[2], encoding="utf-8") as fh:
    meta = json.load(fh)["data"]

def gql(query, **variables):
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        cmd.extend(["-F", f"{k}={v}"])
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    res = json.loads(out)
    if "errors" in res:
        raise RuntimeError(res["errors"])
    return res["data"]

proj = gql("""
query($id: ID!) {
  node(id: $id) {
    ... on ProjectV2 {
      number url
      fields(first: 40) {
        nodes {
          ... on ProjectV2Field { id name }
          ... on ProjectV2SingleSelectField { id name options { id name } }
        }
      }
      views(first: 20) { nodes { id number name layout filter } }
      items(first: 100) { nodes { id content { ... on Issue { id number } } } }
    }
  }
}
""", id=proj_id)["node"]

fields = {f["name"]: f for f in proj["fields"]["nodes"] if f}
status_id = fields["Status"]["id"]

gql(f"""
mutation {{
  updateProjectV2Field(input: {{
    fieldId: "{status_id}",
    name: "Status",
    singleSelectOptions: [
      {{name: "Inbox", color: GRAY, description: "Untriaged or waiting for triage"}},
      {{name: "Ready", color: BLUE, description: "Scoped, unblocked, ready for an agent to pick up"}},
      {{name: "In Progress", color: YELLOW, description: "Actively worked on in an agent session or PR"}},
      {{name: "Blocked", color: RED, description: "Waiting on an upstream dependency, external action, or owner decision"}},
      {{name: "Deferred", color: ORANGE, description: "Intentionally postponed; hidden from active queue until revisit condition"}},
      {{name: "Done", color: GREEN, description: "Acceptance criteria and verification gates completed (or closed as not planned)"}}
    ]
  }}) {{ projectV2Field {{ ... on ProjectV2SingleSelectField {{ id }} }} }}
}}
""")

if "Work Kind" not in fields:
    gql("""
    mutation($projectId: ID!) { createProjectV2Field(input: {
        projectId: $projectId, dataType: SINGLE_SELECT, name: "Work Kind",
        singleSelectOptions: [
          {name: "feat", color: GREEN, description: "New capability or user-visible slice"},
          {name: "fix", color: YELLOW, description: "Bug fix, parity defect, or regression correction"},
          {name: "docs", color: GRAY, description: "Documentation, guides, or governance text"},
          {name: "refactor", color: BLUE, description: "Behavior-preserving restructuring or code health"},
          {name: "chore", color: ORANGE, description: "Maintenance, tooling, workflow, or process work"},
          {name: "perf", color: RED, description: "Code/runtime performance improvement"},
          {name: "test", color: PINK, description: "Tests, verification coverage, or test infrastructure"},
          {name: "research", color: PURPLE, description: "Research, audit, investigation, or evidence gathering"},
          {name: "plan", color: BLUE, description: "Planning, decomposition, scoping, or workstream design"},
          {name: "revert", color: GRAY, description: "Revert a previous change"}
        ]}) { projectV2Field { ... on ProjectV2FieldCommon { id name } } } }
    """, projectId=proj_id)
    print("  + created field 'Work Kind'")

if "Module" not in fields:
    gql("""
    mutation($projectId: ID!) { createProjectV2Field(input: {
        projectId: $projectId, dataType: SINGLE_SELECT, name: "Module",
        singleSelectOptions: [
          {name: "core", color: BLUE, description: "packages/core (msl-core)"},
          {name: "data", color: GREEN, description: "packages/data (msl-data)"},
          {name: "accounting", color: YELLOW, description: "packages/accounting (msl-accounting)"},
          {name: "strategies", color: ORANGE, description: "packages/strategies (msl-strategies)"},
          {name: "proof", color: PURPLE, description: "packages/proof (msl-proof)"},
          {name: "web", color: PINK, description: "packages/web (msl-web)"},
          {name: "Cross-module", color: GRAY, description: "Cross-cutting, tooling, docs, or multi-module work"}
        ]}) { projectV2Field { ... on ProjectV2FieldCommon { id name } } } }
    """, projectId=proj_id)
    print("  + created field 'Module'")

if "Verification Gate" not in fields:
    gql(f"""
    mutation {{
      createProjectV2Field(input: {{
        projectId: "{proj_id}", dataType: SINGLE_SELECT, name: "Verification Gate",
        singleSelectOptions: [
          {{name: "Docs / Quick", color: GRAY, description: "Docs scan + check_health.sh --changed"}},
          {{name: "Full Suite", color: BLUE, description: "verify.py --profile full (health + all package tests + parity tiers)"}},
          {{name: "Parity + Bench", color: PURPLE, description: "Full suite + run_id/hash parity + workstation real-data probe"}},
          {{name: "Browser Smokes", color: GREEN, description: "Full suite + run_browser_smokes.py (UI/routes touched)"}},
          {{name: "C99 Sanitizers", color: RED, description: "Full suite + clang-tidy + ASan/UBSan equivalence"}}
        ]
      }}) {{ projectV2Field {{ ... on ProjectV2SingleSelectField {{ id }} }} }}
    }}
    """)

org_fields = {f["name"]: f["id"] for f in meta["organization"]["issueFields"]["nodes"] if f}
for fname in ("Effort", "Business Value"):
    if fname in org_fields and fname not in fields:
        gql("""
        mutation($proj: ID!, $ifid: ID!) {
          createProjectV2IssueField(input: {projectId: $proj, issueFieldId: $ifid}) {
            projectV2Field { ... on ProjectV2Field { id } ... on ProjectV2SingleSelectField { id } }
          }
        }
        """, proj=proj_id, ifid=org_fields[fname])

existing_issues = {
    it["content"]["id"]
    for it in proj["items"]["nodes"]
    if it.get("content") and "id" in it["content"]
}
for issue in meta["repository"]["issues"]["nodes"]:
    if issue["id"] not in existing_issues:
        gql("""
        mutation($proj: ID!, $cid: ID!) {
          addProjectV2ItemById(input: {projectId: $proj, contentId: $cid}) { item { id } }
        }
        """, proj=proj_id, cid=issue["id"])

print(f"Synced Project #{proj['number']}: {proj['url']}")
PY
