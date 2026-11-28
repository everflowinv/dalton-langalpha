#!/usr/bin/env bash
# One-click idempotent rollback script for Everflow Research Host cutover.
#
# Flow:
#   1. Close & verify host maintenance admission gate FIRST (file + DB lock
#      readback; falling back to emergency --no-db-lock write + readback if
#      Postgres is unavailable), then resolve & verify snapshot (including
#      target frontend release directory & version.json match before stopping
#      any service), verify running backend with SNAPSHOT .env (falling back
#      to current .env on HTTP 401), and run database idle check independently
#      of backend HTTP (skipped only with --skip-idle-wait)
#   2. Drain legacy bridge :8960 FIRST via /admin/drain (timeout >= 240s to
#      cover 180s ingest limit; falling back to verified snapshot admin token
#      on HTTP 401; failing before stopping any refresher service) until
#      inflight/queued/ingest_inflight == 0 & stop :8960, THEN drain
#      (controlled refresh if TTL <= 60s, /admin/drain, in_flight == 0,
#      no .guidepoint_mcp_tokens.*.tmp; --allow-down skips only when unloaded
#      from launchd and absent from ps) & stop com.dalton.research-host (:8951)
#   3. Drain & stop :8943, verify .guidepoint.owner.lock is free, then restore
#      snapshot files (preserving newer live Guidepoint tokens; requiring valid
#      web/releases/current symlink & version.json)
#   4. Start com.dalton.guidepoint-mcp-proxy (:8943) and verify owner_lock_held,
#      and start com.dalton.researchdata-bridge (:8960)
#   5. Resolve Compose args from restored .env, recreate backend container, and
#      verify health + closed gate
#   6. Probe live legacy Wiki `search` and Guidepoint `search_library` (JSON/SSE)
#      and verify served frontend /version.json matches restored snapshot
#   7. Open maintenance admission gate and verify :8000 accepts requests (on
#      failure, explicitly re-close — falling back to --no-db-lock if Postgres
#      is unavailable — and verify closure; error loudly if re-close fails)
#
# Any failure keeps the maintenance admission gate CLOSED and prints manual
# remediation instructions. Re-running the script is safe and idempotent.
#
# Compatible with macOS Bash 3.2.

set -euo pipefail

export PYTHONDONTWRITEBYTECODE=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_research_host_handoff_lib.sh
source "${SCRIPT_DIR}/_research_host_handoff_lib.sh"

SNAPSHOT_DIR=""
DRY_RUN=0
KEEP_ADMISSION_PAUSED=0
SKIP_IDLE_WAIT=0
IDLE_WAIT_SECONDS="${DALTON_ROLLBACK_IDLE_WAIT_S:-120}"
SERVICE_TIMEOUT="${DALTON_ROLLBACK_SERVICE_TIMEOUT_S:-30}"
BRIDGE_DRAIN_TIMEOUT="${DALTON_ROLLBACK_BRIDGE_DRAIN_TIMEOUT_S:-240}"
GATE_VERIFY_TIMEOUT="${DALTON_ROLLBACK_GATE_VERIFY_TIMEOUT_S:-30}"
PROBE_TIMEOUT="${DALTON_ROLLBACK_PROBE_TIMEOUT_S:-15}"
PROBE_RETRIES="${DALTON_ROLLBACK_PROBE_RETRIES:-3}"
PROBE_RETRY_DELAY="${DALTON_ROLLBACK_PROBE_RETRY_DELAY_S:-1}"
BACKEND_URL="${DALTON_BACKEND_URL:-http://127.0.0.1:8000}"
EDGE_URL="${DALTON_EDGE_URL:-http://127.0.0.1:${DALTON_EDGE_PORT:-8088}}"
PROXY_URL="${DALTON_PROXY_URL:-http://127.0.0.1:8943}"
BRIDGE_URL="${DALTON_BRIDGE_LOCAL_URL:-http://127.0.0.1:8960}"
RESEARCH_HOST_URL="${DALTON_RESEARCH_HOST_LOCAL_URL:-http://127.0.0.1:8951}"
CONTAINER_NAME="${DALTON_BACKEND_CONTAINER:-dalton-langalpha-backend-1}"
PLIST_DIR_OVERRIDE="${DALTON_LAUNCH_AGENTS_DIR:-}"
NO_DB_LOCK=0

