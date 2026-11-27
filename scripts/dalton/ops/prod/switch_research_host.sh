#!/usr/bin/env bash
# Everflow Research Host cutover & rollback orchestrator (§6.4b).
#
# Current scope (s34-r2):
#   - Full CLI parsing & per-action flag validation for all §6.4b entrypoints
#   - Read-only `--check` and `--switch --dry-run` (no lock, no writes, zero
#     bytecode writes, exempt from incomplete-journal guard)
#   - `--approve-budgets [--handoff-deadline-s N] [--stage-b-window-m M]`
#     (rejects `--dry-run`; acquires .ops.lock; requires handoff-journal.json
#      present with `completed == true`; validates candidate via preflight.py
#      validate_timing_budgets_env(); atomic 0600 writeback without touching
#      handoff-journal.json)
#   - Unimplemented mutating entrypoints exit 2 BEFORE taking locks or writing files.
#
# Compatible with macOS Bash 3.2.

set -euo pipefail

export PYTHONDONTWRITEBYTECODE=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_research_host_handoff_lib.sh
source "${SCRIPT_DIR}/_research_host_handoff_lib.sh"

ACTION=""
DRY_RUN=0
KEEP_ADMISSION_PAUSED=0
TEST_WORKSPACE_ID=""
HANDOFF_DEADLINE_S=""
STAGE_B_WINDOW_M=""
REPORT_JSON=0

PLIST_DIR="${DALTON_LAUNCH_AGENTS_DIR:-}"
BACKEND_URL="${DALTON_BACKEND_URL:-http://127.0.0.1:8000}"
PROXY_URL="${DALTON_PROXY_URL:-http://127.0.0.1:8943}"
BRIDGE_URL="${DALTON_BRIDGE_LOCAL_URL:-http://127.0.0.1:8960}"
RESEARCH_HOST_URL="${DALTON_RESEARCH_HOST_LOCAL_URL:-http://127.0.0.1:8951}"
BUDGETS_FILE_OVERRIDE="${DALTON_TIMING_BUDGETS_FILE:-}"

usage() {
  cat <<'EOF'
Usage: switch_research_host.sh <action> [options]

Actions:
  --prepare-stage-a                     Stage A prep & budget measurement (pending s34-r2)
  --measure-budgets                     Standalone budget re-measurement (pending s34-r2)
  --approve-budgets                     Confirm measured timing budgets exceeding defaults
    [--handoff-deadline-s N]
    [--stage-b-window-m M]
  --check                               Read-only state summary + preflight.py --switch
  --switch                              Stage B cutover (only --dry-run enabled in this round)
    [--test-workspace-id <uuid>]
    [--keep-admission-paused]
    [--dry-run]
  --recover                             Recover incomplete operation from recovery-bin/ (pending s34-r2)
  --rollback                            Rollback to :8943 baseline (pending s34-r2)
    [--test-workspace-id <uuid>]
    [--keep-admission-paused]
    [--dry-run]

Optional read-only / test overrides:
  --plist-dir DIR
  --backend-url URL
  --proxy-url URL
  --bridge-url URL
  --research-host-url URL
  --budgets-file FILE
  --report-json
  -h, --help
EOF
}

set_action() {
  local next="$1"
  if [[ -n "${ACTION}" && "${ACTION}" != "${next}" ]]; then
    echo "ERROR: multiple actions specified (${ACTION} and ${next})" >&2
    exit 2
  fi
  ACTION="${next}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prepare-stage-a|--measure-budgets|--approve-budgets|--check|--switch|--recover|--rollback)
      set_action "$1"
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --keep-admission-paused)
      KEEP_ADMISSION_PAUSED=1
      shift
      ;;
    --test-workspace-id)
      TEST_WORKSPACE_ID="${2:?missing --test-workspace-id value}"
      shift 2
      ;;
    --test-workspace-id=*)
      TEST_WORKSPACE_ID="${1#--test-workspace-id=}"
      shift
      ;;
    --handoff-deadline-s)
      HANDOFF_DEADLINE_S="${2:?missing --handoff-deadline-s value}"
      shift 2
      ;;
    --handoff-deadline-s=*)
      HANDOFF_DEADLINE_S="${1#--handoff-deadline-s=}"
      shift
      ;;
    --stage-b-window-m)
      STAGE_B_WINDOW_M="${2:?missing --stage-b-window-m value}"
      shift 2
      ;;
    --stage-b-window-m=*)
      STAGE_B_WINDOW_M="${1#--stage-b-window-m=}"
      shift
      ;;
    --plist-dir)
      PLIST_DIR="${2:?missing --plist-dir value}"
      shift 2
      ;;
    --backend-url)
      BACKEND_URL="${2:?missing --backend-url value}"
      shift 2
      ;;
    --proxy-url)
      PROXY_URL="${2:?missing --proxy-url value}"
      shift 2
      ;;
    --bridge-url)
      BRIDGE_URL="${2:?missing --bridge-url value}"
      shift 2
      ;;
    --research-host-url)
      RESEARCH_HOST_URL="${2:?missing --research-host-url value}"
      shift 2
      ;;
    --budgets-file)
      BUDGETS_FILE_OVERRIDE="${2:?missing --budgets-file value}"
      shift 2
      ;;
    --report-json)
      REPORT_JSON=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "${ACTION}" ]]; then
  usage >&2
  exit 2
fi

