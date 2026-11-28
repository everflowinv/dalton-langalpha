#!/usr/bin/env bash
# Everflow production cutover & rollback helper for standalone dalton-transcription (:8965).
#
# Implements docs/dalton/features/researchdata-connectors.md §6.3 cutover & rollback sequence:
#   Switch:
#     0. Pre-check installed plist, two-sided jobs_dir & usage_file parity, repository venv
#        Python deps, enabled ASR adapter config + ffmpeg/ffprobe availability, and historical job sample.
#     1. Drain old bridge ASR (`POST /transcriptions/drain`), poll until `active_jobs == 0`.
#     2. Set `DALTON_BRIDGE_ASR_DISABLED=true` in bridge env, restart bridge, verify `.owner.lock` released & 503.
#     3. Start `com.dalton.transcription` (`:8965`) on identical `DALTON_ASR_JOBS_DIR` & `DALTON_ASR_USAGE_FILE`,
#        verify `/healthz` reports `status=ok`, `owner_lock_held=true`, `ffmpeg_available=true`,
#        `ffprobe_available=true`, verify auth with `DALTON_TRANSCRIPTION_TOKEN`,
#        and verify querying a completed historical job succeeds. Fail closed without touching `.env` or backend.
#     4. Update repo `.env` (`DALTON_TRANSCRIPTION_SERVICE_URL` + `DALTON_TRANSCRIPTION_TOKEN`) and recreate backend:
#        `docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --no-deps backend` (E16).
#   Rollback:
#     1. Check whether `com.dalton.transcription` LaunchAgent is running; while running, require `/healthz`
#        and drain (`active_jobs == 0`) to succeed BEFORE `launchctl bootout` (abort without bootout if
#        `/healthz` fails while process is running); fail fast on `bootout` error and verify `.owner.lock` released.
#     2. Set `DALTON_BRIDGE_ASR_DISABLED=false` in bridge env, restart bridge (failing fast with status snapshot
#        on `launchctl` error), verify bridge `/healthz` reports `asr.disabled: false` and `asr.owner_lock_held: true`,
#        and verify bridge auth passes.
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
BRIDGE_PLIST="${DALTON_BRIDGE_PLIST:-${LAUNCH_AGENTS_DIR}/${BRIDGE_LABEL}.plist}"
TRANSCRIPTION_PLIST="${DALTON_TRANSCRIPTION_PLIST:-${LAUNCH_AGENTS_DIR}/${TRANSCRIPTION_LABEL}.plist}"
DRAIN_POLL_INTERVAL_S="${DALTON_DRAIN_POLL_INTERVAL_S:-2}"
HEALTH_POLL_INTERVAL_S="${DALTON_HEALTH_POLL_INTERVAL_S:-1}"
HEALTH_POLL_ATTEMPTS="${DALTON_HEALTH_POLL_ATTEMPTS:-20}"
WAIT_SECONDS=120
DRY_RUN=0
ALLOW_EMPTY_HISTORY=0
ACTION=""

usage() {
  cat <<'EOF'
Usage: switch_transcription_service.sh <action> [options]

Actions (pick one):
  --check           Inspect bridge/service healthz, .owner.lock holder, two-sided path parity,
                    venv Python deps, and transcription adapter + ffmpeg/ffprobe readiness
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
  if ! "$py_bin" -c "import uvloop" 2>&1; then
    echo "ERROR: $py_bin failed dependency check (import uvloop)." >&2
    echo "       Sync repository dependencies (e.g. 'uv sync') so uvloop is installed in ${py_bin} before cutover." >&2
    return 1
  fi
  echo "PYTHON_DEPS:            OK (fastapi, uvicorn, uvloop, boto3, httpx)"
  return 0
}