usage() {
  cat <<'EOF'
Usage: rollback_research_host.sh [snapshot_dir] [options]

Restores the Everflow host research stack back to the legacy :8943 + :8960
baseline from a verified cutover snapshot. Defaults to the latest valid
snapshot under <repo>/var/dalton/cutover-snapshots/.

Options:
  --snapshot-dir DIR             Explicit snapshot directory to restore
  --dry-run                      Verify snapshot and print planned steps only (no changes)
  --keep-admission-paused        Keep maintenance admission gate closed after rollback
  --skip-idle-wait               Skip wait_backend_idle.sh check (for emergency recovery)
  --idle-wait-seconds N          Max seconds to wait for backend idle (default: 120)
  --service-timeout N            Max seconds to wait for service start/stop (default: 30)
  --bridge-drain-timeout N       Max seconds to wait for :8960 /admin/drain (default: 240; covers 180s ingest limit)
  --gate-verify-timeout N        Max seconds to wait for container gate check (default: 30)
  --probe-timeout N              HTTP timeout per live probe call in seconds (default: 15)
  --probe-retries N              Retry count per live probe (default: 3)
  --backend-url URL              Backend URL (default: http://127.0.0.1:8000)
  --edge-url URL                 Frontend edge URL for /version.json check (default: http://127.0.0.1:8088)
  --proxy-url URL                Legacy Guidepoint proxy URL (default: http://127.0.0.1:8943)
  --bridge-url URL               Legacy bridge URL (default: http://127.0.0.1:8960)
  --research-host-url URL        Research Host URL (default: http://127.0.0.1:8951)
  --container-name NAME          Backend container name (default: dalton-langalpha-backend-1)
  --plist-dir DIR                Override LaunchAgents plist directory
  --no-db-lock                   Write maintenance-admission.json without PG advisory lock
  -h, --help                     Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --snapshot-dir)
      SNAPSHOT_DIR="${2:?missing --snapshot-dir value}"
      shift 2
      ;;
    --snapshot-dir=*)
      SNAPSHOT_DIR="${1#--snapshot-dir=}"
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
    --skip-idle-wait)
      SKIP_IDLE_WAIT=1
      shift
      ;;
    --idle-wait-seconds)
      IDLE_WAIT_SECONDS="${2:?missing --idle-wait-seconds value}"
      shift 2
      ;;
    --service-timeout)
      SERVICE_TIMEOUT="${2:?missing --service-timeout value}"
      shift 2
      ;;
    --bridge-drain-timeout)
      BRIDGE_DRAIN_TIMEOUT="${2:?missing --bridge-drain-timeout value}"
      shift 2
      ;;
    --gate-verify-timeout)
      GATE_VERIFY_TIMEOUT="${2:?missing --gate-verify-timeout value}"
      shift 2
      ;;
    --probe-timeout)
      PROBE_TIMEOUT="${2:?missing --probe-timeout value}"
      shift 2
      ;;
    --probe-retries)
      PROBE_RETRIES="${2:?missing --probe-retries value}"
      shift 2
      ;;
    --backend-url)
      BACKEND_URL="${2:?missing --backend-url value}"
      shift 2
      ;;
    --edge-url)
      EDGE_URL="${2:?missing --edge-url value}"
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
    --container-name)
      CONTAINER_NAME="${2:?missing --container-name value}"
      shift 2
      ;;
    --plist-dir)
      PLIST_DIR_OVERRIDE="${2:?missing --plist-dir value}"
      shift 2
      ;;
    --no-db-lock)
      NO_DB_LOCK=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [[ -n "${SNAPSHOT_DIR}" ]]; then
        echo "ERROR: multiple snapshot directories specified (${SNAPSHOT_DIR} and $1)" >&2
        exit 2
      fi
      SNAPSHOT_DIR="$1"
      shift
      ;;
  esac
done

rh_paths

DOCKER_BIN_EFF="${DOCKER_BIN:-docker}"
CURL_BIN_EFF="${CURL_BIN:-curl}"
WAIT_IDLE_SH="${SCRIPT_DIR}/wait_backend_idle.sh"
if [[ ! -f "${WAIT_IDLE_SH}" ]]; then
  WAIT_IDLE_SH="${RH_REPO_ROOT}/scripts/dalton/ops/prod/wait_backend_idle.sh"
fi

ROLLBACK_RERUN_SCRIPT="scripts/dalton/ops/prod/rollback_research_host.sh"
if [[ "${SCRIPT_DIR}" == *"recovery-bin"* || "${SCRIPT_DIR}" == *".recovery-bin.rel."* ]]; then
  ROLLBACK_RERUN_SCRIPT="var/dalton/cutover-snapshots/recovery-bin/rollback_research_host.sh"
fi

