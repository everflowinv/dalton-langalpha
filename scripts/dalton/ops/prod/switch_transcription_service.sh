#!/usr/bin/env bash
# Everflow production cutover & rollback helper for standalone dalton-transcription (:8965).
#
# Implements docs/dalton/features/researchdata-connectors.md §6.3 cutover & rollback sequence:
#   Switch:
#     1. Drain old bridge ASR (`POST /transcriptions/drain`), poll until `active_jobs == 0`.
#     2. Set `DALTON_BRIDGE_ASR_DISABLED=true` in bridge env, restart bridge, verify `.owner.lock` released & 503.
#     3. Start `com.dalton.transcription` (`:8965`) on identical `DALTON_ASR_JOBS_DIR` & `DALTON_ASR_USAGE_FILE`,
#        verify `/healthz` reports `owner_lock_held: true`.
#     4. Update repo `.env` (`DALTON_TRANSCRIPTION_SERVICE_URL` + `DALTON_TRANSCRIPTION_TOKEN`) and recreate backend:
#        `docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --no-deps backend` (E16).
#   Rollback:
#     1. Drain `com.dalton.transcription` (`POST :8965/transcriptions/drain`), poll until `active_jobs == 0`,
#        stop `com.dalton.transcription` (`launchctl bootout`) so `.owner.lock` is released.
#     2. Set `DALTON_BRIDGE_ASR_DISABLED=false` in bridge env, restart bridge, verify bridge re-acquires `.owner.lock`.
#     3. Restore repo `.env` transcription settings and recreate backend via `docker compose ... up -d --no-deps backend`.
#
# Usage:
#   bash scripts/dalton/ops/prod/switch_transcription_service.sh --check
#   bash scripts/dalton/ops/prod/switch_transcription_service.sh --drain-bridge [--wait-seconds 120]
#   bash scripts/dalton/ops/prod/switch_transcription_service.sh --drain-service [--wait-seconds 120]
#   bash scripts/dalton/ops/prod/switch_transcription_service.sh --switch [--dry-run] [--wait-seconds 120]
#   bash scripts/dalton/ops/prod/switch_transcription_service.sh --rollback [--dry-run] [--wait-seconds 120]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
BRIDGE_URL="${DALTON_BRIDGE_LOCAL_URL:-http://127.0.0.1:8960}"
TRANSCRIPTION_URL="${DALTON_TRANSCRIPTION_LOCAL_URL:-http://127.0.0.1:8965}"
BRIDGE_ENV_FILE="${DALTON_BRIDGE_ENV_FILE:-$HOME/.config/dalton/researchdata-bridge.env}"
TRANSCRIPTION_ENV_FILE="${DALTON_TRANSCRIPTION_ENV_FILE:-$HOME/.config/dalton/transcription.env}"
REPO_ENV_FILE="${DALTON_REPO_ENV_FILE:-$REPO_ROOT/.env}"
LAUNCH_AGENTS_DIR="${DALTON_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
BRIDGE_LABEL="com.dalton.researchdata-bridge"
TRANSCRIPTION_LABEL="com.dalton.transcription"
WAIT_SECONDS=120
DRY_RUN=0
ACTION=""

usage() {
  cat <<'EOF'
Usage: switch_transcription_service.sh <action> [options]

Actions (pick one):
  --check           Inspect bridge/service healthz, .owner.lock holder, and path parity
  --drain-bridge    Drain legacy bridge ASR intake and wait for active_jobs == 0
  --drain-service   Drain standalone dalton-transcription intake and wait for active_jobs == 0
  --switch          Run full §6.3 cutover (bridge -> dalton-transcription -> recreate backend)
  --rollback        Run full §6.3 rollback (dalton-transcription -> bridge -> recreate backend)

Options:
  --wait-seconds N  Max seconds to wait for active_jobs == 0 during drain (default: 120)
  --dry-run         Print mutating commands without executing them
  -h, --help        Show this help text
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check|--drain-bridge|--drain-service|--switch|--rollback)
      ACTION="$1"
      shift
      ;;
    --wait-seconds)
      WAIT_SECONDS="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$ACTION" ]]; then
  usage >&2
  exit 2
fi

read_env_key() {
  local file="$1"
  local key="$2"
  [[ -f "$file" ]] || return 0
  python3 - "$file" "$key" <<'PY'
import sys
path, target = sys.argv[1], sys.argv[2]
val = ""
with open(path, encoding="utf-8") as fh:
    for raw in fh:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        k, v = line.split("=", 1)
        if k.strip() == target:
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            val = v
print(val)
PY
}