check_transcription_runtime_config() {
  local py_bin
  py_bin="$(resolve_transcription_python)"
  if [[ ! -x "$py_bin" ]]; then
    echo "ERROR: Cannot verify transcription adapter config: Python binary not executable ($py_bin)." >&2
    return 1
  fi
  PYTHONPATH="${REPO_ROOT}:${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}" \
    "$py_bin" -m src.dalton.transcription_service.preflight \
    --check-runtime \
    --repo-root "$REPO_ROOT" \
    --plist "$TRANSCRIPTION_PLIST" \
    --env-file "$TRANSCRIPTION_ENV_FILE"
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

resolve_side_paths() {
  local side="$1"
  local plist_file="$2"
  local env_file="$3"
  PYTHONPATH="${REPO_ROOT}:${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}" \
    python3 -m src.dalton.transcription_service.preflight \
    --resolve-paths \
    --side "$side" \
    --repo-root "$REPO_ROOT" \
    --plist "$plist_file" \
    --env-file "$env_file" \
    --shell-jobs "${DALTON_ASR_JOBS_DIR:-}" \
    --shell-usage "${DALTON_ASR_USAGE_FILE:-}"
}

resolve_jobs_dir() {
  local side_lines
  side_lines="$(resolve_side_paths "transcription" "$TRANSCRIPTION_PLIST" "$TRANSCRIPTION_ENV_FILE")"
  echo "$side_lines" | sed -n '1p'
}

check_path_parity() {
  local b_out t_out b_jobs b_usage t_jobs t_usage
  b_out="$(resolve_side_paths "bridge" "$BRIDGE_PLIST" "$BRIDGE_ENV_FILE")"
  t_out="$(resolve_side_paths "transcription" "$TRANSCRIPTION_PLIST" "$TRANSCRIPTION_ENV_FILE")"
  b_jobs="$(printf '%s\n' "$b_out" | sed -n '1p')"
  b_usage="$(printf '%s\n' "$b_out" | sed -n '2p')"
  t_jobs="$(printf '%s\n' "$t_out" | sed -n '1p')"
  t_usage="$(printf '%s\n' "$t_out" | sed -n '2p')"

  local ok=1
  if [[ "$b_jobs" != "$t_jobs" ]]; then
    echo "ERROR: DALTON_ASR_JOBS_DIR mismatch between legacy bridge and standalone transcription service:" >&2
    echo "       legacy bridge:            $b_jobs" >&2
    echo "       standalone transcription: $t_jobs" >&2
    ok=0
  fi
  if [[ "$b_usage" != "$t_usage" ]]; then
    echo "ERROR: DALTON_ASR_USAGE_FILE mismatch between legacy bridge and standalone transcription service:" >&2
    echo "       legacy bridge:            $b_usage" >&2
    echo "       standalone transcription: $t_usage" >&2
    ok=0
  fi
  if [[ "$ok" -ne 1 ]]; then
    echo "       Align both LaunchAgent plists and env files so jobs dir and usage file match before cutover." >&2
    return 1
  fi
  echo "PATH_PARITY:            OK (jobs_dir=$t_jobs, usage_file=$t_usage)"
  return 0
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

launchagent_state() {
  local target="$1"
  local out=""
  if ! out="$(launchctl print "$target" 2>/dev/null)"; then
    echo "unloaded"
    return 0
  fi
  if printf '%s\n' "$out" | grep -qiE 'state[[:space:]]*=[[:space:]]*not running' \
    && ! printf '%s\n' "$out" | grep -qiE 'pid[[:space:]]*=[[:space:]]*[0-9]+'; then
    echo "loaded_stopped"
    return 0
  fi
  echo "running"
}

drain_and_wait() {
  local base_url="$1"
  local token="$2"
  local label="$3"
  if [[ -z "$token" ]]; then
    echo "ERROR: Missing admin/service token for $label ($base_url)" >&2
    return 1
  fi
  echo "[$label] Sending POST $base_url/transcriptions/drain ..."
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] curl -sf --max-time ${WAIT_SECONDS} -X POST -H 'Authorization: Bearer <redacted>' $base_url/transcriptions/drain"
    return 0
  fi
  local post_max_time="$WAIT_SECONDS"
  if [[ "$post_max_time" -gt 10 ]]; then
    post_max_time=10
  fi
  if [[ "$post_max_time" -lt 1 ]]; then
    post_max_time=1
  fi
  if ! curl -sf --max-time "$post_max_time" -X POST \
    -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json" \
    -d '{"drain":true}' \
    "${base_url}/transcriptions/drain"; then
    echo >&2
    echo "ERROR: [$label] Failed to initiate drain via POST ${base_url}/transcriptions/drain." >&2
    echo "       Re-run this script (${ACTION}) once $label is reachable, or cancel drain with:" >&2
    echo "       curl -sf -X POST -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' -d '{\"drain\":false}' ${base_url}/transcriptions/drain" >&2
    return 1
  fi
  echo

  local deadline=$(( $(date +%s) + WAIT_SECONDS ))
  local last_state="unknown"
  local polled_once=0
  while true; do
    local now remaining req_timeout
    now="$(date +%s)"
    remaining=$(( deadline - now ))
    if [[ "$polled_once" -eq 1 && "$remaining" -le 0 ]]; then
      echo "ERROR: Timed out after ${WAIT_SECONDS}s waiting for $label ready_for_handoff=true and active_jobs=0 (last state: ${last_state})." >&2
      echo "       $label remains in draining mode. To continue waiting, re-run this script (${ACTION});" >&2
      echo "       to cancel drain and resume intake, run:" >&2
      echo "       curl -sf -X POST -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' -d '{\"drain\":false}' ${base_url}/transcriptions/drain" >&2
      return 1
    fi
    req_timeout="$remaining"
    if [[ "$req_timeout" -gt 10 ]]; then
      req_timeout=10
    fi
    if [[ "$req_timeout" -lt 1 ]]; then
      req_timeout=1
    fi
    polled_once=1

    local resp=""
    if resp="$(curl -sf --max-time "$req_timeout" -H "Authorization: Bearer ${token}" "${base_url}/transcriptions/drain" 2>/dev/null)"; then
      local parsed=""
      if parsed="$(python3 - "$resp" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
if not isinstance(d, dict):
    sys.exit(1)
active = d.get("active_jobs")
ready = d.get("ready_for_handoff")
active_valid = isinstance(active, int) and not isinstance(active, bool)
active_str = str(active) if active_valid else "invalid"
ready_str = "true" if ready is True else ("false" if ready is False else "invalid")
is_ready = (ready is True and active_valid and active == 0)
print(f"{'READY' if is_ready else 'WAIT'} {ready_str} {active_str}")
PY
      )"; then
        local status_tok ready_flag active
        read -r status_tok ready_flag active <<< "$parsed"
        last_state="ready_for_handoff=${ready_flag}, active_jobs=${active}"
        echo "[$label] drain status: ready_for_handoff=${ready_flag}, active_jobs=${active}"
        if [[ "$status_tok" == "READY" ]]; then
          echo "[$label] ready for handoff (ready_for_handoff=true, active_jobs=0)."
          return 0
        fi
      else
        last_state="invalid_response"
        echo "[$label] WARNING: GET ${base_url}/transcriptions/drain returned non-JSON response; retrying until timeout..." >&2
      fi
    else
      last_state="poll_curl_failed"
      echo "[$label] WARNING: curl failed while polling ${base_url}/transcriptions/drain; retrying until timeout..." >&2
    fi
    if [[ "$(date +%s)" -ge "$deadline" ]]; then
      echo "ERROR: Timed out after ${WAIT_SECONDS}s waiting for $label ready_for_handoff=true and active_jobs=0 (last state: ${last_state})." >&2
      echo "       $label remains in draining mode. To continue waiting, re-run this script (${ACTION});" >&2
      echo "       to cancel drain and resume intake, run:" >&2
      echo "       curl -sf -X POST -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' -d '{\"drain\":false}' ${base_url}/transcriptions/drain" >&2
      return 1
    fi
    sleep "$DRAIN_POLL_INTERVAL_S"
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
  local b_out t_out b_jobs b_usage t_jobs t_usage lock_file
  b_out="$(resolve_side_paths "bridge" "$BRIDGE_PLIST" "$BRIDGE_ENV_FILE")"
  t_out="$(resolve_side_paths "transcription" "$TRANSCRIPTION_PLIST" "$TRANSCRIPTION_ENV_FILE")"
  b_jobs="$(printf '%s\n' "$b_out" | sed -n '1p')"
  b_usage="$(printf '%s\n' "$b_out" | sed -n '2p')"
  t_jobs="$(printf '%s\n' "$t_out" | sed -n '1p')"
  t_usage="$(printf '%s\n' "$t_out" | sed -n '2p')"
  lock_file="${t_jobs}/.owner.lock"
  echo "=== Paths ==="
  echo "REPO_ROOT:                    $REPO_ROOT"
  echo "BRIDGE_ASR_JOBS_DIR:          $b_jobs"
  echo "TRANSCRIPTION_ASR_JOBS_DIR:   $t_jobs"
  echo "BRIDGE_ASR_USAGE_FILE:        $b_usage"
  echo "TRANSCRIPTION_ASR_USAGE_FILE: $t_usage"
  echo "OWNER_LOCK_FILE:              $lock_file"
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
  echo "=== Recovery Commands (use --rollback to drain, wait for active_jobs=0, confirm lock release, and restore legacy bridge) ===" >&2
  cat >&2 <<EOF
  1. Run rollback so ${TRANSCRIPTION_LABEL} stops accepting new jobs, waits for in-flight jobs to finish, stops safely, and confirms .owner.lock release before re-enabling ${BRIDGE_LABEL}:
       bash scripts/dalton/ops/prod/switch_transcription_service.sh --rollback --wait-seconds ${WAIT_SECONDS}
  2. Verify post-rollback health and lock ownership:
       bash scripts/dalton/ops/prod/switch_transcription_service.sh --check
EOF
}