fail_with_gate_closed() {
  local step="$1"
  local hint="$2"
  echo "" >&2
  echo "================================================================" >&2
  echo "ROLLBACK STOPPED AT STEP: ${step}" >&2
  echo "MAINTENANCE ADMISSION GATE REMAINS CLOSED (${RH_ADMISSION_FILE})" >&2
  echo "Manual guidance: ${hint}" >&2
  echo "Re-run after resolving the issue:" >&2
  echo "  bash ${ROLLBACK_RERUN_SCRIPT} ${SNAPSHOT_DIR}" >&2
  echo "================================================================" >&2
  exit 1
}

fail_gate_state_unknown() {
  local step="$1"
  local hint="$2"
  echo "" >&2
  echo "================================================================" >&2
  echo "ROLLBACK STOPPED AT STEP: ${step}" >&2
  echo "CRITICAL: MAINTENANCE ADMISSION GATE COULD NOT BE VERIFIED CLOSED (${RH_ADMISSION_FILE})" >&2
  echo "Manual guidance: ${hint}" >&2
  echo "Close the gate explicitly and re-run after resolving the issue:" >&2
  echo "  bash ${ROLLBACK_RERUN_SCRIPT} ${SNAPSHOT_DIR}" >&2
  echo "================================================================" >&2
  exit 1
}

read_manifest_field() {
  local key="$1"
  PYTHONDONTWRITEBYTECODE=1 "${RH_PYTHON_BIN}" -B - "${SNAPSHOT_DIR}/snapshot-manifest.json" "${key}" <<'PY'
import json
import sys

m = json.loads(open(sys.argv[1], encoding="utf-8").read())
k = sys.argv[2]
if k == "plist_dir":
    print(m.get("plist_dir", ""))
else:
    print((m.get("files", {}).get(k, {}) or {}).get("source_path", ""))
PY
}

load_snapshot_manifest_paths() {
  SNAP_PLIST_DIR="$(read_manifest_field "plist_dir")"
  EFF_PLIST_DIR="${PLIST_DIR_OVERRIDE:-${SNAP_PLIST_DIR:-${RH_HOME}/Library/LaunchAgents}}"
  PROXY_PLIST="$(read_manifest_field "plist_proxy")"
  BRIDGE_PLIST="$(read_manifest_field "plist_bridge")"
  RH_PLIST="$(read_manifest_field "plist_research_host")"
  REPO_ENV_PATH="$(read_manifest_field "repo_env")"
  BRIDGE_ENV_PATH="$(read_manifest_field "bridge_env")"
  RH_ENV_PATH="$(read_manifest_field "research_host_env")"
  GP_ENV_PATH="$(read_manifest_field "guidepoint_env")"
  TOKENS_PATH="$(read_manifest_field "guidepoint_tokens")"

  if [[ -z "${PROXY_PLIST}" ]]; then PROXY_PLIST="${EFF_PLIST_DIR}/com.dalton.guidepoint-mcp-proxy.plist"; fi
  if [[ -z "${BRIDGE_PLIST}" ]]; then BRIDGE_PLIST="${EFF_PLIST_DIR}/com.dalton.researchdata-bridge.plist"; fi
  if [[ -z "${RH_PLIST}" ]]; then RH_PLIST="${EFF_PLIST_DIR}/com.dalton.research-host.plist"; fi
  return 0
}

resolve_compose_args_array() {
  local env_override="${1:-}"
  COMPOSE_ARGS=()
  local compose_lines=""
  if [[ -n "${env_override}" && -f "${env_override}" ]]; then
    compose_lines="$(rh_compose_args --repo-env "${env_override}")" || return 1
  else
    compose_lines="$(rh_compose_args)" || return 1
  fi
  local line=""
  while IFS= read -r line; do
    if [[ -n "${line}" ]]; then
      COMPOSE_ARGS+=("${line}")
    fi
  done <<EOF
${compose_lines}
EOF
  return 0
}

is_gate_fully_closed() {
  local gate_json=""
  gate_json="$(rh_gate_status)" || return 1
  local closed_flag=""
  closed_flag="$(PYTHONDONTWRITEBYTECODE=1 "${RH_PYTHON_BIN}" -B - "${gate_json}" <<'PY'
import json
import sys

d = json.loads(sys.argv[1])
closed = (
    d.get("paused") is True
    and d.get("pause_host_direct") is True
    and not (d.get("allow_workspace_ids") or [])
)
print("1" if closed else "0")
PY
)" || return 1
  [[ "${closed_flag}" == "1" ]]
}