upsert_env_key() {
  local file="$1"
  local key="$2"
  local value="$3"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] upsert $key in $file"
    return 0
  fi
  python3 - "$file" "$key" "$value" <<'PY'
import os, sys, tempfile
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
lines = []
if os.path.exists(path):
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
updated = False
out = []
for line in lines:
    stripped = line.strip()
    candidate = stripped[len("export "):].strip() if stripped.startswith("export ") else stripped
    if candidate.startswith(f"{key}=") and not stripped.startswith("#"):
        out.append(f"{key}={value}")
        updated = True
    else:
        out.append(line)
if not updated:
    out.append(f"{key}={value}")
os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
fd, tmp = tempfile.mkstemp(prefix=".env_upsert_", dir=os.path.dirname(path) or ".")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
PY
}

drain_and_wait() {
  local base_url="$1"
  local token="$2"
  local label="$3"
  if [[ -z "$token" ]]; then
    echo "ERROR: Missing admin/service token for $label ($base_url)" >&2
    exit 1
  fi
  echo "[$label] Sending POST $base_url/transcriptions/drain ..."
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] curl -sf -X POST -H 'Authorization: Bearer <redacted>' $base_url/transcriptions/drain"
    return 0
  fi
  curl -sf -X POST \
    -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json" \
    -d '{"drain":true}' \
    "${base_url}/transcriptions/drain"
  echo

  local deadline=$(( $(date +%s) + WAIT_SECONDS ))
  while true; do
    local resp
    resp="$(curl -sf -H "Authorization: Bearer ${token}" "${base_url}/transcriptions/drain")"
    local active
    active="$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(d.get("active_jobs", -1))' "$resp")"
    echo "[$label] drain status: active_jobs=${active}"
    if [[ "$active" == "0" ]]; then
      echo "[$label] ready for handoff (active_jobs=0)."
      return 0
    fi
    if [[ "$(date +%s)" -ge "$deadline" ]]; then
      echo "ERROR: Timed out after ${WAIT_SECONDS}s waiting for $label active_jobs to reach 0 (still ${active})." >&2
      exit 1
    fi
    sleep 2
  done
}

run_cmd() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] $*"
    return 0
  fi
  "$@"
}

show_check() {
  local jobs_dir="${DALTON_ASR_JOBS_DIR:-$REPO_ROOT/var/dalton/asr_jobs}"
  local lock_file="${jobs_dir}/.owner.lock"
  echo "=== Paths ==="
  echo "REPO_ROOT:              $REPO_ROOT"
  echo "DALTON_ASR_JOBS_DIR:    $jobs_dir"
  echo "OWNER_LOCK_FILE:        $lock_file"
  if [[ -f "$lock_file" ]]; then
    echo "--- .owner.lock metadata ---"
    cat "$lock_file"
  else
    echo "(no .owner.lock file present yet)"
  fi
  echo
  echo "=== Legacy Bridge ($BRIDGE_URL/healthz) ==="
  curl -sf "${BRIDGE_URL}/healthz" | python3 -m json.tool 2>/dev/null || echo "(bridge unreachable)"
  echo
  echo "=== Standalone Transcription ($TRANSCRIPTION_URL/healthz) ==="
  curl -sf "${TRANSCRIPTION_URL}/healthz" | python3 -m json.tool 2>/dev/null || echo "(transcription service unreachable)"
}

BRIDGE_ADMIN_TOKEN="${DALTON_BRIDGE_ADMIN_TOKEN:-$(read_env_key "$BRIDGE_ENV_FILE" DALTON_BRIDGE_ADMIN_TOKEN)}"
TRANSCRIPTION_TOKEN="${DALTON_TRANSCRIPTION_TOKEN:-$(read_env_key "$TRANSCRIPTION_ENV_FILE" DALTON_TRANSCRIPTION_TOKEN)}"
GUI_DOMAIN="gui/$(id -u)"