resolve_bridge_port() {
  local port
  port="$(read_env_key "$BRIDGE_ENV_FILE" DALTON_BRIDGE_PORT)"
  if [[ -n "$port" ]]; then
    echo "$port"
    return 0
  fi
  python3 - "$BRIDGE_URL" <<'PY'
from urllib.parse import urlparse
import sys
p = urlparse(sys.argv[1])
print(p.port or 8960)
PY
}

show_check() {
  local check_failed=0
  show_status_snapshot
  echo
  echo "=== Path Parity ==="
  if ! check_path_parity; then
    check_failed=1
  fi
  echo
  echo "=== Transcription Python Runtime ==="
  if ! check_transcription_python; then
    check_failed=1
  fi
  echo
  echo "=== Transcription Adapter & Binary Pre-Check ==="
  if ! check_transcription_runtime_config; then
    check_failed=1
  fi
  if [[ "$check_failed" -ne 0 ]]; then
    exit 1
  fi
}

BRIDGE_ADMIN_TOKEN="${DALTON_BRIDGE_ADMIN_TOKEN:-$(read_env_key "$BRIDGE_ENV_FILE" DALTON_BRIDGE_ADMIN_TOKEN)}"
TRANSCRIPTION_TOKEN="${DALTON_TRANSCRIPTION_TOKEN:-$(read_env_key "$TRANSCRIPTION_ENV_FILE" DALTON_TRANSCRIPTION_TOKEN)}"
GUI_DOMAIN="gui/$(id -u)"