if [[ "${DRY_RUN}" -eq 1 ]]; then
  if [[ -z "${SNAPSHOT_DIR}" ]]; then
    if ! SNAPSHOT_DIR="$(rh_snapshot_latest)"; then
      echo "ERROR: could not find a valid latest snapshot under ${RH_SNAPSHOTS_ROOT}" >&2
      exit 1
    fi
  fi
  if ! rh_snapshot_verify --snapshot-dir "${SNAPSHOT_DIR}" >/dev/null; then
    echo "ERROR: snapshot integrity verification failed for ${SNAPSHOT_DIR}" >&2
    exit 1
  fi
  load_snapshot_manifest_paths
  dry_env="${SNAPSHOT_DIR}/files/repo.env"
  if [[ ! -f "${dry_env}" ]]; then
    dry_env="${REPO_ENV_PATH}"
  fi
  if ! resolve_compose_args_array "${dry_env}"; then
    echo "ERROR: failed to resolve Docker Compose arguments for ${RH_REPO_ROOT}" >&2
    exit 1
  fi
  echo "=== DRY-RUN: Research Host Rollback Plan ==="
  echo "Snapshot: ${SNAPSHOT_DIR} (SHA-256 verified)"
  echo "1. Close & verify host maintenance admission gate FIRST (${RH_ADMISSION_FILE}, with --no-db-lock fallback on DB failure), verify snapshot & frontend release target, verify running backend using snapshot .env (fallback to current .env on HTTP 401), and run database idle check via wait_backend_idle.sh"
  echo "2. Drain legacy bridge :8960 FIRST via /admin/drain (timeout ${BRIDGE_DRAIN_TIMEOUT}s, snapshot token fallback on 401, inflight=0/queued=0/ingest_inflight=0) & stop :8960, then drain com.dalton.research-host (${RESEARCH_HOST_URL}: controlled refresh if TTL<=60s, /admin/drain, in_flight=0, no .tmp) and stop via launchctl bootout"
  echo "3. Drain & stop :8943, verify .guidepoint.owner.lock is free, then restore files (preserving newer live Guidepoint tokens):"
  rh_snapshot_restore --snapshot-dir "${SNAPSHOT_DIR}" --dry-run
  echo "4. Start com.dalton.guidepoint-mcp-proxy (${PROXY_PLIST}, verify owner_lock_held) and com.dalton.researchdata-bridge (${BRIDGE_PLIST})"
  echo "5. Recreate backend container: ${DOCKER_BIN_EFF} compose ${COMPOSE_ARGS[*]} up -d --no-deps --force-recreate backend"
  echo "6. Probe legacy services: :8943/healthz, :8960/healthz, Wiki search, Guidepoint search_library (JSON/SSE), backend /health, and verify served frontend ${EDGE_URL}/version.json matches snapshot"
  if [[ "${KEEP_ADMISSION_PAUSED}" -eq 1 ]]; then
    echo "7. Keep maintenance admission gate closed (--keep-admission-paused)"
  else
    echo "7. Open maintenance admission gate and verify ${BACKEND_URL} admission probe returns 200"
  fi
  echo "=== DRY-RUN complete: zero files modified ==="
  exit 0
fi

# Acquire exclusive ops lock before any mutating step
trap rh_release_ops_lock EXIT
trap 'rh_release_ops_lock; exit 130' INT
trap 'rh_release_ops_lock; exit 143' TERM
rh_acquire_ops_lock "rollback_research_host" || exit 1

gate_set_extra=()
if [[ "${NO_DB_LOCK}" -eq 1 ]]; then
  gate_set_extra+=(--no-db-lock)
fi

