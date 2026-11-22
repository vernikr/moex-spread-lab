#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
started=$SECONDS
logs="$(mktemp -d "${TMPDIR:-/tmp}/msl-health.XXXXXX")"
trap 'rm -rf "$logs"' EXIT

declare -a names=()
declare -a pids=()

run_check() {
    local name="$1"
    shift
    names+=("$name")
    "$@" >"$logs/$name.log" 2>&1 &
    pids+=("$!")
}

run_check ruff-check uv run --no-sync ruff check packages src tests scripts
run_check ruff-format uv run --no-sync ruff format --check packages src tests scripts
run_check basedpyright uv run --no-sync basedpyright
run_check vulture uv run --no-sync vulture packages src scripts --min-confidence 80
run_check custom-gate uv run --no-sync python scripts/code_health_gate.py
run_check architecture uv run --no-sync python scripts/architecture_gate.py

status=0
for index in "${!pids[@]}"; do
    name="${names[$index]}"
    if wait "${pids[$index]}"; then
        printf '=== %s: passed ===\n' "$name"
    else
        result=$?
        printf '=== %s: failed (exit %s) ===\n' "$name" "$result"
        status=1
    fi
    cat "$logs/$name.log"
done

if ((status != 0)); then
    printf 'check_health: failed\n' >&2
    exit 1
fi
printf 'check_health: passed in %ss\n' "$((SECONDS - started))"
