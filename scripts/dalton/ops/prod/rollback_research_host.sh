#!/usr/bin/env bash
# One-click idempotent rollback script for Everflow Research Host cutover.
#
# Flow:
#   0. Resolve & verify snapshot directory (defaults to latest valid snapshot)
#   1. Confirm maintenance admission gate is closed (close it if still open)
#      and drain active work via wait_backend_idle.sh if backend is running
#   2. Stop com.dalton.research-host (:8951) and verify it stopped listening
#   3. Stop :8943 and :8960, atomically restore .env, plists, tokens, journal,
#      and web/releases/current from the verified snapshot
#   4. Start com.dalton.guidepoint-mcp-proxy (:8943) and com.dalton.researchdata-bridge (:8960)
#   5. Recreate backend container via Docker Compose and verify health + closed gate
#   6. Probe live legacy Wiki `search` and Guidepoint `search_library`
#   7. Open maintenance admission gate and verify :8000 accepts requests
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
GATE_VERIFY_TIMEOUT="${DALTON_ROLLBACK_GATE_VERIFY_TIMEOUT_S:-30}"
PROBE_TIMEOUT="${DALTON_ROLLBACK_PROBE_TIMEOUT_S:-15}"
PROBE_RETRIES="${DALTON_ROLLBACK_PROBE_RETRIES:-3}"
PROBE_RETRY_DELAY="${DALTON_ROLLBACK_PROBE_RETRY_DELAY_S:-1}"
BACKEND_URL="${DALTON_BACKEND_URL:-http://127.0.0.1:8000}"
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
  --gate-verify-timeout N        Max seconds to wait for container gate check (default: 30)
  --probe-timeout N              HTTP timeout per live probe call in seconds (default: 15)
  --probe-retries N              Retry count per live probe (default: 3)
  --backend-url URL              Backend URL (default: http://127.0.0.1:8000)
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

fail_with_gate_closed() {
  local step="$1"
  local hint="$2"
  echo "" >&2
  echo "================================================================" >&2
  echo "ROLLBACK STOPPED AT STEP: ${step}" >&2
  echo "MAINTENANCE ADMISSION GATE REMAINS CLOSED (${RH_ADMISSION_FILE})" >&2
  echo "Manual guidance: ${hint}" >&2
  echo "Re-run after resolving the issue:" >&2
  echo "  bash scripts/dalton/ops/prod/rollback_research_host.sh ${SNAPSHOT_DIR}" >&2
  echo "================================================================" >&2
  exit 1
}

# Step 0: Resolve & verify snapshot directory
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

# Resolve target plist paths and env paths recorded in the verified snapshot manifest
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

SNAP_PLIST_DIR="$(read_manifest_field "plist_dir")"
EFF_PLIST_DIR="${PLIST_DIR_OVERRIDE:-${SNAP_PLIST_DIR:-${RH_HOME}/Library/LaunchAgents}}"
PROXY_PLIST="$(read_manifest_field "plist_proxy")"
BRIDGE_PLIST="$(read_manifest_field "plist_bridge")"
RH_PLIST="$(read_manifest_field "plist_research_host")"
REPO_ENV_PATH="$(read_manifest_field "repo_env")"
BRIDGE_ENV_PATH="$(read_manifest_field "bridge_env")"
RH_ENV_PATH="$(read_manifest_field "research_host_env")"

[[ -z "${PROXY_PLIST}" ]] && PROXY_PLIST="${EFF_PLIST_DIR}/com.dalton.guidepoint-mcp-proxy.plist"
[[ -z "${BRIDGE_PLIST}" ]] && BRIDGE_PLIST="${EFF_PLIST_DIR}/com.dalton.researchdata-bridge.plist"
[[ -z "${RH_PLIST}" ]] && RH_PLIST="${EFF_PLIST_DIR}/com.dalton.research-host.plist"

# Resolve Docker Compose args via preflight.py (single source of truth)
COMPOSE_ARGS=()
compose_lines=""
if ! compose_lines="$(rh_compose_args)"; then
  echo "ERROR: failed to resolve Docker Compose arguments for ${RH_REPO_ROOT}" >&2
  exit 1
fi
while IFS= read -r line; do
  if [[ -n "${line}" ]]; then
    COMPOSE_ARGS+=("${line}")
  fi
done <<EOF
${compose_lines}
EOF

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "=== DRY-RUN: Research Host Rollback Plan ==="
  echo "Snapshot: ${SNAPSHOT_DIR} (SHA-256 verified)"
  echo "1. Ensure maintenance admission gate is closed (${RH_ADMISSION_FILE}) and verify backend idle via wait_backend_idle.sh"
  echo "2. Stop com.dalton.research-host (${RESEARCH_HOST_URL}) via launchctl bootout"
  echo "3. Stop com.dalton.guidepoint-mcp-proxy (:8943) and com.dalton.researchdata-bridge (:8960), then restore files:"
  rh_snapshot_restore --snapshot-dir "${SNAPSHOT_DIR}" --dry-run
  echo "4. Start com.dalton.guidepoint-mcp-proxy (${PROXY_PLIST}) and com.dalton.researchdata-bridge (${BRIDGE_PLIST})"
  echo "5. Recreate backend container: ${DOCKER_BIN_EFF} compose ${COMPOSE_ARGS[*]} up -d --no-deps --force-recreate backend"
  echo "6. Probe legacy services: :8943/healthz, :8960/healthz, Wiki search, Guidepoint search_library, backend /health"
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

gate_verify_extra=()
if [[ -n "${REPO_ENV_PATH}" ]]; then
  gate_verify_extra+=(--repo-env "${REPO_ENV_PATH}")
fi

echo "[1/7] Checking maintenance admission gate..."
gate_json="$(rh_gate_status)"
gate_already_closed="$(PYTHONDONTWRITEBYTECODE=1 "${RH_PYTHON_BIN}" -B - "${gate_json}" <<'PY'
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
)"