case "$ACTION" in
  --check)
    show_check
    ;;
  --drain-bridge)
    drain_and_wait "$BRIDGE_URL" "$BRIDGE_ADMIN_TOKEN" "researchdata-bridge" || exit 1
    ;;
  --drain-service)
    drain_and_wait "$TRANSCRIPTION_URL" "$TRANSCRIPTION_TOKEN" "dalton-transcription" || exit 1
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
      check_path_parity
      check_transcription_python
      check_transcription_runtime_config
      if [[ -z "$SAMPLE_JOB_ID" && "$ALLOW_EMPTY_HISTORY" -eq 0 ]]; then
        echo "ERROR: No completed historical job (status=succeeded) found under ${JOBS_DIR}." >&2
        echo "       Verify DALTON_ASR_JOBS_DIR parity before cutover, or pass --allow-empty-history for a brand-new directory." >&2
        exit 1
      fi
    fi

    echo "=== Step 1/4: Drain legacy bridge ASR ==="
    drain_and_wait "$BRIDGE_URL" "$BRIDGE_ADMIN_TOKEN" "researchdata-bridge" || exit 1

    echo "=== Step 2/4: Disable legacy bridge ASR and restart bridge to release .owner.lock ==="
    upsert_env_key "$BRIDGE_ENV_FILE" "DALTON_BRIDGE_ASR_DISABLED" "true"
    if ! run_cmd launchctl kickstart -k "${GUI_DOMAIN}/${BRIDGE_LABEL}"; then
      echo "ERROR: launchctl kickstart failed for ${GUI_DOMAIN}/${BRIDGE_LABEL} after writing DALTON_BRIDGE_ASR_DISABLED=true." >&2
      print_switch_failure_recovery
      exit 1
    fi
    if [[ "$DRY_RUN" -eq 0 ]]; then
      bridge_disabled_ok=0
      for _ in $(seq 1 "$HEALTH_POLL_ATTEMPTS"); do
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
        sleep "$HEALTH_POLL_INTERVAL_S"
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
    ts_agent_state="$(launchagent_state "${GUI_DOMAIN}/${TRANSCRIPTION_LABEL}")"
    ts_already_running=0
    if [[ "$ts_agent_state" == "running" ]]; then
      ts_already_running=1
      echo "${TRANSCRIPTION_LABEL} is already loaded and running; verifying /healthz, auth, and historical job without restarting."
    elif [[ "$ts_agent_state" == "loaded_stopped" ]]; then
      if ! run_cmd launchctl kickstart "${GUI_DOMAIN}/${TRANSCRIPTION_LABEL}"; then
        echo "ERROR: launchctl kickstart failed for ${GUI_DOMAIN}/${TRANSCRIPTION_LABEL}." >&2
        print_switch_failure_recovery
        exit 1
      fi
    else
      if ! run_cmd launchctl bootstrap "${GUI_DOMAIN}" "${TRANSCRIPTION_PLIST}"; then
        echo "ERROR: launchctl bootstrap failed for ${GUI_DOMAIN} (${TRANSCRIPTION_PLIST})." >&2
        print_switch_failure_recovery
        exit 1
      fi
    fi
    if [[ "$DRY_RUN" -eq 0 ]]; then
      service_lock_ok=0
      last_hz=""
      for _ in $(seq 1 "$HEALTH_POLL_ATTEMPTS"); do
        if hz="$(curl -sf "${TRANSCRIPTION_URL}/healthz" 2>/dev/null)"; then
          last_hz="$hz"
          if python3 - "$hz" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