# Per-action flag validation BEFORE acquiring locks or writing files
case "${ACTION}" in
  --approve-budgets)
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      echo "ERROR: --approve-budgets does not support --dry-run" >&2
      exit 2
    fi
    if [[ "${KEEP_ADMISSION_PAUSED}" -eq 1 || -n "${TEST_WORKSPACE_ID}" ]]; then
      echo "ERROR: --approve-budgets does not accept --keep-admission-paused or --test-workspace-id" >&2
      exit 2
    fi
    ;;
  --check)
    if [[ "${DRY_RUN}" -eq 1 || "${KEEP_ADMISSION_PAUSED}" -eq 1 || -n "${TEST_WORKSPACE_ID}" || -n "${HANDOFF_DEADLINE_S}" || -n "${STAGE_B_WINDOW_M}" ]]; then
      echo "ERROR: --check does not accept mutating or approval options" >&2
      exit 2
    fi
    ;;
  --switch)
    if [[ -n "${HANDOFF_DEADLINE_S}" || -n "${STAGE_B_WINDOW_M}" ]]; then
      echo "ERROR: --switch does not accept --handoff-deadline-s or --stage-b-window-m" >&2
      exit 2
    fi
    ;;
esac

# NOTE (s34-r2 placeholder):
# When invoked as `--recover` from the repository worktree, the next round will
# delegate execution to `${RH_RECOVERY_BIN_DIR}/switch_research_host.sh --recover`.

# Unimplemented mutating entrypoints MUST exit 2 BEFORE acquiring locks or writing files.
case "${ACTION}" in
  --prepare-stage-a|--measure-budgets|--recover|--rollback)
    echo "本版本尚未实现: ${ACTION}" >&2
    exit 2
    ;;
  --switch)
    if [[ "${DRY_RUN}" -ne 1 ]]; then
      echo "本版本尚未实现: --switch (正式执行将在下一轮实现；只读演练请加 --dry-run)" >&2
      exit 2
    fi
    ;;
esac

rh_paths

EFF_BUDGETS_FILE="${BUDGETS_FILE_OVERRIDE:-${RH_TIMING_BUDGETS_FILE}}"
EFF_PLIST_DIR="${PLIST_DIR:-${RH_HOME}/Library/LaunchAgents}"

run_preflight_switch() {
  local -a cmd=(
    "${RH_PYTHON_BIN}" -B "${RH_PREFLIGHT_PY}" --switch
    --repo "${RH_REPO_ROOT}"
    --home "${RH_HOME}"
    --plist-dir "${EFF_PLIST_DIR}"
    --backend-url "${BACKEND_URL}"
    --proxy-url "${PROXY_URL}"
    --bridge-url "${BRIDGE_URL}"
    --research-host-url "${RESEARCH_HOST_URL}"
    --budgets-file "${EFF_BUDGETS_FILE}"
  )
  if [[ "${REPORT_JSON}" -eq 1 ]]; then
    cmd+=(--report-json)
  fi
  PYTHONDONTWRITEBYTECODE=1 "${cmd[@]}"
}

case "${ACTION}" in
  --check)
    # Strictly read-only: no lock, no journal write, exempt from completed==false guard.
    PYTHONDONTWRITEBYTECODE=1 "${RH_PYTHON_BIN}" -B "${RH_PREFLIGHT_PY}" status-summary \
      --journal-file "${RH_JOURNAL_FILE}" \
      --admission-file "${RH_ADMISSION_FILE}" \
      --budgets-file "${EFF_BUDGETS_FILE}"
    echo ""
    run_preflight_switch
    exit $?
    ;;

  --switch)
    # DRY_RUN == 1 guaranteed here. Strictly read-only & exempt from completed==false guard.
    PYTHONDONTWRITEBYTECODE=1 "${RH_PYTHON_BIN}" -B "${RH_PREFLIGHT_PY}" status-summary \
      --journal-file "${RH_JOURNAL_FILE}" \
      --admission-file "${RH_ADMISSION_FILE}" \
      --budgets-file "${EFF_BUDGETS_FILE}" \
      --include-dry-run-plan
    if [[ -n "${TEST_WORKSPACE_ID}" ]]; then
      echo "  test_workspace_id:                    ${TEST_WORKSPACE_ID}"
    fi
    if [[ "${KEEP_ADMISSION_PAUSED}" -eq 1 ]]; then
      echo "  keep_admission_paused:                true"
    fi
    echo ""
    run_preflight_switch
    exit $?
    ;;

  --approve-budgets)
    trap rh_release_ops_lock EXIT
    trap 'rh_release_ops_lock; exit 130' INT
    trap 'rh_release_ops_lock; exit 143' TERM
    rh_acquire_ops_lock "switch_research_host:--approve-budgets" || exit 1
    rh_require_completed_journal_for_approval || exit 1
    cmd=(
      "${RH_PYTHON_BIN}" -B "${RH_PREFLIGHT_PY}" approve-budgets
      --budgets-file "${EFF_BUDGETS_FILE}"
      --journal-file "${RH_JOURNAL_FILE}"
      --lock-file "${RH_OPS_LOCK_FILE}"
      --lock-fd 8
    )
    if [[ -n "${HANDOFF_DEADLINE_S}" ]]; then
      cmd+=(--handoff-deadline-s "${HANDOFF_DEADLINE_S}")
    fi
    if [[ -n "${STAGE_B_WINDOW_M}" ]]; then
      cmd+=(--stage-b-window-m "${STAGE_B_WINDOW_M}")
    fi
    PYTHONDONTWRITEBYTECODE=1 "${cmd[@]}"
    exit $?
    ;;
esac
