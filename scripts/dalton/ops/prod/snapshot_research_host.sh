#!/usr/bin/env bash
# Atomic snapshot creator & verifier for Everflow Research Host downtime cutover.
#
# Saves all state required for one-click rollback (.env, LaunchAgent plists,
# service env files, Guidepoint OAuth tokens, handoff-journal.json, and
# web/releases/current pointer) into a temporary directory, fsyncs every file
# and directory, verifies SHA-256 hashes, atomically renames into place, and
# re-verifies the published manifest.
#
# Compatible with macOS Bash 3.2.

set -euo pipefail

export PYTHONDONTWRITEBYTECODE=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_research_host_handoff_lib.sh
source "${SCRIPT_DIR}/_research_host_handoff_lib.sh"

MODE="create"
VERIFY_DIR=""
OUT_DIR=""
LABEL=""
PRINT_PATH=0
OUT_JSON=0

PLIST_DIR=""
REPO_ENV=""
BRIDGE_ENV=""
RESEARCH_HOST_ENV=""
GUIDEPOINT_ENV=""
TOKENS_FILE=""
JOURNAL_FILE=""
WEB_RELEASES_CURRENT=""

usage() {
  cat <<'EOF'
Usage: snapshot_research_host.sh [options]

Default mode creates an atomic snapshot under:
  <repo>/var/dalton/cutover-snapshots/<YYYYMMDD-HHMMSSZ>[-<label>]/

Options:
  --label TAG                    Optional suffix tag (e.g. precheck, cutover)
  --out-dir DIR                  Write snapshot to an explicit target directory
  --verify DIR                   Verify SHA-256 manifest of an existing snapshot
  --latest                       Print path to the latest valid snapshot
  --print-path                   Print only the created snapshot directory path
  --json                         Print JSON output
  --plist-dir DIR                Override LaunchAgents plist directory
  --repo-env FILE                Override repository .env path
  --bridge-env FILE              Override researchdata-bridge.env path
  --research-host-env FILE       Override research-host.env path
  --guidepoint-env FILE          Override guidepoint_mcp.env path
  --tokens-file FILE             Override guidepoint_mcp_tokens.json path
  --journal-file FILE            Override handoff-journal.json path
  --web-releases-current PATH    Override web/releases/current path
  -h, --help                     Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label)
      LABEL="${2:?missing --label value}"
      shift 2
      ;;
    --label=*)
      LABEL="${1#--label=}"
      shift
      ;;
    --out-dir)
      OUT_DIR="${2:?missing --out-dir value}"
      shift 2
      ;;
    --out-dir=*)
      OUT_DIR="${1#--out-dir=}"
      shift
      ;;
    --verify)
      MODE="verify"
      VERIFY_DIR="${2:?missing --verify value}"
      shift 2
      ;;
    --verify=*)
      MODE="verify"
      VERIFY_DIR="${1#--verify=}"
      shift
      ;;
    --latest)
      MODE="latest"
      shift
      ;;
    --print-path)
      PRINT_PATH=1
      shift
      ;;
    --json)
      OUT_JSON=1
      shift
      ;;
    --plist-dir)
      PLIST_DIR="${2:?missing --plist-dir value}"
      shift 2
      ;;
    --repo-env)
      REPO_ENV="${2:?missing --repo-env value}"
      shift 2
      ;;
    --bridge-env)
      BRIDGE_ENV="${2:?missing --bridge-env value}"
      shift 2
      ;;
    --research-host-env)
      RESEARCH_HOST_ENV="${2:?missing --research-host-env value}"
      shift 2
      ;;
    --guidepoint-env)
      GUIDEPOINT_ENV="${2:?missing --guidepoint-env value}"
      shift 2
      ;;
    --tokens-file)
      TOKENS_FILE="${2:?missing --tokens-file value}"
      shift 2
      ;;
    --journal-file)
      JOURNAL_FILE="${2:?missing --journal-file value}"
      shift 2
      ;;
    --web-releases-current)
      WEB_RELEASES_CURRENT="${2:?missing --web-releases-current value}"
      shift 2
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

case "${MODE}" in
  verify)
    args=(--snapshot-dir "${VERIFY_DIR}")
    if [[ "${OUT_JSON}" -eq 1 ]]; then
      args+=(--json)
    fi
    rh_snapshot_verify "${args[@]}"
    ;;
  latest)
    rh_snapshot_latest
    ;;
  create)
    args=()
    if [[ -n "${LABEL}" ]]; then
      args+=(--label "${LABEL}")
    fi
    if [[ -n "${OUT_DIR}" ]]; then
      args+=(--out-dir "${OUT_DIR}")
    fi
    if [[ -n "${PLIST_DIR}" ]]; then
      args+=(--plist-dir "${PLIST_DIR}")
    fi
    if [[ -n "${REPO_ENV}" ]]; then
      args+=(--repo-env "${REPO_ENV}")
    fi
    if [[ -n "${BRIDGE_ENV}" ]]; then
      args+=(--bridge-env "${BRIDGE_ENV}")
    fi
    if [[ -n "${RESEARCH_HOST_ENV}" ]]; then
      args+=(--research-host-env "${RESEARCH_HOST_ENV}")
    fi
    if [[ -n "${GUIDEPOINT_ENV}" ]]; then
      args+=(--guidepoint-env "${GUIDEPOINT_ENV}")
    fi
    if [[ -n "${TOKENS_FILE}" ]]; then
      args+=(--tokens-file "${TOKENS_FILE}")
    fi
    if [[ -n "${JOURNAL_FILE}" ]]; then
      args+=(--journal-file "${JOURNAL_FILE}")
    fi
    if [[ -n "${WEB_RELEASES_CURRENT}" ]]; then
      args+=(--web-releases-current "${WEB_RELEASES_CURRENT}")
    fi
    if [[ "${PRINT_PATH}" -eq 1 ]]; then
      args+=(--print-path)
    fi
    if [[ "${OUT_JSON}" -eq 1 ]]; then
      args+=(--json)
    fi
    if [[ ${#args[@]} -gt 0 ]]; then
      rh_snapshot_create "${args[@]}"
    else
      rh_snapshot_create
    fi
    ;;
esac