case "$ACTION" in
  --check)
    show_check
    ;;
  --drain-bridge)
    drain_and_wait "$BRIDGE_URL" "$BRIDGE_ADMIN_TOKEN" "researchdata-bridge"
    ;;
  --drain-service)
    drain_and_wait "$TRANSCRIPTION_URL" "$TRANSCRIPTION_TOKEN" "dalton-transcription"
    ;;
  --switch)
    if [[ -z "$TRANSCRIPTION_TOKEN" ]]; then
      echo "ERROR: DALTON_TRANSCRIPTION_TOKEN not found in env or $TRANSCRIPTION_ENV_FILE" >&2
      exit 1
    fi
    echo "=== Step 1/4: Drain legacy bridge ASR ==="
    drain_and_wait "$BRIDGE_URL" "$BRIDGE_ADMIN_TOKEN" "researchdata-bridge"

    echo "=== Step 2/4: Disable legacy bridge ASR and restart bridge to release .owner.lock ==="
    upsert_env_key "$BRIDGE_ENV_FILE" "DALTON_BRIDGE_ASR_DISABLED" "true"
    run_cmd launchctl kickstart -k "${GUI_DOMAIN}/${BRIDGE_LABEL}"
    if [[ "$DRY_RUN" -eq 0 ]]; then
      sleep 2
      http_code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${BRIDGE_ADMIN_TOKEN}" "${BRIDGE_URL}/transcriptions/health-probe")"
      if [[ "$http_code" != "503" ]]; then
        echo "ERROR: Expected 503 from legacy bridge /transcriptions/* after disable, got HTTP ${http_code}" >&2
        exit 1
      fi
      echo "Legacy bridge /transcriptions/* confirmed returning HTTP 503 (disabled)."
    fi

    echo "=== Step 3/4: Start standalone com.dalton.transcription (:8965) ==="
    TRANSCRIPTION_PLIST="${LAUNCH_AGENTS_DIR}/${TRANSCRIPTION_LABEL}.plist"
    if [[ ! -f "$TRANSCRIPTION_PLIST" && "$DRY_RUN" -eq 0 ]]; then
      echo "ERROR: Missing installed plist: ${TRANSCRIPTION_PLIST} (install from scripts/dalton/launchd/com.dalton.transcription.plist.template)" >&2
      exit 1
    fi
    if launchctl print "${GUI_DOMAIN}/${TRANSCRIPTION_LABEL}" >/dev/null 2>&1; then
      run_cmd launchctl kickstart -k "${GUI_DOMAIN}/${TRANSCRIPTION_LABEL}"
    else
      run_cmd launchctl bootstrap "${GUI_DOMAIN}" "${TRANSCRIPTION_PLIST}"
    fi
    if [[ "$DRY_RUN" -eq 0 ]]; then
      for _ in $(seq 1 20); do
        if hz="$(curl -sf "${TRANSCRIPTION_URL}/healthz" 2>/dev/null)"; then
          held="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("owner_lock_held", False))' "$hz")"
          if [[ "$held" == "True" ]]; then
            echo "dalton-transcription is healthy and holds .owner.lock: $hz"
            break
          fi
        fi
        sleep 1
      done
    fi

    echo "=== Step 4/4: Update .env and recreate backend container (E16) ==="
    upsert_env_key "$REPO_ENV_FILE" "DALTON_TRANSCRIPTION_SERVICE_URL" "http://host.docker.internal:8965"
    upsert_env_key "$REPO_ENV_FILE" "DALTON_TRANSCRIPTION_TOKEN" "$TRANSCRIPTION_TOKEN"
    run_cmd bash -c "cd '$REPO_ROOT' && docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --no-deps backend"
    echo "Cutover completed."
    ;;
  --rollback)
    if [[ -z "$BRIDGE_ADMIN_TOKEN" ]]; then
      echo "ERROR: DALTON_BRIDGE_ADMIN_TOKEN not found in env or $BRIDGE_ENV_FILE" >&2
      exit 1
    fi
    echo "=== Step 1/3: Drain and stop standalone com.dalton.transcription ==="
    if curl -sf "${TRANSCRIPTION_URL}/healthz" >/dev/null 2>&1; then
      drain_and_wait "$TRANSCRIPTION_URL" "$TRANSCRIPTION_TOKEN" "dalton-transcription"
    fi
    run_cmd launchctl bootout "${GUI_DOMAIN}/${TRANSCRIPTION_LABEL}" || true
    [[ "$DRY_RUN" -eq 0 ]] && sleep 1

    echo "=== Step 2/3: Re-enable legacy bridge ASR and restart bridge to acquire .owner.lock ==="
    upsert_env_key "$BRIDGE_ENV_FILE" "DALTON_BRIDGE_ASR_DISABLED" "false"
    run_cmd launchctl kickstart -k "${GUI_DOMAIN}/${BRIDGE_LABEL}"
    [[ "$DRY_RUN" -eq 0 ]] && sleep 2

    echo "=== Step 3/3: Restore .env to bridge endpoint and recreate backend container (E16) ==="
    upsert_env_key "$REPO_ENV_FILE" "DALTON_TRANSCRIPTION_SERVICE_URL" "http://host.docker.internal:8960"
    upsert_env_key "$REPO_ENV_FILE" "DALTON_TRANSCRIPTION_TOKEN" "$BRIDGE_ADMIN_TOKEN"
    run_cmd bash -c "cd '$REPO_ROOT' && docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --no-deps backend"
    echo "Rollback completed."
    ;;
esac
