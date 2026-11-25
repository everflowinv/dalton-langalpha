#!/usr/bin/env bash
# Everflow production cutover & rollback helper for standalone dalton-transcription (:8965).
#
# Implements docs/dalton/features/researchdata-connectors.md §6.3 cutover & rollback sequence:
#   Switch:
#     0. Pre-check installed plist, repository venv Python deps, and historical job sample.
#     1. Drain old bridge ASR (`POST /transcriptions/drain`), poll until `active_jobs == 0`.
#     2. Set `DALTON_BRIDGE_ASR_DISABLED=true` in bridge env, restart bridge, verify `.owner.lock` released & 503.
#     3. Start `com.dalton.transcription` (`:8965`) on identical `DALTON_ASR_JOBS_DIR` & `DALTON_ASR_USAGE_FILE`,
#        verify `/healthz` reports `owner_lock_held: true`, verify auth with `DALTON_TRANSCRIPTION_TOKEN`,
#        and verify querying a completed historical job succeeds. Fail closed without touching `.env` or backend.
#     4. Update repo `.env` (`DALTON_TRANSCRIPTION_SERVICE_URL` + `DALTON_TRANSCRIPTION_TOKEN`) and recreate backend:
#        `docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --no-deps backend` (E16).
#   Rollback:
#     1. Drain `com.dalton.transcription` (`POST :8965/transcriptions/drain`), poll until `active_jobs == 0`,
#        stop `com.dalton.transcription` (`launchctl bootout`, failing fast on error) and verify process stopped
#        (or draining with active_jobs=0 and lock released) and `.owner.lock` is released.
#     2. Set `DALTON_BRIDGE_ASR_DISABLED=false` in bridge env, restart bridge, verify bridge `/healthz` reports
#        `asr.disabled: false` and `asr.owner_lock_held: true`, and verify bridge auth passes.
#     3. Restore repo `.env` transcription settings and recreate backend via `docker compose ... up -d --no-deps backend`.
#
# Usage:
#   bash scripts/dalton/ops/prod/switch_transcription_service.sh --check
#   bash scripts/dalton/ops/prod/switch_transcription_service.sh --drain-bridge [--wait-seconds 120]
#   bash scripts/dalton/ops/prod/switch_transcription_service.sh --drain-service [--wait-seconds 120]
#   bash scripts/dalton/ops/prod/switch_transcription_service.sh --switch [--dry-run] [--wait-seconds 120] [--allow-empty-history]
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
TRANSCRIPTION_PLIST="${LAUNCH_AGENTS_DIR}/${TRANSCRIPTION_LABEL}.plist"
WAIT_SECONDS=120
DRY_RUN=0
ALLOW_EMPTY_HISTORY=0
ACTION=""

usage() {
  cat <<'EOF'
Usage: switch_transcription_service.sh <action> [options]

Actions (pick one):
  --check           Inspect bridge/service healthz, .owner.lock holder, path parity, and venv Python deps
  --drain-bridge    Drain legacy bridge ASR intake and wait for active_jobs == 0
  --drain-service   Drain standalone dalton-transcription intake and wait for active_jobs == 0
  --switch          Run full §6.3 cutover (bridge -> dalton-transcription -> recreate backend)
  --rollback        Run full §6.3 rollback (dalton-transcription -> bridge -> recreate backend)

Options:
  --wait-seconds N       Max seconds to wait for active_jobs == 0 during drain (default: 120)
  --allow-empty-history  Allow --switch when DALTON_ASR_JOBS_DIR has no completed historical job
  --dry-run              Print mutating commands without executing them
  -h, --help             Show this help text
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
    --allow-empty-history)
      ALLOW_EMPTY_HISTORY=1
      shift
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