if [[ "${gate_already_closed}" != "1" ]]; then
  echo "  Admission gate is open; closing gate before rollback..."
  if [[ ${#gate_set_extra[@]} -gt 0 ]]; then
    rh_gate_set --paused true --pause-host-direct true --operation rollback --reason "manual_rollback_research_host" "${gate_set_extra[@]}" >/dev/null \
      || fail_with_gate_closed "1/7 close-gate" "Could not write maintenance-admission.json. Check Postgres connectivity or pass --no-db-lock if Postgres is unavailable."
  else
    rh_gate_set --paused true --pause-host-direct true --operation rollback --reason "manual_rollback_research_host" >/dev/null \
      || fail_with_gate_closed "1/7 close-gate" "Could not write maintenance-admission.json. Check Postgres connectivity or pass --no-db-lock if Postgres is unavailable."
  fi
else
  echo "  Admission gate is already closed."
fi

verify_out=""
if [[ ${#gate_verify_extra[@]} -gt 0 ]]; then
  verify_out="$(rh_gate_verify --expected-paused true --backend-url "${BACKEND_URL}" --container-name "${CONTAINER_NAME}" --deadline-s "${GATE_VERIFY_TIMEOUT}" --allow-unavailable "${gate_verify_extra[@]}")" \
    || fail_with_gate_closed "1/7 verify-gate" "Backend container is running but rejected closed-gate verification. Inspect ${CONTAINER_NAME} logs and /app/var/dalton/maintenance-admission.json."
else
  verify_out="$(rh_gate_verify --expected-paused true --backend-url "${BACKEND_URL}" --container-name "${CONTAINER_NAME}" --deadline-s "${GATE_VERIFY_TIMEOUT}" --allow-unavailable)" \
    || fail_with_gate_closed "1/7 verify-gate" "Backend container is running but rejected closed-gate verification. Inspect ${CONTAINER_NAME} logs and /app/var/dalton/maintenance-admission.json."
fi

container_avail="$(PYTHONDONTWRITEBYTECODE=1 "${RH_PYTHON_BIN}" -B - "${verify_out}" <<'PY'
import json
import sys

d = json.loads(sys.argv[1])
print("1" if d.get("container_available") is True else "0")
PY
)"

if [[ "${SKIP_IDLE_WAIT}" -eq 0 && "${container_avail}" == "1" ]]; then
  echo "  Waiting for active backend threads and data tasks to become idle (max ${IDLE_WAIT_SECONDS}s)..."
  PYTHON_BIN="${RH_PYTHON_BIN}" DOCKER_BIN="${DOCKER_BIN_EFF}" CURL_BIN="${CURL_BIN_EFF}" \
    bash "${WAIT_IDLE_SH}" --max-wait-seconds "${IDLE_WAIT_SECONDS}" \
    || fail_with_gate_closed "1/7 wait-backend-idle" "Backend still has active work after ${IDLE_WAIT_SECONDS}s. Wait for running jobs to finish or re-run with --skip-idle-wait during emergency downtime."
else
  echo "  Skipping idle wait (skip_idle_wait=${SKIP_IDLE_WAIT}, container_available=${container_avail})."
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

wait_url_up() {
  local url="$1"
  local timeout_s="$2"
  local elapsed=0
  while [[ "${elapsed}" -lt "${timeout_s}" ]]; do
    local code
    code="$(http_status_code "${url}")"
    if [[ "${code}" == "200" ]]; then
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  return 1
}

echo "[2/7] Stopping com.dalton.research-host (${RESEARCH_HOST_URL})..."
rh_launchctl_bootout "com.dalton.research-host" "${RH_PLIST}" \
  || fail_with_gate_closed "2/7 stop-research-host" "Failed to stop com.dalton.research-host via launchctl bootout. Stop the process manually and confirm ${RESEARCH_HOST_URL}/healthz is unreachable."

if ! wait_url_down "${RESEARCH_HOST_URL%/}/healthz" "${SERVICE_TIMEOUT}"; then
  fail_with_gate_closed "2/7 wait-research-host-down" "${RESEARCH_HOST_URL}/healthz is still responding after ${SERVICE_TIMEOUT}s. Check launchctl list and stop any stray research-host process before re-running."
fi

echo "[3/7] Stopping :8943 and :8960 and restoring snapshot files..."
rh_launchctl_bootout "com.dalton.guidepoint-mcp-proxy" "${PROXY_PLIST}" \
  || fail_with_gate_closed "3/7 stop-legacy-proxy" "Failed to stop com.dalton.guidepoint-mcp-proxy before file restore."
rh_launchctl_bootout "com.dalton.researchdata-bridge" "${BRIDGE_PLIST}" \
  || fail_with_gate_closed "3/7 stop-legacy-bridge" "Failed to stop com.dalton.researchdata-bridge before file restore."

rh_snapshot_restore --snapshot-dir "${SNAPSHOT_DIR}" \
  || fail_with_gate_closed "3/7 restore-snapshot" "Failed to restore files from ${SNAPSHOT_DIR}. Inspect permissions and disk space, then re-run rollback."

echo "[4/7] Starting legacy Guidepoint proxy (:8943) and legacy bridge (:8960)..."
rh_launchctl_bootstrap "com.dalton.guidepoint-mcp-proxy" "${PROXY_PLIST}" \
  || fail_with_gate_closed "4/7 start-legacy-proxy" "Failed to bootstrap ${PROXY_PLIST}. Check plist syntax with plutil -lint and logs in ~/Library/Logs/dalton-guidepoint-mcp-proxy.err.log."
rh_launchctl_bootstrap "com.dalton.researchdata-bridge" "${BRIDGE_PLIST}" \
  || fail_with_gate_closed "4/7 start-legacy-bridge" "Failed to bootstrap ${BRIDGE_PLIST}. Check plist syntax with plutil -lint and logs in ~/Library/Logs/dalton-researchdata-bridge.err.log."

wait_url_up "${PROXY_URL%/}/healthz" "${SERVICE_TIMEOUT}" \
  || fail_with_gate_closed "4/7 wait-legacy-proxy" "Legacy Guidepoint proxy (${PROXY_URL}/healthz) did not return HTTP 200 within ${SERVICE_TIMEOUT}s."
wait_url_up "${BRIDGE_URL%/}/healthz" "${SERVICE_TIMEOUT}" \
  || fail_with_gate_closed "4/7 wait-legacy-bridge" "Legacy bridge (${BRIDGE_URL}/healthz) did not return HTTP 200 within ${SERVICE_TIMEOUT}s."

echo "[5/7] Recreating backend container via Docker Compose..."
(
  cd "${RH_REPO_ROOT}"
  "${DOCKER_BIN_EFF}" compose "${COMPOSE_ARGS[@]}" up -d --no-deps --force-recreate backend
) || fail_with_gate_closed "5/7 recreate-backend" "Docker Compose failed to recreate backend. Check docker compose logs backend and verify ${REPO_ENV_PATH}."

wait_url_up "${BACKEND_URL%/}/health" "${SERVICE_TIMEOUT}" \
  || fail_with_gate_closed "5/7 wait-backend-health" "Backend (${BACKEND_URL}/health) did not return HTTP 200 within ${SERVICE_TIMEOUT}s after recreate."

if [[ ${#gate_verify_extra[@]} -gt 0 ]]; then
  rh_gate_verify --expected-paused true --backend-url "${BACKEND_URL}" --container-name "${CONTAINER_NAME}" --deadline-s "${GATE_VERIFY_TIMEOUT}" "${gate_verify_extra[@]}" >/dev/null \
    || fail_with_gate_closed "5/7 verify-post-recreate-gate" "Backend came up but closed-gate verification failed. Confirm /app/var/dalton/maintenance-admission.json is mounted properly."
else
  rh_gate_verify --expected-paused true --backend-url "${BACKEND_URL}" --container-name "${CONTAINER_NAME}" --deadline-s "${GATE_VERIFY_TIMEOUT}" >/dev/null \
    || fail_with_gate_closed "5/7 verify-post-recreate-gate" "Backend came up but closed-gate verification failed. Confirm /app/var/dalton/maintenance-admission.json is mounted properly."
fi

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

if [[ "${KEEP_ADMISSION_PAUSED}" -eq 1 ]]; then
  echo "[7/7] Rollback verified; keeping maintenance admission gate CLOSED as requested (--keep-admission-paused)."
  echo "ROLLBACK_OK (gate kept paused): restored from ${SNAPSHOT_DIR}"
  exit 0
fi

echo "[7/7] Opening maintenance admission gate..."
if [[ ${#gate_set_extra[@]} -gt 0 ]]; then
  rh_gate_set --paused false --pause-host-direct false --operation rollback --reason "rollback_verified" "${gate_set_extra[@]}" >/dev/null \
    || fail_with_gate_closed "7/7 open-gate" "Failed to open maintenance admission gate. Open it manually after checking Postgres connectivity."
else
  rh_gate_set --paused false --pause-host-direct false --operation rollback --reason "rollback_verified" >/dev/null \
    || fail_with_gate_closed "7/7 open-gate" "Failed to open maintenance admission gate. Open it manually after checking Postgres connectivity."
fi

if [[ ${#gate_verify_extra[@]} -gt 0 ]]; then
  if ! rh_gate_verify --expected-paused false --backend-url "${BACKEND_URL}" --container-name "${CONTAINER_NAME}" --deadline-s "${GATE_VERIFY_TIMEOUT}" "${gate_verify_extra[@]}" >/dev/null; then
    if [[ ${#gate_set_extra[@]} -gt 0 ]]; then
      rh_gate_set --paused true --pause-host-direct true --operation rollback --reason "rollback_open_verify_failed" "${gate_set_extra[@]}" >/dev/null || true
    else
      rh_gate_set --paused true --pause-host-direct true --operation rollback --reason "rollback_open_verify_failed" >/dev/null || true
    fi
    fail_with_gate_closed "7/7 verify-gate-open" "Backend did not confirm open admission gate (HTTP 200); gate was re-closed for safety."
  fi
else
  if ! rh_gate_verify --expected-paused false --backend-url "${BACKEND_URL}" --container-name "${CONTAINER_NAME}" --deadline-s "${GATE_VERIFY_TIMEOUT}" >/dev/null; then
    if [[ ${#gate_set_extra[@]} -gt 0 ]]; then
      rh_gate_set --paused true --pause-host-direct true --operation rollback --reason "rollback_open_verify_failed" "${gate_set_extra[@]}" >/dev/null || true
    else
      rh_gate_set --paused true --pause-host-direct true --operation rollback --reason "rollback_open_verify_failed" >/dev/null || true
    fi
    fail_with_gate_closed "7/7 verify-gate-open" "Backend did not confirm open admission gate (HTTP 200); gate was re-closed for safety."
  fi
fi

echo "ROLLBACK_OK: restored legacy :8943 + :8960 baseline from ${SNAPSHOT_DIR} and reopened admission gate."