ok = (
    d.get("owner_lock_held") is True
    and d.get("disabled") is False
    and d.get("misconfigured") is not True
    and d.get("status") == "ok"
    and d.get("ffmpeg_available") is True
    and d.get("ffprobe_available") is True
    and d.get("worker_name") not in (None, "", "none", "unconfigured")
)
sys.exit(0 if ok else 1)
PY
          then
            service_lock_ok=1
            echo "dalton-transcription is healthy and holds .owner.lock: $hz"
            break
          fi
        fi
        sleep "$HEALTH_POLL_INTERVAL_S"
      done
      if [[ "$service_lock_ok" -ne 1 ]]; then
        echo "ERROR: dalton-transcription did not report status=ok, configured worker, ffmpeg/ffprobe available, and owner_lock_held=true (last healthz: ${last_hz:-unreachable})." >&2
        if [[ "$ts_already_running" -eq 1 ]]; then
          echo "ERROR: ${TRANSCRIPTION_LABEL} is already running and failed verification; refusing to restart via kickstart -k. Run --rollback first." >&2
        fi
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
        if [[ "$ts_already_running" -eq 1 ]]; then
          echo "ERROR: ${TRANSCRIPTION_LABEL} is already running and failed auth verification; refusing to restart via kickstart -k. Run --rollback first." >&2
        fi
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
          if [[ "$ts_already_running" -eq 1 ]]; then
            echo "ERROR: ${TRANSCRIPTION_LABEL} is already running and failed historical job verification; refusing to restart via kickstart -k. Run --rollback first." >&2
          fi
          print_switch_failure_recovery
          exit 1
        fi
        echo "Verified historical completed job query on dalton-transcription: job_id=${SAMPLE_JOB_ID} (status=succeeded)."
      elif [[ "$ALLOW_EMPTY_HISTORY" -eq 1 ]]; then
        echo "Skipping historical job read check (--allow-empty-history set and no succeeded jobs in ${JOBS_DIR})."
      else
        echo "ERROR: No completed historical job found in ${JOBS_DIR} to verify dalton-transcription storage read." >&2
        if [[ "$ts_already_running" -eq 1 ]]; then
          echo "ERROR: ${TRANSCRIPTION_LABEL} is already running and failed historical job verification; refusing to restart via kickstart -k. Run --rollback first." >&2
        fi
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
    ts_agent_state="$(launchagent_state "${GUI_DOMAIN}/${TRANSCRIPTION_LABEL}")"
    if [[ "$ts_agent_state" == "running" ]]; then
      if [[ "$DRY_RUN" -eq 0 ]]; then
        if ! ts_hz="$(curl -sf "${TRANSCRIPTION_URL}/healthz" 2>/dev/null)"; then
          echo "ERROR: LaunchAgent ${TRANSCRIPTION_LABEL} is still running, but ${TRANSCRIPTION_URL}/healthz failed; refusing to bootout while in-flight jobs cannot be verified." >&2
          show_status_snapshot >&2
          exit 1
        fi
        ts_skip_drain="$(python3 - "$ts_hz" <<'PY'
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    print("INVALID")
    sys.exit(0)
if not isinstance(d, dict):
    print("INVALID")
    sys.exit(0)