run_gate_set() {
  if [[ ${#gate_set_extra[@]} -gt 0 ]]; then
    rh_gate_set "$@" "${gate_set_extra[@]}" >/dev/null
  else
    rh_gate_set "$@" >/dev/null
  fi
}

LAST_CLOSE_USED_NO_DB_LOCK=0

close_gate_with_emergency_fallback() {
  local reason="$1"
  LAST_CLOSE_USED_NO_DB_LOCK=0
  if ! run_gate_set --paused true --pause-host-direct true --operation rollback --reason "${reason}"; then
    if [[ "${NO_DB_LOCK}" -eq 0 ]]; then
      echo "  WARN: locked gate close failed (e.g. Postgres unavailable); falling back to emergency --no-db-lock write..." >&2
      if rh_gate_set --paused true --pause-host-direct true --operation rollback --reason "${reason}" --no-db-lock >/dev/null; then
        LAST_CLOSE_USED_NO_DB_LOCK=1
      else
        return 1
      fi
    else
      return 1
    fi
  fi
  is_gate_fully_closed || return 1
  return 0
}

# Step 1a: Close and verify host maintenance admission gate FIRST (DB lock + file readback,
# falling back to emergency --no-db-lock write + readback if Postgres is unavailable),
# BEFORE snapshot verification or Compose resolution.
echo "[1/7] Closing maintenance admission gate and verifying snapshot..."
if is_gate_fully_closed; then
  echo "  Admission gate file is already paused; confirming close state and readback..."
else
  echo "  Admission gate is open; closing gate before rollback..."
fi
if ! close_gate_with_emergency_fallback "manual_rollback_research_host"; then
  fail_gate_state_unknown "1/7 close-gate" "Could not write or verify closed maintenance-admission.json (both DB-locked write and emergency --no-db-lock fallback failed). Check disk permissions and close the gate manually."
fi
if [[ "${LAST_CLOSE_USED_NO_DB_LOCK}" -eq 1 ]]; then
  echo "  Admission gate closed via emergency --no-db-lock fallback and readback verified (paused=true, pause_host_direct=true)."
fi

# Step 1b: Resolve & verify snapshot directory (host gate is already closed & read back;
# verifies SHA-256 manifest AND frontend rollback target dir/version.json before stopping services)
if [[ -z "${SNAPSHOT_DIR}" ]]; then
  if ! SNAPSHOT_DIR="$(rh_snapshot_latest)"; then
    fail_with_gate_closed "1/7 resolve-snapshot" "Could not find a valid latest snapshot under ${RH_SNAPSHOTS_ROOT}. Pass an explicit verified snapshot directory."
  fi
fi

if ! rh_snapshot_verify --snapshot-dir "${SNAPSHOT_DIR}" >/dev/null; then
  fail_with_gate_closed "1/7 verify-snapshot" "Snapshot integrity or frontend release target verification failed for ${SNAPSHOT_DIR}. Inspect SHA-256 manifest and target web/releases directory or select another verified snapshot."
fi

load_snapshot_manifest_paths
SNAPSHOT_REPO_ENV="${SNAPSHOT_DIR}/files/repo.env"
LIVE_REPO_ENV="${REPO_ENV_PATH:-${RH_REPO_ROOT}/.env}"

# Step 1c: Verify still-running backend container sees closed gate:
# prefer SNAPSHOT .env token first (immune to corrupted live .env before recreate),
# and fall back to current live .env token on HTTP 401 (if backend was already recreated with a new token).
pre_verify_args=(
  --expected-paused true
  --backend-url "${BACKEND_URL}"
  --container-name "${CONTAINER_NAME}"
  --deadline-s "${GATE_VERIFY_TIMEOUT}"
  --allow-unavailable
)
if [[ -f "${SNAPSHOT_REPO_ENV}" ]]; then
  pre_verify_args+=(--repo-env "${SNAPSHOT_REPO_ENV}")
  if [[ -n "${LIVE_REPO_ENV}" && -f "${LIVE_REPO_ENV}" ]]; then
    pre_verify_args+=(--fallback-repo-env "${LIVE_REPO_ENV}")
  fi
elif [[ -n "${LIVE_REPO_ENV}" ]]; then
  pre_verify_args+=(--repo-env "${LIVE_REPO_ENV}")
fi
rh_gate_verify "${pre_verify_args[@]}" >/dev/null \
  || fail_gate_state_unknown "1/7 verify-gate" "Backend container is running but rejected closed-gate verification (tried snapshot .env and current .env on HTTP 401). Host gate file remains closed (paused=true); inspect ${CONTAINER_NAME} logs, INTERNAL_SERVICE_TOKEN, and /app/var/dalton/maintenance-admission.json."

gate_verify_extra=()
if [[ -n "${REPO_ENV_PATH}" ]]; then
  gate_verify_extra+=(--repo-env "${REPO_ENV_PATH}")
fi

run_gate_verify() {
  if [[ ${#gate_verify_extra[@]} -gt 0 ]]; then
    rh_gate_verify "$@" "${gate_verify_extra[@]}"
  else
    rh_gate_verify "$@"
  fi
}

# Step 1d: Run database idle wait independently of backend HTTP unless --skip-idle-wait is passed
if [[ "${SKIP_IDLE_WAIT}" -eq 0 ]]; then
  echo "  Waiting for active database threads and data tasks to become idle (max ${IDLE_WAIT_SECONDS}s)..."
  PYTHON_BIN="${RH_PYTHON_BIN}" DOCKER_BIN="${DOCKER_BIN_EFF}" CURL_BIN="${CURL_BIN_EFF}" \
    bash "${WAIT_IDLE_SH}" --max-wait-seconds "${IDLE_WAIT_SECONDS}" \
    || fail_with_gate_closed "1/7 wait-backend-idle" "Database still has active work (or Postgres is unreachable) after ${IDLE_WAIT_SECONDS}s. Wait for running jobs to finish or re-run with --skip-idle-wait during emergency downtime."
else
  echo "  Skipping database idle wait (--skip-idle-wait)."
fi

http_status_code() {
  local url="$1"
  local code=""
  code="$("${CURL_BIN_EFF}" -sS --max-time 3 -o /dev/null -w '%{http_code}' "${url}" 2>/dev/null)" || true
  if [[ -z "${code}" ]]; then
    code="000"
  fi
  printf '%s\n' "${code}"
}

wait_url_down() {
  local url="$1"
  local timeout_s="$2"
  local elapsed=0
  while [[ "${elapsed}" -lt "${timeout_s}" ]]; do
    local code
    code="$(http_status_code "${url}")"
    if [[ "${code}" == "000" ]]; then
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  return 1
}

drain_common_args=()
if [[ -n "${EFF_PLIST_DIR}" ]]; then drain_common_args+=(--plist-dir "${EFF_PLIST_DIR}"); fi
if [[ -n "${REPO_ENV_PATH}" ]]; then drain_common_args+=(--repo-env "${REPO_ENV_PATH}"); fi
if [[ -n "${BRIDGE_ENV_PATH}" ]]; then drain_common_args+=(--bridge-env "${BRIDGE_ENV_PATH}"); fi
if [[ -n "${RH_ENV_PATH}" ]]; then drain_common_args+=(--research-host-env "${RH_ENV_PATH}"); fi
if [[ -n "${GP_ENV_PATH}" ]]; then drain_common_args+=(--guidepoint-env "${GP_ENV_PATH}"); fi
if [[ -n "${TOKENS_PATH}" ]]; then drain_common_args+=(--tokens-file "${TOKENS_PATH}"); fi

echo "[2/7] Draining and stopping legacy bridge (:8960) FIRST, then com.dalton.research-host (${RESEARCH_HOST_URL})..."
bridge_drain_args=()
if [[ -n "${EFF_PLIST_DIR}" ]]; then bridge_drain_args+=(--plist-dir "${EFF_PLIST_DIR}"); fi
if [[ -n "${REPO_ENV_PATH}" ]]; then bridge_drain_args+=(--repo-env "${REPO_ENV_PATH}"); fi
if [[ -n "${BRIDGE_ENV_PATH}" ]]; then bridge_drain_args+=(--bridge-env "${BRIDGE_ENV_PATH}"); fi
if [[ -n "${SNAPSHOT_DIR}" ]]; then bridge_drain_args+=(--snapshot-dir "${SNAPSHOT_DIR}"); fi

rh_drain_bridge \
  --url "${BRIDGE_URL}" \
  --timeout "${BRIDGE_DRAIN_TIMEOUT}" \
  --allow-down \
  "${bridge_drain_args[@]}" >/dev/null \
  || fail_with_gate_closed "2/7 drain-legacy-bridge" "com.dalton.researchdata-bridge did not drain via /admin/drain (draining=true, inflight=0, queued=0, ingest_inflight=0) within ${BRIDGE_DRAIN_TIMEOUT}s. No refresher service (:8951/:8943) was stopped. 入库未结束时先等待再重跑 (if ingest_inflight > 0, wait for ingest to finish, inspect ${BRIDGE_URL}/admin/drain and ${BRIDGE_URL}/healthz, then re-run rollback)."

rh_launchctl_bootout "com.dalton.researchdata-bridge" "${BRIDGE_PLIST}" \
  || fail_with_gate_closed "2/7 stop-legacy-bridge" "Failed to stop com.dalton.researchdata-bridge before stopping refreshers."
wait_url_down "${BRIDGE_URL%/}/healthz" "${SERVICE_TIMEOUT}" \
  || fail_with_gate_closed "2/7 wait-legacy-bridge-down" "${BRIDGE_URL}/healthz is still responding after ${SERVICE_TIMEOUT}s."

rh_drain_refresher \
  --target research-host \
  --url "${RESEARCH_HOST_URL}" \
  --timeout "${SERVICE_TIMEOUT}" \
  --allow-down \
  "${drain_common_args[@]}" >/dev/null \
  || fail_with_gate_closed "2/7 drain-research-host" "com.dalton.research-host did not drain (in_flight=0, no .guidepoint_mcp_tokens.*.tmp, or controlled token refresh failed). Inspect ${RESEARCH_HOST_URL}/healthz and logs before retrying."

rh_launchctl_bootout "com.dalton.research-host" "${RH_PLIST}" \
  || fail_with_gate_closed "2/7 stop-research-host" "Failed to stop com.dalton.research-host via launchctl bootout. Stop the process manually and confirm ${RESEARCH_HOST_URL}/healthz is unreachable."

if ! wait_url_down "${RESEARCH_HOST_URL%/}/healthz" "${SERVICE_TIMEOUT}"; then
  fail_with_gate_closed "2/7 wait-research-host-down" "${RESEARCH_HOST_URL}/healthz is still responding after ${SERVICE_TIMEOUT}s. Check launchctl list and stop any stray research-host process before re-running."
fi

echo "[3/7] Draining/stopping :8943, confirming lock release, and restoring snapshot files..."
rh_drain_refresher \
  --target legacy \
  --url "${PROXY_URL}" \
  --timeout "${SERVICE_TIMEOUT}" \
  --allow-down \
  "${drain_common_args[@]}" >/dev/null \
  || fail_with_gate_closed "3/7 drain-legacy-proxy" "com.dalton.guidepoint-mcp-proxy did not drain (in_flight=0, no .guidepoint_mcp_tokens.*.tmp, or controlled token refresh failed). Inspect ${PROXY_URL}/healthz before retrying."

rh_launchctl_bootout "com.dalton.guidepoint-mcp-proxy" "${PROXY_PLIST}" \
  || fail_with_gate_closed "3/7 stop-legacy-proxy" "Failed to stop com.dalton.guidepoint-mcp-proxy before file restore."
wait_url_down "${PROXY_URL%/}/healthz" "${SERVICE_TIMEOUT}" \
  || fail_with_gate_closed "3/7 wait-legacy-proxy-down" "${PROXY_URL}/healthz is still responding after ${SERVICE_TIMEOUT}s."

rh_wait_lock_free --timeout "${SERVICE_TIMEOUT}" "${drain_common_args[@]}" >/dev/null \
  || fail_with_gate_closed "3/7 wait-owner-lock-free" "Guidepoint .guidepoint.owner.lock is still held after stopping refreshers. Stop any lingering process holding the lock before retrying."

rh_snapshot_restore --snapshot-dir "${SNAPSHOT_DIR}" \
  || fail_with_gate_closed "3/7 restore-snapshot" "Failed to restore files or web/releases/current from ${SNAPSHOT_DIR}. Inspect permissions, disk space, and target frontend release directory, then re-run rollback."

echo "[4/7] Starting legacy Guidepoint proxy (:8943) and legacy bridge (:8960)..."
rh_launchctl_bootstrap "com.dalton.guidepoint-mcp-proxy" "${PROXY_PLIST}" \
  || fail_with_gate_closed "4/7 start-legacy-proxy" "Failed to bootstrap ${PROXY_PLIST}. Check plist syntax with plutil -lint and logs in ~/Library/Logs/dalton-guidepoint-mcp-proxy.err.log."
rh_launchctl_bootstrap "com.dalton.researchdata-bridge" "${BRIDGE_PLIST}" \
  || fail_with_gate_closed "4/7 start-legacy-bridge" "Failed to bootstrap ${BRIDGE_PLIST}. Check plist syntax with plutil -lint and logs in ~/Library/Logs/dalton-researchdata-bridge.err.log."

rh_wait_owner --target legacy --url "${PROXY_URL}" --timeout "${SERVICE_TIMEOUT}" >/dev/null \
  || fail_with_gate_closed "4/7 wait-legacy-proxy" "Legacy Guidepoint proxy (${PROXY_URL}/healthz) did not report healthy + owner_lock_held=true within ${SERVICE_TIMEOUT}s."
rh_wait_health --url "${BRIDGE_URL%/}/healthz" --timeout "${SERVICE_TIMEOUT}" >/dev/null \
  || fail_with_gate_closed "4/7 wait-legacy-bridge" "Legacy bridge (${BRIDGE_URL}/healthz) did not report healthy HTTP 200 within ${SERVICE_TIMEOUT}s."

echo "[5/7] Resolving Compose args and recreating backend container..."
resolve_compose_args_array "${REPO_ENV_PATH}" \
  || fail_with_gate_closed "5/7 resolve-compose-args" "Failed to resolve Docker Compose arguments via preflight.py compose-args for ${RH_REPO_ROOT}. Inspect COMPOSE_FILE in ${REPO_ENV_PATH}."

(
  cd "${RH_REPO_ROOT}"
  "${DOCKER_BIN_EFF}" compose "${COMPOSE_ARGS[@]}" up -d --no-deps --force-recreate backend
) || fail_with_gate_closed "5/7 recreate-backend" "Docker Compose failed to recreate backend. Check docker compose logs backend and verify ${REPO_ENV_PATH}."

rh_wait_health --url "${BACKEND_URL%/}/health" --timeout "${SERVICE_TIMEOUT}" >/dev/null \
  || fail_with_gate_closed "5/7 wait-backend-health" "Backend (${BACKEND_URL}/health) did not report healthy HTTP 200 within ${SERVICE_TIMEOUT}s after recreate."

run_gate_verify --expected-paused true --backend-url "${BACKEND_URL}" --container-name "${CONTAINER_NAME}" --deadline-s "${GATE_VERIFY_TIMEOUT}" >/dev/null \
  || fail_with_gate_closed "5/7 verify-post-recreate-gate" "Backend came up but closed-gate verification failed. Confirm /app/var/dalton/maintenance-admission.json is mounted properly."

echo "[6/7] Running live probes (Wiki search & Guidepoint search_library)..."
probe_args=(
  --target legacy
  --backend-url "${BACKEND_URL}"
  --proxy-url "${PROXY_URL}"
  --bridge-url "${BRIDGE_URL}"
  --timeout "${PROBE_TIMEOUT}"
  --retries "${PROBE_RETRIES}"
  --retry-delay "${PROBE_RETRY_DELAY}"
)
if [[ -n "${REPO_ENV_PATH}" ]]; then
  probe_args+=(--repo-env "${REPO_ENV_PATH}")
fi
if [[ -n "${BRIDGE_ENV_PATH}" ]]; then
  probe_args+=(--bridge-env "${BRIDGE_ENV_PATH}")
fi
if [[ -n "${RH_ENV_PATH}" ]]; then
  probe_args+=(--research-host-env "${RH_ENV_PATH}")
fi

rh_probe_services "${probe_args[@]}" \
  || fail_with_gate_closed "6/7 live-probes" "Live Wiki search or Guidepoint search_library probe failed. Inspect :8943 and :8960 logs; admission gate remains closed."

fe_verify_args=(
  --edge-url "${EDGE_URL}"
  --snapshot-dir "${SNAPSHOT_DIR}"
  --timeout "${PROBE_TIMEOUT}"
)
if [[ -n "${REPO_ENV_PATH}" ]]; then
  fe_verify_args+=(--repo-env "${REPO_ENV_PATH}")
fi
rh_verify_frontend_version "${fe_verify_args[@]}" >/dev/null \
  || fail_with_gate_closed "6/7 verify-frontend-version" "Served frontend ${EDGE_URL}/version.json does not match restored snapshot release. Confirm edge container mounts web/releases/current before re-running rollback."

if [[ "${KEEP_ADMISSION_PAUSED}" -eq 1 ]]; then
  echo "[7/7] Rollback verified; keeping maintenance admission gate CLOSED as requested (--keep-admission-paused)."
  echo "ROLLBACK_OK (gate kept paused): restored from ${SNAPSHOT_DIR}"
  exit 0
fi

reclose_after_open_failure() {
  local fail_step="$1"
  local fail_msg="$2"
  local reclose_ok=1
  local used_no_db_lock_fallback=0
  if close_gate_with_emergency_fallback "rollback_open_verify_failed"; then
    used_no_db_lock_fallback="${LAST_CLOSE_USED_NO_DB_LOCK}"
  else
    reclose_ok=0
  fi
  if [[ "${reclose_ok}" -eq 1 ]]; then
    if ! run_gate_verify --expected-paused true --backend-url "${BACKEND_URL}" --container-name "${CONTAINER_NAME}" --deadline-s "${GATE_VERIFY_TIMEOUT}" --allow-unavailable >/dev/null; then
      reclose_ok=0
    fi
  fi

  if [[ "${reclose_ok}" -eq 1 ]]; then
    local suffix="gate was re-closed and verified closed for safety."
    if [[ "${used_no_db_lock_fallback}" -eq 1 ]]; then
      suffix="gate was re-closed via emergency --no-db-lock fallback and verified closed for safety."
    fi
    fail_with_gate_closed "${fail_step}" "${fail_msg}; ${suffix}"
  else
    fail_gate_state_unknown "7/7 reclose-gate" "${fail_msg}, AND re-closing or verifying closed admission gate FAILED! Immediately close the gate manually (or with --no-db-lock if Postgres is unavailable) and inspect ${RH_ADMISSION_FILE}."
  fi
}

echo "[7/7] Opening maintenance admission gate..."
if ! run_gate_set --paused false --pause-host-direct false --operation rollback --reason "rollback_verified"; then
  reclose_after_open_failure "7/7 open-gate" "Failed to open maintenance admission gate (disk state may have been updated before error)"
fi

if ! run_gate_verify --expected-paused false --backend-url "${BACKEND_URL}" --container-name "${CONTAINER_NAME}" --deadline-s "${GATE_VERIFY_TIMEOUT}" >/dev/null; then
  reclose_after_open_failure "7/7 verify-gate-open" "Backend did not confirm open admission gate (HTTP 200)"
fi

echo "ROLLBACK_OK: restored legacy :8943 + :8960 baseline from ${SNAPSHOT_DIR} and reopened admission gate."