resolve_transcription_python() {
  if [[ -n "${DALTON_TRANSCRIPTION_PYTHON_BIN:-}" ]]; then
    echo "$DALTON_TRANSCRIPTION_PYTHON_BIN"
    return 0
  fi
  if [[ -f "$TRANSCRIPTION_PLIST" ]]; then
    local from_plist
    from_plist="$(python3 - "$TRANSCRIPTION_PLIST" <<'PY'
import plistlib, sys
try:
    with open(sys.argv[1], "rb") as fh:
        data = plistlib.load(fh)
    args = data.get("ProgramArguments") or []
    val = str(args[0]).strip() if args else ""
    if val and "__PYTHON_BIN__" not in val:
        print(val)
except Exception:
    pass
PY
)"
    if [[ -n "$from_plist" ]]; then
      echo "$from_plist"
      return 0
    fi
  fi
  echo "${REPO_ROOT}/.venv/bin/python"
}

check_transcription_python() {
  local py_bin
  py_bin="$(resolve_transcription_python)"
  echo "PYTHON_BIN:             $py_bin"
  if [[ ! -x "$py_bin" ]]; then
    echo "ERROR: Transcription Python interpreter is missing or not executable: $py_bin" >&2
    echo "       Set __PYTHON_BIN__ in ${TRANSCRIPTION_PLIST} to <repo>/.venv/bin/python." >&2
    return 1
  fi
  if ! "$py_bin" -c "import fastapi, uvicorn, boto3, httpx" 2>&1; then
    echo "ERROR: $py_bin failed dependency check (import fastapi, uvicorn, boto3, httpx)." >&2
    echo "       __PYTHON_BIN__ must point to the repository virtualenv (<repo>/.venv/bin/python)." >&2
    return 1
  fi
  echo "PYTHON_DEPS:            OK (fastapi, uvicorn, boto3, httpx)"
  return 0
}

check_plist_installed() {
  if [[ ! -f "$TRANSCRIPTION_PLIST" ]]; then
    echo "ERROR: Missing installed plist: ${TRANSCRIPTION_PLIST} (install from scripts/dalton/launchd/com.dalton.transcription.plist.template)" >&2
    return 1
  fi
  if grep -Eq '__PYTHON_BIN__|__REPO__|__HOME__|__LOG_DIR__' "$TRANSCRIPTION_PLIST"; then
    echo "ERROR: Installed plist ${TRANSCRIPTION_PLIST} still contains unreplaced template placeholders." >&2
    return 1
  fi
  return 0
}

resolve_jobs_dir() {
  if [[ -n "${DALTON_ASR_JOBS_DIR:-}" ]]; then
    echo "$DALTON_ASR_JOBS_DIR"
    return 0
  fi
  local from_env
  from_env="$(read_env_key "$TRANSCRIPTION_ENV_FILE" DALTON_ASR_JOBS_DIR)"
  if [[ -n "$from_env" ]]; then
    echo "$from_env"
    return 0
  fi
  if [[ -f "$TRANSCRIPTION_PLIST" ]]; then
    local from_plist
    from_plist="$(python3 - "$TRANSCRIPTION_PLIST" <<'PY'
import plistlib, sys
try:
    with open(sys.argv[1], "rb") as fh:
        data = plistlib.load(fh)
    val = str((data.get("EnvironmentVariables") or {}).get("DALTON_ASR_JOBS_DIR") or "").strip()
    if val and "__REPO__" not in val:
        print(val)
except Exception:
    pass
PY
)"
    if [[ -n "$from_plist" ]]; then
      echo "$from_plist"
      return 0
    fi
  fi
  echo "${REPO_ROOT}/var/dalton/asr_jobs"
}

find_completed_job_id() {
  local jobs_dir="$1"
  [[ -d "$jobs_dir" ]] || return 0
  python3 - "$jobs_dir" <<'PY'
import json, os, sys
jobs_dir = sys.argv[1]
try:
    entries = sorted(os.listdir(jobs_dir))
except OSError:
    sys.exit(0)
for name in entries:
    state_path = os.path.join(jobs_dir, name, "state.json")
    if not os.path.isfile(state_path):
        continue
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception:
        continue
    if isinstance(state, dict) and str(state.get("status") or "").strip().lower() == "succeeded":
        jid = str(state.get("job_id") or name).strip()
        if jid:
            print(jid)
            sys.exit(0)
PY
}