disabled = d.get("disabled") is True
misconfigured_idle = (
    d.get("misconfigured") is True
    and d.get("owner_lock_held") is False
    and d.get("active_jobs", -1) == 0
)
print("SKIP" if (disabled or misconfigured_idle) else "DRAIN")
PY
)"
        if [[ "$ts_skip_drain" == "INVALID" ]]; then
          echo "ERROR: LaunchAgent ${TRANSCRIPTION_LABEL} returned invalid JSON on ${TRANSCRIPTION_URL}/healthz; refusing to bootout." >&2
          show_status_snapshot >&2
          exit 1
        fi
        if [[ "$ts_skip_drain" == "DRAIN" ]]; then
          if ! drain_and_wait "$TRANSCRIPTION_URL" "$TRANSCRIPTION_TOKEN" "dalton-transcription"; then
            show_status_snapshot >&2
            exit 1
          fi
        fi
      else
        drain_and_wait "$TRANSCRIPTION_URL" "$TRANSCRIPTION_TOKEN" "dalton-transcription"
      fi
      if ! run_cmd launchctl bootout "${GUI_DOMAIN}/${TRANSCRIPTION_LABEL}"; then
        echo "ERROR: launchctl bootout failed for ${GUI_DOMAIN}/${TRANSCRIPTION_LABEL}; aborting rollback before modifying .env or backend." >&2
        show_status_snapshot >&2
        exit 1
      fi
    elif [[ "$ts_agent_state" == "loaded_stopped" ]]; then
      if [[ "$DRY_RUN" -eq 0 ]] && ts_hz="$(curl -sf "${TRANSCRIPTION_URL}/healthz" 2>/dev/null)"; then
        ts_disabled="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("disabled", False))' "$ts_hz" 2>/dev/null || echo "False")"
        if [[ "$ts_disabled" != "True" ]]; then
          if ! drain_and_wait "$TRANSCRIPTION_URL" "$TRANSCRIPTION_TOKEN" "dalton-transcription"; then
            show_status_snapshot >&2
            exit 1
          fi
        fi
      fi
      if ! run_cmd launchctl bootout "${GUI_DOMAIN}/${TRANSCRIPTION_LABEL}"; then
        echo "ERROR: launchctl bootout failed for ${GUI_DOMAIN}/${TRANSCRIPTION_LABEL}; aborting rollback before modifying .env or backend." >&2
        show_status_snapshot >&2
        exit 1
      fi
    else
      if [[ "$DRY_RUN" -eq 0 ]] && ts_hz="$(curl -sf "${TRANSCRIPTION_URL}/healthz" 2>/dev/null)"; then
        ts_disabled="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("disabled", False))' "$ts_hz" 2>/dev/null || echo "False")"
        if [[ "$ts_disabled" != "True" ]]; then
          if ! drain_and_wait "$TRANSCRIPTION_URL" "$TRANSCRIPTION_TOKEN" "dalton-transcription"; then
            show_status_snapshot >&2
            exit 1
          fi
        fi
      fi
    fi

    if [[ "$DRY_RUN" -eq 0 ]]; then
      service_stopped_ok=0
      for _ in $(seq 1 "$HEALTH_POLL_ATTEMPTS"); do
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
        sleep "$HEALTH_POLL_INTERVAL_S"
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
    if ! run_cmd launchctl kickstart -k "${GUI_DOMAIN}/${BRIDGE_LABEL}"; then
      echo "ERROR: launchctl kickstart failed for ${GUI_DOMAIN}/${BRIDGE_LABEL}; repo .env and backend container were left unchanged." >&2
      show_status_snapshot >&2
      exit 1
    fi
    if [[ "$DRY_RUN" -eq 0 ]]; then
      bridge_ready_ok=0
      last_bhz=""
      for _ in $(seq 1 "$HEALTH_POLL_ATTEMPTS"); do
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
        sleep "$HEALTH_POLL_INTERVAL_S"
      done
      if [[ "$bridge_ready_ok" -ne 1 ]]; then
        echo "ERROR: Legacy bridge did not report asr.disabled=false and asr.owner_lock_held=true (last healthz: ${last_bhz:-unreachable})." >&2
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
    BRIDGE_PORT="$(resolve_bridge_port)"
    upsert_env_key "$REPO_ENV_FILE" "DALTON_TRANSCRIPTION_SERVICE_URL" "http://host.docker.internal:${BRIDGE_PORT}"
    upsert_env_key "$REPO_ENV_FILE" "DALTON_TRANSCRIPTION_TOKEN" "$BRIDGE_ADMIN_TOKEN"
    run_cmd bash -c "cd '$REPO_ROOT' && docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --no-deps backend"
    echo "Rollback completed."
    ;;
esac
