#!/usr/bin/env bash
# Everflow Research Host read-only preflight check wrapper.
#
# Automated 7-step online cutover orchestration (--prepare-stage-a,
# --measure-budgets, --approve-budgets, --switch, --recover, --rollback) has
# been retired in favor of the manual downtime cutover runbook and dedicated
# snapshot / one-click rollback scripts:
#   - Runbook:  scripts/dalton/ops/prod/RESEARCH_HOST_SWITCH_RUNBOOK.md
#   - Snapshot: scripts/dalton/ops/prod/snapshot_research_host.sh
#   - Rollback: scripts/dalton/ops/prod/rollback_research_host.sh
#
# Compatible with macOS Bash 3.2.

set -euo pipefail

export PYTHONDONTWRITEBYTECODE=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_research_host_handoff_lib.sh
source "${SCRIPT_DIR}/_research_host_handoff_lib.sh"

ACTION=""
REPORT_JSON=0
SKIP_BUDGETS=0

PLIST_DIR="${DALTON_LAUNCH_AGENTS_DIR:-}"
RESEARCH_HOST_PLIST="${DALTON_RESEARCH_HOST_PLIST:-}"
ARCHIVED_PLIST_DIR="${DALTON_ARCHIVED_PLIST_DIR:-}"
BACKEND_URL="${DALTON_BACKEND_URL:-http://127.0.0.1:8000}"
PROXY_URL="${DALTON_PROXY_URL:-http://127.0.0.1:8943}"
BRIDGE_URL="${DALTON_BRIDGE_LOCAL_URL:-http://127.0.0.1:8960}"
RESEARCH_HOST_URL="${DALTON_RESEARCH_HOST_LOCAL_URL:-http://127.0.0.1:8951}"
BUDGETS_FILE_OVERRIDE="${DALTON_TIMING_BUDGETS_FILE:-}"

usage() {
  cat <<'EOF'
Usage: switch_research_host.sh --check [options]

Actions:
  --check                               Read-only state summary + preflight.py --switch

Manual downtime cutover tools (automated orchestration retired):
  Runbook:  scripts/dalton/ops/prod/RESEARCH_HOST_SWITCH_RUNBOOK.md
  Snapshot: scripts/dalton/ops/prod/snapshot_research_host.sh
  Rollback: scripts/dalton/ops/prod/rollback_research_host.sh

Optional read-only / test overrides:
  --skip-budgets                        Skip timing-budgets.env check (manual downtime cutover)
  --plist-dir DIR
  --research-host-plist FILE
  --archived-plist-dir DIR
  --backend-url URL
  --proxy-url URL
  --bridge-url URL
  --research-host-url URL
  --budgets-file FILE
  --report-json
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      ACTION="--check"
      shift
      ;;
    --prepare-stage-a|--measure-budgets|--approve-budgets|--switch|--recover|--rollback)
      echo "ERROR: automated cutover action '$1' has been retired. Follow scripts/dalton/ops/prod/RESEARCH_HOST_SWITCH_RUNBOOK.md and use snapshot_research_host.sh / rollback_research_host.sh instead." >&2
      exit 2
      ;;
    --skip-budgets)
      SKIP_BUDGETS=1
      shift
      ;;
    --plist-dir)
      PLIST_DIR="${2:?missing --plist-dir value}"
      shift 2
      ;;
    --research-host-plist)
      RESEARCH_HOST_PLIST="${2:?missing --research-host-plist value}"
      shift 2
      ;;
    --archived-plist-dir)
      ARCHIVED_PLIST_DIR="${2:?missing --archived-plist-dir value}"
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

if [[ "${ACTION}" != "--check" ]]; then
  usage >&2
  exit 2
fi

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
  if [[ "${SKIP_BUDGETS}" -eq 1 ]]; then
    cmd+=(--skip-budgets)
  fi
  if [[ -n "${RESEARCH_HOST_PLIST}" ]]; then
    cmd+=(--research-host-plist "${RESEARCH_HOST_PLIST}")
  fi
  if [[ -n "${ARCHIVED_PLIST_DIR}" ]]; then
    cmd+=(--archived-plist-dir "${ARCHIVED_PLIST_DIR}")
  fi
  if [[ "${REPORT_JSON}" -eq 1 ]]; then
    cmd+=(--report-json --include-summary)
  fi
  PYTHONDONTWRITEBYTECODE=1 "${cmd[@]}"
}

# Strictly read-only: no lock, no journal write, exempt from completed==false guard.
if [[ "${REPORT_JSON}" -eq 1 ]]; then
  run_preflight_switch
  exit $?
fi
PYTHONDONTWRITEBYTECODE=1 "${RH_PYTHON_BIN}" -B "${RH_PREFLIGHT_PY}" status-summary \
  --repo "${RH_REPO_ROOT}" \
  --budgets-file "${EFF_BUDGETS_FILE}"
echo ""
run_preflight_switch
exit $?
