#!/usr/bin/env bash
# Tier-1 repository gate (docs/development/agent-performance-2/plan.md, slice S1).
#
# Full mode (default): all seven checks, capped parallelism, quiet on pass — one
# line per check, no log bodies unless --verbose. On failure, prints the failing
# checks and a bounded log tail.
#
# Changed mode (`--changed [--base REV]`): scoped to touched files. Prints at most
# three lines on success and states which full checks were skipped.
#
# Exit status is authoritative: 0 = all run checks passed; 1 = a check failed or
# timed out; 2 = a check could not run (unavailable) and none failed. Exit 2 and 127
# both mean "could not run". A check that did not run is never reported as passed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

usage() {
    cat <<'EOF'
usage: check_health.sh [--verbose|-v] [--changed [--base REV]] [--timeout SECONDS]

  --verbose       cat every check's log, not only failing ones
  --changed       only check touched files (ruff, format, basedpyright and the
                  LOC/token, complexity and broad-exception ratchets on touched
                  files; js-modules only when static/** changed; architecture
                  --fast only when imports changed or files were added/moved).
                  Prints the full checks it skipped.
  --base REV      base revision for --changed (default: HEAD; use origin/main to
                  cover a whole branch)
  --timeout SECS  per-check timeout (default: ${MSL_HEALTH_TIMEOUT:-900})

Set MSL_HEALTH_STATUS_FILE to receive a machine-readable per-check status JSON
(consumed by scripts/verify_record.py).
EOF
}

VERBOSE=0
MODE=full
BASE=HEAD
TIMEOUT="${MSL_HEALTH_TIMEOUT:-900}"
while [ $# -gt 0 ]; do
    case "$1" in
        --verbose | -v) VERBOSE=1 ;;
        --changed) MODE=changed ;;
        --base) BASE="${2:?--base needs a revision}"; shift ;;
        --timeout) TIMEOUT="${2:?--timeout needs seconds}"; shift ;;
        -h | --help) usage; exit 0 ;;
        *) echo "check_health: unknown option: $1" >&2; usage >&2; exit 64 ;;
    esac
    shift
done

started=$SECONDS
logs="$(mktemp -d "${TMPDIR:-/tmp}/msl-health.XXXXXX")"
declare -a BG_PIDS=()
cleanup() {
    status=$?
    if [ "${#BG_PIDS[@]}" -gt 0 ]; then
        kill "${BG_PIDS[@]}" 2>/dev/null || true
        wait "${BG_PIDS[@]}" 2>/dev/null || true
    fi
    rm -rf "$logs"
    exit $status
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# Ensure venv tools (complexipy, lizard, ...) are resolvable by shutil.which().
if [ -d "$ROOT/.venv/bin" ]; then PATH="$ROOT/.venv/bin:$PATH"; export PATH; fi

NCPUS="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 2)"
case "$NCPUS" in (*[!0-9]*|"") NCPUS=2 ;; esac
[ "$NCPUS" -lt 1 ] && NCPUS=1

if [ -x "$ROOT/.venv/bin/python" ]; then PY_BIN="$ROOT/.venv/bin/python"; PY_USE_VENV=1; else PY_BIN="uv run --no-sync python"; PY_USE_VENV=0; fi
if [ -x "$ROOT/.venv/bin/ruff" ]; then RUFF_BIN="$ROOT/.venv/bin/ruff"; RUFF_USE_VENV=1; else RUFF_BIN="uv run --no-sync ruff"; RUFF_USE_VENV=0; fi
if [ -x "$ROOT/.venv/bin/basedpyright" ]; then PYRIGHT_BIN="$ROOT/.venv/bin/basedpyright"; PYRIGHT_USE_VENV=1; else PYRIGHT_BIN="uv run --no-sync basedpyright"; PYRIGHT_USE_VENV=0; fi
if [ -x "$ROOT/.venv/bin/vulture" ]; then VULTURE_BIN="$ROOT/.venv/bin/vulture"; VULTURE_USE_VENV=1; else VULTURE_BIN="uv run --no-sync vulture"; VULTURE_USE_VENV=0; fi

# --- touched files (changed mode only) ---------------------------------------
ALL_FILES=()
PY_FILES=()
HAS_STATIC_JS=0
IMPORTS_CHANGED=0
if [ "$MODE" = changed ]; then
    if ! ALL_OUT=$($PY_BIN scripts/changed_files.py --base "$BASE"); then
        echo "check_health: changed-files detection failed (base $BASE)" >&2
        exit 2
    fi
    if ! PY_OUT=$($PY_BIN scripts/changed_files.py --base "$BASE" --ext .py); then
        echo "check_health: changed-files detection failed (base $BASE)" >&2
        exit 2
    fi
    while IFS= read -r line; do [ -n "$line" ] && ALL_FILES+=("$line"); done <<< "$ALL_OUT"
    while IFS= read -r line; do [ -n "$line" ] && PY_FILES+=("$line"); done <<< "$PY_OUT"
    for path in ${ALL_FILES[@]+"${ALL_FILES[@]}"}; do
        case "$path" in
            packages/web/src/*/static/js/*.js) HAS_STATIC_JS=1 ;;
        esac
    done
    imports_rc=0
    $PY_BIN scripts/changed_files.py --base "$BASE" --imports-changed >/dev/null || imports_rc=$?
    if [ "$imports_rc" -eq 0 ]; then
        IMPORTS_CHANGED=1
    elif [ "$imports_rc" -ne 1 ]; then
        echo "check_health: import-change detection failed (base $BASE)" >&2
        exit 2
    fi
    if [ "${#ALL_FILES[@]}" -eq 0 ]; then
        printf 'check_health (changed): no touched files (base %s)\n' "$BASE"
        if [ -n "${MSL_HEALTH_STATUS_FILE:-}" ]; then
            printf '{"mode": "changed", "checks": {}}\n' >"$MSL_HEALTH_STATUS_FILE" || true
        fi
        exit 0
    fi
fi

# --- check commands ----------------------------------------------------------
# rc 77 means "skipped"; every command writes to $logs/<name>.log.
# This function is also exported to `bash -c` children when a timeout wrapper is
# used, so it must only rely on exported scalars: MODE, BASE, ROOT, HAS_STATIC_JS,
# IMPORTS_CHANGED, PY_BIN, RUFF_BIN, PYRIGHT_BIN, VULTURE_BIN, PY_LIST (newline
# separated), and on tool fallbacks via `uv run --no-sync` when the venv lacks a tool.
run_check_command() {
    local line
    local -a py_files=()
    while IFS= read -r line; do [ -n "$line" ] && py_files+=("$line"); done <<< "$PY_LIST"
    case "$1" in
        ruff-check)
            if [ "$MODE" = changed ]; then
                [ "${#py_files[@]}" -eq 0 ] && return 77
                if [ "$RUFF_USE_VENV" = 1 ]; then "$RUFF_BIN" check "${py_files[@]}"; else uv run --no-sync ruff check "${py_files[@]}"; fi
            else
                if [ "$RUFF_USE_VENV" = 1 ]; then "$RUFF_BIN" check packages src tests scripts; else uv run --no-sync ruff check packages src tests scripts; fi
            fi ;;
        ruff-format)
            if [ "$MODE" = changed ]; then
                [ "${#py_files[@]}" -eq 0 ] && return 77
                if [ "$RUFF_USE_VENV" = 1 ]; then "$RUFF_BIN" format --check "${py_files[@]}"; else uv run --no-sync ruff format --check "${py_files[@]}"; fi
            else
                if [ "$RUFF_USE_VENV" = 1 ]; then "$RUFF_BIN" format --check packages src tests scripts; else uv run --no-sync ruff format --check packages src tests scripts; fi
            fi ;;
        basedpyright)
            if [ "$MODE" = changed ]; then
                [ "${#py_files[@]}" -eq 0 ] && return 77
                if [ "$PYRIGHT_USE_VENV" = 1 ]; then "$PYRIGHT_BIN" "${py_files[@]}"; else uv run --no-sync basedpyright "${py_files[@]}"; fi
            else
                if [ "$PYRIGHT_USE_VENV" = 1 ]; then "$PYRIGHT_BIN"; else uv run --no-sync basedpyright; fi
            fi ;;
        vulture)
            if [ "$MODE" = changed ]; then return 77; fi
            if [ "$VULTURE_USE_VENV" = 1 ]; then "$VULTURE_BIN" packages src scripts --min-confidence 80; else uv run --no-sync vulture packages src scripts --min-confidence 80; fi ;;
        custom-gate)
            # Runs even when no .py files are touched: it re-scopes itself to the
            # touched source suffixes (.c/.js/.css/.html) and passes trivially on none.
            if [ "$PY_USE_VENV" = 1 ]; then
                if [ "$MODE" = changed ]; then "$PY_BIN" scripts/code_health_gate.py --changed --base "$BASE"; else "$PY_BIN" scripts/code_health_gate.py; fi
            else
                if [ "$MODE" = changed ]; then uv run --no-sync python scripts/code_health_gate.py --changed --base "$BASE"; else uv run --no-sync python scripts/code_health_gate.py; fi
            fi ;;
        architecture)
            if [ "$MODE" = changed ] && [ "$IMPORTS_CHANGED" != 1 ]; then return 77; fi
            if [ "$PY_USE_VENV" = 1 ]; then
                if [ "$MODE" = changed ]; then "$PY_BIN" scripts/architecture_gate.py --fast; else "$PY_BIN" scripts/architecture_gate.py; fi
            else
                if [ "$MODE" = changed ]; then uv run --no-sync python scripts/architecture_gate.py --fast; else uv run --no-sync python scripts/architecture_gate.py; fi
            fi ;;
        js-modules)
            if [ "$MODE" = changed ] && [ "$HAS_STATIC_JS" != 1 ]; then return 77; fi
            if [ "$PY_USE_VENV" = 1 ]; then "$PY_BIN" scripts/check_js_modules.py; else uv run --no-sync python scripts/check_js_modules.py; fi ;;
        *) echo "unknown check: $1" >&2; return 127 ;;
    esac
}

# newline-separated lists consumed by run_check_command (arrays are not exportable)
PY_LIST=""
if [ "$MODE" = changed ] && [ "${#PY_FILES[@]}" -gt 0 ]; then
    printf -v PY_LIST '%s\n' "${PY_FILES[@]}"
fi

declare -a NAMES=() STATUS=() DUR=() SKIPPED=()

record_status() { # name status dur_s
    NAMES+=("$1"); STATUS+=("$2"); DUR+=("$3")
    [ "$2" = skipped ] && SKIPPED+=("$1")
    return 0
}

declare -a LAUNCH_NAME=() LAUNCH_PID=() LAUNCH_START=()

launch_bg() { # name
    local name=$1
    local start=$SECONDS
    # the child times itself: durations reflect the check, not the harvest order
    $PY_BIN scripts/with_timeout.py "$TIMEOUT" -- \
        bash -c 'start=$SECONDS; run_check_command "$1"; rc=$?; echo $((SECONDS-start)) > "$2"; exit $rc' \
        _ "$name" "$logs/$name.dur" >"$logs/$name.log" 2>&1 &
    local pid=$!
    BG_PIDS+=("$pid"); LAUNCH_NAME+=("$name"); LAUNCH_PID+=("$pid"); LAUNCH_START+=("$start")
}

collect_launched() {
    local i
    for i in "${!LAUNCH_PID[@]}"; do
        local name="${LAUNCH_NAME[$i]}" pid="${LAUNCH_PID[$i]}" start="${LAUNCH_START[$i]}"
        local rc=0
        wait "$pid" || rc=$?
        local dur=""
        [ -f "$logs/$name.dur" ] && dur=$(cat "$logs/$name.dur" 2>/dev/null || true)
        case "$dur" in (*[!0-9]*|"") dur=$((SECONDS - start)) ;; esac
        case "$rc" in
            0) record_status "$name" passed "$dur" ;;
            77) record_status "$name" skipped "$dur" ;;
            124 | 137 | 143) record_status "$name" timeout "$dur" ;;
            2 | 127) record_status "$name" unavailable "$dur" ;;
            *) record_status "$name" failed "$dur" ;;
        esac
    done
    LAUNCH_NAME=(); LAUNCH_PID=(); LAUNCH_START=(); BG_PIDS=()
}

launch_waves() { # task... — deterministic waves of at most $NCPUS concurrent checks
    local -a tasks=("$@")
    local i=0 n=${#tasks[@]}
    while [ "$i" -lt "$n" ]; do
        local count=0
        while [ "$i" -lt "$n" ] && [ "$count" -lt "$NCPUS" ]; do
            launch_bg "${tasks[$i]}"
            i=$((i + 1)); count=$((count + 1))
        done
        collect_launched
    done
}

run_tasks() { # task...
    local -a tasks=("$@")
    if [ "$MODE" = changed ]; then
        # Overlap the import-graph probe with basedpyright startup, and the
        # ruff/format spawn with the custom gate's analyzers; basedpyright and
        # the complexipy-backed gate never share a wave (deterministic too).
        local -a ordered=(basedpyright architecture custom-gate ruff-check ruff-format js-modules vulture)
        local -a picked=()
        local wanted
        for wanted in "${ordered[@]}"; do
            local t
            for t in "${tasks[@]}"; do [ "$t" = "$wanted" ] && picked+=("$t"); done
        done
        launch_waves "${picked[@]+"${picked[@]}"}"
        return
    fi
    # Full mode on small machines: keep the CPU-heavy checks contention-free —
    # each runs alone before the light checks fill the remaining slots.
    if [ "$NCPUS" -le 2 ]; then
        local heavy rest=()
        for heavy in basedpyright custom-gate; do
            local t
            for t in "${tasks[@]}"; do
                if [ "$t" = "$heavy" ]; then
                    launch_bg "$heavy"
                    collect_launched
                else
                    rest+=("$t")
                fi
            done
            tasks=("${rest[@]+"${rest[@]}"}")
            rest=()
        done
    fi
    launch_waves "${tasks[@]+"${tasks[@]}"}"
}

write_status_file() { # json-for-checks mode
    [ -z "${MSL_HEALTH_STATUS_FILE:-}" ] && return 0
    local checks_json="$1" mode="$2"
    printf '{"mode": "%s", "checks": {%s}}\n' "$mode" "$checks_json" >"$MSL_HEALTH_STATUS_FILE" || true
}

build_status_json() {
    local json="" i
    for i in "${!NAMES[@]}"; do
        json+=", \"${NAMES[$i]}\": \"${STATUS[$i]}\""
    done
    printf '%s' "${json#, }"
}

# run_check_command must be visible to the bash -c children spawned when a
# timeout wrapper is available; only scalars survive the boundary.
export -f run_check_command
export MODE BASE ROOT HAS_STATIC_JS IMPORTS_CHANGED
export PY_BIN RUFF_BIN PYRIGHT_BIN VULTURE_BIN
export PY_USE_VENV RUFF_USE_VENV PYRIGHT_USE_VENV VULTURE_USE_VENV
export PY_LIST

TASKS=(ruff-check ruff-format basedpyright vulture custom-gate architecture js-modules)
run_tasks "${TASKS[@]}"

failures=0
unavailable=0
skipped_list=""
for i in "${!NAMES[@]}"; do
    case "${STATUS[$i]}" in
        failed | timeout) failures=$((failures + 1)) ;;
        unavailable) unavailable=$((unavailable + 1)) ;;
        skipped) skipped_list+=", ${NAMES[$i]}" ;;
    esac
done

for i in "${!NAMES[@]}"; do
    name="${NAMES[$i]}"; status="${STATUS[$i]}"; dur="${DUR[$i]}"
    if [ "$VERBOSE" -eq 1 ]; then cat "$logs/$name.log"; fi
    case "$status" in
        passed)
            [ "$MODE" = full ] && printf '%s: passed (%ss)\n' "$name" "$dur" ;;
        timeout)
            printf '=== %s: TIMEOUT after %ss ===\n' "$name" "$TIMEOUT"
            tail -n 40 "$logs/$name.log" ;;
        unavailable)
            printf '=== %s: UNAVAILABLE (could not run) ===\n' "$name"
            tail -n 10 "$logs/$name.log" ;;
        failed)
            printf '=== %s: failed ===\n' "$name"
            tail -n 40 "$logs/$name.log" ;;
        skipped) : ;;
    esac
done

if [ "$MODE" = changed ]; then
    executed=""
    for i in "${!NAMES[@]}"; do
        [ "${STATUS[$i]}" != skipped ] && executed+=", ${NAMES[$i]}"
    done
    executed="${executed#, }"
    skipped_list="${skipped_list#, }"
    if [ "$failures" -eq 0 ] && [ "$unavailable" -eq 0 ]; then
        printf 'check_health (changed): passed in %ss (%s)\n' "$((SECONDS - started))" "$executed"
    fi
    printf 'skipped (full gate): %s\n' "${skipped_list:-none}"
    printf 'full verification: ./scripts/check_health.sh\n'
else
    if [ "$failures" -eq 0 ] && [ "$unavailable" -eq 0 ]; then
        printf 'check_health: passed in %ss\n' "$((SECONDS - started))"
    fi
fi

write_status_file "$(build_status_json)" "$MODE"

if [ "$failures" -gt 0 ]; then
    printf 'check_health: failed\n' >&2
    exit 1
fi
if [ "$unavailable" -gt 0 ]; then
    printf 'check_health: %s check(s) unavailable\n' "$unavailable" >&2
    exit 2
fi
exit 0