is_owner_lock_free() {
  local jobs_dir="$1"
  local lock_file="${jobs_dir}/.owner.lock"
  [[ -f "$lock_file" ]] || return 0
  python3 - "$lock_file" <<'PY'
import fcntl, sys
path = sys.argv[1]
with open(path, "a+", encoding="utf-8") as fh:
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(1)
    else:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        sys.exit(0)
PY
}

http_get_to_file() {
  local url="$1"
  local out_file="$2"
  local token="${3:-}"
  if [[ -n "$token" ]]; then
    curl -s -o "$out_file" -w '%{http_code}' -H "Authorization: Bearer ${token}" "$url" || echo "000"
  else
    curl -s -o "$out_file" -w '%{http_code}' "$url" || echo "000"
  fi
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

show_status_snapshot() {
  local jobs_dir
  jobs_dir="$(resolve_jobs_dir)"
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

print_switch_failure_recovery() {
  echo >&2
  echo "=== Current Service Status (repo .env and backend container were left unchanged) ===" >&2
  show_status_snapshot >&2
  echo >&2
  echo "=== Recovery Commands (re-enable legacy bridge ASR intake) ===" >&2
  cat >&2 <<EOF
  1. Stop standalone transcription service (if loaded):
       launchctl bootout "${GUI_DOMAIN}/${TRANSCRIPTION_LABEL}" || true
  2. Re-enable legacy bridge ASR in ${BRIDGE_ENV_FILE} and restart bridge:
       python3 -c 'import pathlib; p=pathlib.Path("${BRIDGE_ENV_FILE}"); lines=[l for l in (p.read_text().splitlines() if p.exists() else []) if not l.strip().startswith("DALTON_BRIDGE_ASR_DISABLED=")]; lines.append("DALTON_BRIDGE_ASR_DISABLED=false"); p.write_text("\n".join(lines)+"\n")'
       launchctl kickstart -k "${GUI_DOMAIN}/${BRIDGE_LABEL}"
  3. Verify legacy bridge holds .owner.lock and accepts requests:
       curl -sf "${BRIDGE_URL}/healthz" | python3 -m json.tool
  (Or run: bash scripts/dalton/ops/prod/switch_transcription_service.sh --rollback)
EOF
}

show_check() {
  show_status_snapshot
  echo
  echo "=== Transcription Python Runtime ==="
  check_transcription_python
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
    JOBS_DIR="$(resolve_jobs_dir)"
    SAMPLE_JOB_ID="$(find_completed_job_id "$JOBS_DIR")"
    if [[ "$DRY_RUN" -eq 0 ]]; then
      check_plist_installed
      check_transcription_python
      if [[ -z "$SAMPLE_JOB_ID" && "$ALLOW_EMPTY_HISTORY" -eq 0 ]]; then
        echo "ERROR: No completed historical job (status=succeeded) found under ${JOBS_DIR}." >&2
        echo "       Verify DALTON_ASR_JOBS_DIR parity before cutover, or pass --allow-empty-history for a brand-new directory." >&2
        exit 1
      fi
    fi

    echo "=== Step 1/4: Drain legacy bridge ASR ==="
    drain_and_wait "$BRIDGE_URL" "$BRIDGE_ADMIN_TOKEN" "researchdata-bridge"

    echo "=== Step 2/4: Disable legacy bridge ASR and restart bridge to release .owner.lock ==="
    upsert_env_key "$BRIDGE_ENV_FILE" "DALTON_BRIDGE_ASR_DISABLED" "true"
    run_cmd launchctl kickstart -k "${GUI_DOMAIN}/${BRIDGE_LABEL}"
    if [[ "$DRY_RUN" -eq 0 ]]; then
      bridge_disabled_ok=0
      for _ in $(seq 1 15); do
        if bhz="$(curl -sf "${BRIDGE_URL}/healthz" 2>/dev/null)"; then
          if python3 - "$bhz" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
asr = d.get("asr") or {}
sys.exit(0 if (asr.get("disabled") is True and asr.get("owner_lock_held") is False) else 1)
PY
          then
            bridge_disabled_ok=1
            break
          fi
        fi
        sleep 1
      done
      if [[ "$bridge_disabled_ok" -ne 1 ]]; then
        echo "ERROR: Legacy bridge did not confirm asr.disabled=true and owner_lock_held=false after restart." >&2
        print_switch_failure_recovery
        exit 1
      fi
      http_code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${BRIDGE_ADMIN_TOKEN}" "${BRIDGE_URL}/transcriptions/health-probe" || echo "000")"
      if [[ "$http_code" != "503" ]]; then
        echo "ERROR: Expected HTTP 503 from legacy bridge /transcriptions/* after disable, got HTTP ${http_code}" >&2
        print_switch_failure_recovery
        exit 1
      fi
      echo "Legacy bridge /transcriptions/* confirmed disabled, lock released, returning HTTP 503."
    fi

    echo "=== Step 3/4: Start standalone com.dalton.transcription (:8965) and verify lock, auth, and historical read ==="
    if launchctl print "${GUI_DOMAIN}/${TRANSCRIPTION_LABEL}" >/dev/null 2>&1; then
      run_cmd launchctl kickstart -k "${GUI_DOMAIN}/${TRANSCRIPTION_LABEL}"
    else
      run_cmd launchctl bootstrap "${GUI_DOMAIN}" "${TRANSCRIPTION_PLIST}"
    fi
    if [[ "$DRY_RUN" -eq 0 ]]; then
      service_lock_ok=0
      last_hz=""
      for _ in $(seq 1 20); do
        if hz="$(curl -sf "${TRANSCRIPTION_URL}/healthz" 2>/dev/null)"; then
          last_hz="$hz"
          if python3 - "$hz" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
ok = (
    d.get("owner_lock_held") is True
    and d.get("disabled") is False
    and d.get("status") == "ok"
)
sys.exit(0 if ok else 1)
PY
          then
            service_lock_ok=1
            echo "dalton-transcription is healthy and holds .owner.lock: $hz"
            break
          fi
        fi
        sleep 1
      done
      if [[ "$service_lock_ok" -ne 1 ]]; then
        echo "ERROR: dalton-transcription did not report status=ok and owner_lock_held=true within 20s (last healthz: ${last_hz:-unreachable})." >&2
        print_switch_failure_recovery
        exit 1
      fi

      # Verify new token authenticates against dalton-transcription
      auth_body_file="$(mktemp)"
      auth_code="$(http_get_to_file "${TRANSCRIPTION_URL}/transcriptions/drain" "$auth_body_file" "$TRANSCRIPTION_TOKEN")"
      auth_body="$(cat "$auth_body_file")"
      rm -f "$auth_body_file"
      if [[ "$auth_code" != "200" ]] || ! python3 - "$auth_body" <<'PY'
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    sys.exit(1)
sys.exit(0 if (d.get("ok") is True and d.get("owner_lock_held") is True) else 1)
PY
      then
        echo "ERROR: Authentication check with DALTON_TRANSCRIPTION_TOKEN failed (HTTP ${auth_code}): ${auth_body}" >&2
        print_switch_failure_recovery
        exit 1
      fi
      echo "Authenticated against dalton-transcription with DALTON_TRANSCRIPTION_TOKEN (HTTP 200)."

      # Re-scan jobs_dir in case a job completed during drain
      if [[ -z "$SAMPLE_JOB_ID" ]]; then
        SAMPLE_JOB_ID="$(find_completed_job_id "$JOBS_DIR")"
      fi
      if [[ -n "$SAMPLE_JOB_ID" ]]; then
        hist_body_file="$(mktemp)"
        hist_code="$(http_get_to_file "${TRANSCRIPTION_URL}/transcriptions/${SAMPLE_JOB_ID}" "$hist_body_file" "$TRANSCRIPTION_TOKEN")"
        hist_body="$(cat "$hist_body_file")"
        rm -f "$hist_body_file"
        if [[ "$hist_code" != "200" ]] || ! python3 - "$hist_body" "$SAMPLE_JOB_ID" <<'PY'
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    sys.exit(1)
job = d.get("job") if isinstance(d, dict) else None
ok = (
    isinstance(job, dict)
    and d.get("ok") is True
    and str(job.get("job_id") or "") == sys.argv[2]
    and str(job.get("status") or "").lower() == "succeeded"
)
sys.exit(0 if ok else 1)
PY
        then
          echo "ERROR: Historical job query failed on dalton-transcription for job_id=${SAMPLE_JOB_ID} (HTTP ${hist_code}): ${hist_body}" >&2
          print_switch_failure_recovery
          exit 1
        fi
        echo "Verified historical completed job query on dalton-transcription: job_id=${SAMPLE_JOB_ID} (status=succeeded)."
      elif [[ "$ALLOW_EMPTY_HISTORY" -eq 1 ]]; then
        echo "Skipping historical job read check (--allow-empty-history set and no succeeded jobs in ${JOBS_DIR})."
      else
        echo "ERROR: No completed historical job found in ${JOBS_DIR} to verify dalton-transcription storage read." >&2
        print_switch_failure_recovery
        exit 1
      fi
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
    JOBS_DIR="$(resolve_jobs_dir)"

    echo "=== Step 1/3: Drain and stop standalone com.dalton.transcription ==="
    if ts_hz="$(curl -sf "${TRANSCRIPTION_URL}/healthz" 2>/dev/null)"; then
      ts_disabled="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("disabled", False))' "$ts_hz" 2>/dev/null || echo "False")"
      if [[ "$ts_disabled" != "True" ]]; then
        drain_and_wait "$TRANSCRIPTION_URL" "$TRANSCRIPTION_TOKEN" "dalton-transcription"
      fi
    fi
    if launchctl print "${GUI_DOMAIN}/${TRANSCRIPTION_LABEL}" >/dev/null 2>&1; then
      if ! run_cmd launchctl bootout "${GUI_DOMAIN}/${TRANSCRIPTION_LABEL}"; then
        echo "ERROR: launchctl bootout failed for ${GUI_DOMAIN}/${TRANSCRIPTION_LABEL}; aborting rollback before modifying .env or backend." >&2
        show_status_snapshot >&2
        exit 1
      fi
    fi
    if [[ "$DRY_RUN" -eq 0 ]]; then
      service_stopped_ok=0
      for _ in $(seq 1 15); do
        if ts_check_hz="$(curl -sf "${TRANSCRIPTION_URL}/healthz" 2>/dev/null)"; then
          if python3 - "$ts_check_hz" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
safe = (
    (d.get("draining") is True or d.get("disabled") is True)
    and d.get("active_jobs") == 0
    and d.get("owner_lock_held") is False
)
sys.exit(0 if safe else 1)
PY
          then
            if is_owner_lock_free "$JOBS_DIR"; then
              service_stopped_ok=1
              break
            fi
          fi
        else
          if is_owner_lock_free "$JOBS_DIR"; then
            service_stopped_ok=1
            break
          fi
        fi
        sleep 1
      done
      if [[ "$service_stopped_ok" -ne 1 ]]; then
        echo "ERROR: Standalone dalton-transcription has not stopped or released .owner.lock; aborting rollback before enabling legacy bridge or modifying .env." >&2
        show_status_snapshot >&2
        exit 1
      fi
      echo "Confirmed standalone dalton-transcription stopped / idle and .owner.lock released."
    fi

    echo "=== Step 2/3: Re-enable legacy bridge ASR and verify .owner.lock re-acquired ==="
    upsert_env_key "$BRIDGE_ENV_FILE" "DALTON_BRIDGE_ASR_DISABLED" "false"
    run_cmd launchctl kickstart -k "${GUI_DOMAIN}/${BRIDGE_LABEL}"
    if [[ "$DRY_RUN" -eq 0 ]]; then
      bridge_ready_ok=0
      last_bhz=""
      for _ in $(seq 1 20); do
        if bhz="$(curl -sf "${BRIDGE_URL}/healthz" 2>/dev/null)"; then
          last_bhz="$bhz"
          if python3 - "$bhz" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
asr = d.get("asr") or {}
ok = (
    asr.get("disabled") is False
    and asr.get("owner_lock_held") is True
)
sys.exit(0 if ok else 1)
PY
          then
            bridge_ready_ok=1
            echo "Legacy bridge is healthy with ASR enabled and holds .owner.lock."
            break
          fi
        fi
        sleep 1
      done
      if [[ "$bridge_ready_ok" -ne 1 ]]; then
        echo "ERROR: Legacy bridge did not report asr.disabled=false and asr.owner_lock_held=true within 20s (last healthz: ${last_bhz:-unreachable})." >&2
        echo "       Repo .env and backend container were left unchanged." >&2
        show_status_snapshot >&2
        exit 1
      fi

      b_auth_body_file="$(mktemp)"
      b_auth_code="$(http_get_to_file "${BRIDGE_URL}/transcriptions/drain" "$b_auth_body_file" "$BRIDGE_ADMIN_TOKEN")"
      b_auth_body="$(cat "$b_auth_body_file")"
      rm -f "$b_auth_body_file"
      if [[ "$b_auth_code" != "200" ]] || ! python3 - "$b_auth_body" <<'PY'
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    sys.exit(1)
sys.exit(0 if (d.get("ok") is True and d.get("disabled") is False and d.get("owner_lock_held") is True) else 1)
PY
      then
        echo "ERROR: Legacy bridge auth/lock check failed (HTTP ${b_auth_code}): ${b_auth_body}" >&2
        echo "       Repo .env and backend container were left unchanged." >&2
        show_status_snapshot >&2
        exit 1
      fi

      SAMPLE_JOB_ID="$(find_completed_job_id "$JOBS_DIR")"
      if [[ -n "$SAMPLE_JOB_ID" ]]; then
        b_hist_body_file="$(mktemp)"
        b_hist_code="$(http_get_to_file "${BRIDGE_URL}/transcriptions/${SAMPLE_JOB_ID}" "$b_hist_body_file" "$BRIDGE_ADMIN_TOKEN")"
        b_hist_body="$(cat "$b_hist_body_file")"
        rm -f "$b_hist_body_file"
        if [[ "$b_hist_code" != "200" ]]; then
          echo "ERROR: Legacy bridge historical job read failed for job_id=${SAMPLE_JOB_ID} (HTTP ${b_hist_code}): ${b_hist_body}" >&2
          echo "       Repo .env and backend container were left unchanged." >&2
          show_status_snapshot >&2
          exit 1
        fi
        echo "Verified historical job query on legacy bridge: job_id=${SAMPLE_JOB_ID} (HTTP 200)."
      fi
    fi

    echo "=== Step 3/3: Restore .env to bridge endpoint and recreate backend container (E16) ==="
    upsert_env_key "$REPO_ENV_FILE" "DALTON_TRANSCRIPTION_SERVICE_URL" "http://host.docker.internal:8960"
    upsert_env_key "$REPO_ENV_FILE" "DALTON_TRANSCRIPTION_TOKEN" "$BRIDGE_ADMIN_TOKEN"
    run_cmd bash -c "cd '$REPO_ROOT' && docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --no-deps backend"
    echo "Rollback completed."
    ;;
esac
