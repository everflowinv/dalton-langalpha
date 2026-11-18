#!/usr/bin/env bash
# Plan, apply, rerender, and rollback migration of host research services
# (alphaengine-mcp, alphaengine-desktop-watchdog, guidepoint-mcp-proxy)
# from OpenClaw launchd labels to Dalton launchd labels.
# Follows the active mode (legacy DALTON_OCW_ROOT vs new DALTON_RESEARCH_HOME)
# configured in the bridge env file. Default mode is --plan (read-only).
# Compatible with macOS Bash 3.2.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${DALTON_REPO_ROOT:-$(cd "${SCRIPT_DIR}/../../.." && pwd)}"

BRIDGE_ENV_FILE="${DALTON_BRIDGE_ENV_FILE:-${HOME}/.config/dalton/researchdata-bridge.env}"
LAUNCH_AGENTS_DIR="${DALTON_LAUNCH_AGENTS_DIR:-${HOME}/Library/LaunchAgents}"
LOGS_DIR="${DALTON_LOGS_DIR:-${HOME}/Library/Logs}"
NODE_BIN="${DALTON_NODE_BIN:-/opt/homebrew/bin/node}"
PYTHON_BIN="${GUIDEPOINT_PYTHON:-/opt/homebrew/bin/python3.12}"

LAUNCHCTL_CMD="${LAUNCHCTL_BIN:-launchctl}"
LSOF_CMD="${LSOF_BIN:-lsof}"
CURL_CMD="${CURL_BIN:-curl}"
PLUTIL_CMD="${PLUTIL_BIN:-plutil}"
UID_NUM="${DALTON_LAUNCHD_UID:-$(id -u)}"
PORT_RELEASE_TIMEOUT_S="${HOST_SERVICES_PORT_RELEASE_TIMEOUT_S:-15}"
HEALTH_TIMEOUT_S="${HOST_SERVICES_HEALTH_TIMEOUT_S:-60}"
HEALTH_INTERVAL_S="${HOST_SERVICES_HEALTH_INTERVAL_S:-1}"

ALPHAENGINE_PORT="${ALPHAENGINE_MCP_PORT:-8950}"
GUIDEPOINT_PORT="${GUIDEPOINT_PROXY_PORT:-8943}"

MODE="plan"
RERENDER_ONLY=0
TMP_ARTIFACTS=()

cleanup() {
  local rc=$?
  if [[ "${#TMP_ARTIFACTS[@]}" -gt 0 ]]; then
    local f
    for f in "${TMP_ARTIFACTS[@]}"; do
      [[ -n "${f}" && -e "${f}" ]] && rm -f "${f}" 2>/dev/null || true
    done
  fi
  return "${rc}"
}
trap cleanup EXIT INT TERM

usage() {
  cat <<'EOF'
Usage: host_services_switch.sh [--plan | --apply [--rerender-only] | --rollback]

Options:
  --plan           Print detected mode, service paths, switch steps, and launchd status (default).
  --apply          Render/lint plists and switch services sequentially (stop old, verify port release, disable old, enable/bootstrap new, health+PID check).
  --rerender-only  With --apply (or standalone): rerender and restart only already-loaded com.dalton.* services; restore pre-switch plists on failure.
  --rollback       Switch all three services back to ai.openclaw.* labels with full return-code and health checks.
  -h, --help       Show this help message.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan)
      MODE="plan"
      shift
      ;;
    --apply)
      MODE="apply"
      shift
      ;;
    --rerender-only)
      RERENDER_ONLY=1
      shift
      ;;
    --rollback)
      MODE="rollback"
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

if [[ "${RERENDER_ONLY}" -eq 1 && "${MODE}" == "plan" ]]; then
  MODE="apply"
elif [[ "${RERENDER_ONLY}" -eq 1 && "${MODE}" == "rollback" ]]; then
  echo "ERROR: --rerender-only cannot be combined with --rollback" >&2
  exit 2
fi

SERVICES=(
  "alphaengine-mcp|ai.openclaw.alphaengine-mcp|com.dalton.alphaengine-mcp|${ALPHAENGINE_PORT}"
  "alphaengine-desktop-watchdog|ai.openclaw.alphaengine-desktop-watchdog|com.dalton.alphaengine-desktop-watchdog|-"
  "guidepoint-mcp-proxy|ai.openclaw.guidepoint-mcp-proxy|com.dalton.guidepoint-mcp-proxy|${GUIDEPOINT_PORT}"
)

resolve_helper_python() {
  if [[ -x "${PYTHON_BIN}" ]]; then
    printf '%s\n' "${PYTHON_BIN}"
  elif [[ -x "/opt/homebrew/bin/python3" ]]; then
    printf '%s\n' "/opt/homebrew/bin/python3"
  else
    command -v python3
  fi
}

# Resolve mode and data root using the exact same rule as researchdata_bridge.py:
# read ONLY DALTON_RESEARCH_HOME, DALTON_RESEARCH_CODE, DALTON_OCW_ROOT from BRIDGE_ENV_FILE
# (plus process environment overrides for those 3 keys). Never read or print any other key.
RUNTIME_MODE=""
DATA_ROOT=""
RUNTIME_ENV_KEY=""
RUNTIME_ENV_VAL=""
BRIDGE_ENV_STATUS=""

resolve_runtime_context() {
  local helper_py
  helper_py="$(resolve_helper_python)"
  local resolved_lines
  resolved_lines="$("${helper_py}" - "${BRIDGE_ENV_FILE}" "${HOME}" <<'PY'
import os
import sys
from pathlib import Path

env_path = Path(sys.argv[1]).expanduser()
home_dir = sys.argv[2]
allowed_keys = ("DALTON_RESEARCH_HOME", "DALTON_RESEARCH_CODE", "DALTON_OCW_ROOT")
vals: dict[str, str] = {}
env_status = "missing"

try:
    if env_path.is_file():
        env_status = "ok"
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k not in allowed_keys:
                continue
            v = v.strip().strip("'").strip('"').strip()
            if v:
                vals[k] = v
except OSError:
    env_status = "unreadable"

for k in allowed_keys:
    env_v = os.environ.get(k, "").strip()
    if env_v:
        vals[k] = env_v

raw_home = vals.get("DALTON_RESEARCH_HOME", "")
raw_code = vals.get("DALTON_RESEARCH_CODE", "")
raw_ocw = vals.get("DALTON_OCW_ROOT", "")

if raw_ocw and not raw_home and not raw_code:
    data_root = os.path.abspath(os.path.expanduser(raw_ocw))
    print("legacy")
    print(data_root)
    print("DALTON_OCW_ROOT")
    print(data_root)
else:
    if raw_home:
        data_root = os.path.abspath(os.path.expanduser(raw_home))
    else:
        data_root = os.path.abspath(os.path.join(home_dir, "Library", "Application Support", "Everflow", "research"))
    print("new")
    print(data_root)
    print("DALTON_RESEARCH_HOME")
    print(data_root)
print(env_status)
PY
)"
  RUNTIME_MODE="$(printf '%s\n' "${resolved_lines}" | sed -n '1p')"
  DATA_ROOT="$(printf '%s\n' "${resolved_lines}" | sed -n '2p')"
  RUNTIME_ENV_KEY="$(printf '%s\n' "${resolved_lines}" | sed -n '3p')"
  RUNTIME_ENV_VAL="$(printf '%s\n' "${resolved_lines}" | sed -n '4p')"
  BRIDGE_ENV_STATUS="$(printf '%s\n' "${resolved_lines}" | sed -n '5p')"
}

resolve_runtime_context

GUIDEPOINT_ENV_PATH="${GUIDEPOINT_ENV_FILE:-${DATA_ROOT}/config/guidepoint_mcp.env}"
GUIDEPOINT_TOKENS_PATH="${GUIDEPOINT_TOKENS_FILE:-${DATA_ROOT}/config/guidepoint_mcp_tokens.json}"
WATCHDOG_STATE_FILE="${ALPHAENGINE_WATCHDOG_STATE_FILE:-${DATA_ROOT}/state/alphaengine-desktop-watchdog.json}"
ALPHAENGINE_DOWNLOADS_DIR="${DATA_ROOT}/temp/alphaengine-mcp-downloads"

file_size_bytes() {
  local p="$1"
  if [[ ! -f "${p}" ]]; then
    printf '%s' "missing"
    return 0
  fi
  local sz=""
  sz="$(stat -f%z "${p}" 2>/dev/null || stat -c%s "${p}" 2>/dev/null || wc -c < "${p}" 2>/dev/null | tr -d '[:space:]')"
  printf '%s bytes' "${sz:-0}"
}

launchd_label_loaded() {
  local label="$1"
  "${LAUNCHCTL_CMD}" print "gui/${UID_NUM}/${label}" >/dev/null 2>&1
}

launchd_label_status() {
  local label="$1"
  if launchd_label_loaded "${label}"; then
    printf '%s' "loaded"
  else
    printf '%s' "not_loaded"
  fi
}

get_launchd_pid() {
  local label="$1"
  local print_out
  print_out="$("${LAUNCHCTL_CMD}" print "gui/${UID_NUM}/${label}" 2>/dev/null)" || return 1
  printf '%s\n' "${print_out}" | awk -F'=' '/^[[:space:]]*pid[[:space:]]*=/{gsub(/[^0-9]/, "", $2); if ($2 != "") { print $2; exit }}'
}

get_listening_pids() {
  local port="$1"
  local lsof_out
  lsof_out="$("${LSOF_CMD}" -nP -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -z "${lsof_out}" ]]; then
    return 0
  fi
  printf '%s\n' "${lsof_out}" | awk '{if ($2 ~ /^[0-9]+$/) print $2; else if ($1 ~ /^[0-9]+$/) print $1}'
}

port_is_released() {
  local port="$1"
  if [[ "${port}" == "-" ]]; then
    return 0
  fi
  local lsof_out
  lsof_out="$("${LSOF_CMD}" -nP -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true)"
  [[ -z "$(printf '%s' "${lsof_out}" | tr -d '[:space:]')" ]]
}

wait_unloaded_and_port_released() {
  local label="$1"
  local port="$2"
  local helper_py
  helper_py="$(resolve_helper_python)"
  local deadline_epoch now_epoch
  now_epoch="$(date +%s)"
  deadline_epoch=$((now_epoch + PORT_RELEASE_TIMEOUT_S))
  while true; do
    if ! launchd_label_loaded "${label}" && port_is_released "${port}"; then
      return 0
    fi
    now_epoch="$(date +%s)"
    if [[ "${now_epoch}" -ge "${deadline_epoch}" ]]; then
      return 1
    fi
    "${helper_py}" -c "import time; time.sleep(${HEALTH_INTERVAL_S})" 2>/dev/null || sleep 1
  done
}

render_plist_template() {
  local template_path="$1"
  local dest_path="$2"
  local helper_py
  helper_py="$(resolve_helper_python)"
  local tmp_path="${dest_path}.tmp.$$"
  TMP_ARTIFACTS+=("${tmp_path}")
  if ! "${helper_py}" - "${template_path}" "${tmp_path}" \
    "${REPO_ROOT}" "${HOME}" "${LOGS_DIR}" "${RUNTIME_ENV_KEY}" "${RUNTIME_ENV_VAL}" \
    "${NODE_BIN}" "${PYTHON_BIN}" "${GUIDEPOINT_ENV_PATH}" "${GUIDEPOINT_TOKENS_PATH}" <<'PY'
import sys
from xml.sax.saxutils import escape

(
    tpl_path,
    out_path,
    repo,
    home,
    log_dir,
    rt_key,
    rt_val,
    node_bin,
    py_bin,
    gp_env,
    gp_tokens,
) = sys.argv[1:12]

with open(tpl_path, "r", encoding="utf-8") as f:
    text = f.read()

replacements = {
    "__REPO__": escape(repo),
    "__HOME__": escape(home),
    "__LOG_DIR__": escape(log_dir),
    "__RUNTIME_ENV_KEY__": escape(rt_key),
    "__RUNTIME_ENV_VAL__": escape(rt_val),
    "__NODE_BIN__": escape(node_bin),
    "__PYTHON_BIN__": escape(py_bin),
    "__GUIDEPOINT_ENV_FILE__": escape(gp_env),
    "__GUIDEPOINT_TOKENS_FILE__": escape(gp_tokens),
}
for k, v in replacements.items():
    text = text.replace(k, v)

with open(out_path, "w", encoding="utf-8") as f:
    f.write(text)
PY
  then
    echo "[error] rendering ${template_path} failed" >&2
    return 1
  fi
  chmod 0644 "${tmp_path}" && mv -f "${tmp_path}" "${dest_path}" || return 1
}

resolve_watchdog_state_file_for_plist() {
  local plist_path="$1"
  if [[ -n "${ALPHAENGINE_WATCHDOG_STATE_FILE:-}" ]]; then
    printf '%s\n' "${ALPHAENGINE_WATCHDOG_STATE_FILE}"
    return 0
  fi
  if [[ ! -f "${plist_path}" ]]; then
    printf '%s\n' "${WATCHDOG_STATE_FILE}"
    return 0
  fi
  local helper_py
  helper_py="$(resolve_helper_python)"
  "${helper_py}" - "${plist_path}" "${HOME}" "${WATCHDOG_STATE_FILE}" <<'PY'
import os
import plistlib
import sys
from pathlib import Path

plist_path = Path(sys.argv[1])
home_dir = sys.argv[2]
fallback = sys.argv[3]

try:
    data = plistlib.loads(plist_path.read_bytes())
except Exception:
    print(fallback)
    sys.exit(0)

env_vars = data.get("EnvironmentVariables") if isinstance(data, dict) else None
if not isinstance(env_vars, dict):
    print(fallback)
    sys.exit(0)

state_dir = str(env_vars.get("ALPHAENGINE_WATCHDOG_STATE_DIR") or "").strip()
if state_dir:
    if state_dir == "~" or state_dir.startswith("~/"):
        state_dir = os.path.join(home_dir, state_dir[2:]) if state_dir != "~" else home_dir
    print(os.path.join(os.path.abspath(os.path.expanduser(state_dir)), "alphaengine-desktop-watchdog.json"))
    sys.exit(0)

root = ""
for key in ("DALTON_RESEARCH_HOME", "DALTON_OCW_ROOT", "OPENCLAW_WORKSPACE"):
    val = str(env_vars.get(key) or "").strip()
    if val:
        root = val
        break

if not root:
    print(fallback)
    sys.exit(0)

if root == "~":
    root_expanded = home_dir
elif root.startswith("~/"):
    root_expanded = os.path.join(home_dir, root[2:])
else:
    root_expanded = os.path.expanduser(root)
print(os.path.join(os.path.abspath(root_expanded), "state", "alphaengine-desktop-watchdog.json"))
PY
}

check_service_health() {
  local name="$1"
  local label="$2"
  local port="$3"
  local switch_start_utc="$4"
  local state_file_override="${5:-}"
  local helper_py
  helper_py="$(resolve_helper_python)"

  case "${name}" in
    alphaengine-mcp)
      if ! "${CURL_CMD}" -fsS --max-time 3 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
        return 1
      fi
      ;;
    guidepoint-mcp-proxy)
      local rc=0 code=""
      code="$("${CURL_CMD}" -s --max-time 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/mcp" 2>/dev/null)" || rc=$?
      if [[ "${rc}" -ne 0 ]] || [[ -n "${code}" && "${code}" != "200" && "${code}" != "405" ]]; then
        return 1
      fi
      ;;
    alphaengine-desktop-watchdog)
      local target_state_file="${state_file_override:-${WATCHDOG_STATE_FILE}}"
      if [[ ! -s "${target_state_file}" ]]; then
        return 1
      fi
      "${helper_py}" - "${target_state_file}" "${switch_start_utc}" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

state_path = Path(sys.argv[1])
start_utc = sys.argv[2]

try:
    data = json.loads(state_path.read_text(encoding="utf-8"))
except Exception:
    sys.exit(1)

checked_at = str(data.get("checked_at") or "").strip()
if not checked_at:
    sys.exit(1)

def parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)

try:
    dt_checked = parse_iso(checked_at)
    dt_start = parse_iso(start_utc)
except Exception:
    sys.exit(1)

sys.exit(0 if dt_checked > dt_start else 1)
PY
      return $?
      ;;
    *)
      return 1
      ;;
  esac

  # For port-listening services, also verify the listening PID matches launchctl print's PID.
  local launchd_pid="" listen_pids="" pid_item=""
  launchd_pid="$(get_launchd_pid "${label}" 2>/dev/null || true)"
  if [[ -z "${launchd_pid}" ]]; then
    return 1
  fi
  listen_pids="$(get_listening_pids "${port}")"
  for pid_item in ${listen_pids}; do
    if [[ "${pid_item}" == "${launchd_pid}" ]]; then
      return 0
    fi
  done
  return 1
}

wait_for_service_health() {
  local name="$1"
  local label="$2"
  local port="$3"
  local switch_start_utc="$4"
  local state_file_override="${5:-}"
  local helper_py
  helper_py="$(resolve_helper_python)"
  local deadline_epoch now_epoch
  now_epoch="$(date +%s)"
  deadline_epoch=$((now_epoch + HEALTH_TIMEOUT_S))
  while true; do
    if check_service_health "${name}" "${label}" "${port}" "${switch_start_utc}" "${state_file_override}"; then
      return 0
    fi
    now_epoch="$(date +%s)"
    if [[ "${now_epoch}" -ge "${deadline_epoch}" ]]; then
      return 1
    fi
    "${helper_py}" -c "import time; time.sleep(${HEALTH_INTERVAL_S})" 2>/dev/null || sleep 1
  done
}

wait_watchdog_process_exited() {
  local label="$1"
  local deadline_epoch="$2"
  local helper_py
  helper_py="$(resolve_helper_python)"
  local wd_pid="" now_epoch
  while true; do
    wd_pid="$(get_launchd_pid "${label}" 2>/dev/null || true)"
    if [[ -z "${wd_pid}" ]]; then
      return 0
    fi
    now_epoch="$(date +%s)"
    if [[ "${now_epoch}" -ge "${deadline_epoch}" ]]; then
      return 1
    fi
    "${helper_py}" -c "import time; time.sleep(${HEALTH_INTERVAL_S})" 2>/dev/null || sleep 1
  done
}

start_and_verify_watchdog() {
  local label="$1"
  local plist_path="$2"
  local start_utc="$3"
  local do_bootstrap="$4"  # 1 = bootstrap (RunAtLoad), 0 = already loaded
  local helper_py
  helper_py="$(resolve_helper_python)"
  local state_file
  state_file="$(resolve_watchdog_state_file_for_plist "${plist_path}")"

  local now_epoch deadline_epoch run_at_load_deadline wd_pid=""
  now_epoch="$(date +%s)"
  deadline_epoch=$((now_epoch + HEALTH_TIMEOUT_S))

  if [[ "${do_bootstrap}" -eq 1 ]]; then
    if ! "${LAUNCHCTL_CMD}" bootstrap "gui/${UID_NUM}" "${plist_path}"; then
      echo "[error] alphaengine-desktop-watchdog: bootstrap ${plist_path} failed" >&2
      return 1
    fi
    # Wait for RunAtLoad run to finish before falling back to kickstart (never kickstart -k).
    local run_at_load_budget=$(( HEALTH_TIMEOUT_S / 2 ))
    if [[ "${run_at_load_budget}" -lt 1 ]]; then
      run_at_load_budget=1
    fi
    run_at_load_deadline=$((now_epoch + run_at_load_budget))
    while true; do
      if check_service_health "alphaengine-desktop-watchdog" "${label}" "-" "${start_utc}" "${state_file}"; then
        return 0
      fi
      wd_pid="$(get_launchd_pid "${label}" 2>/dev/null || true)"
      now_epoch="$(date +%s)"
      if [[ -z "${wd_pid}" ]]; then
        # RunAtLoad already exited (or did not start) without writing a fresh state.
        break
      fi
      if [[ "${now_epoch}" -ge "${run_at_load_deadline}" ]]; then
        break
      fi
      "${helper_py}" -c "import time; time.sleep(${HEALTH_INTERVAL_S})" 2>/dev/null || sleep 1
    done
  fi

  # Wait for any active watchdog process to exit before invoking kickstart (without -k).
  if ! wait_watchdog_process_exited "${label}" "${deadline_epoch}"; then
    # Process stayed running past wait budget; check one last time if it just finished writing state.
    if check_service_health "alphaengine-desktop-watchdog" "${label}" "-" "${start_utc}" "${state_file}"; then
      return 0
    fi
    echo "[error] alphaengine-desktop-watchdog: active process for ${label} did not exit before kickstart" >&2
    return 1
  fi

  if check_service_health "alphaengine-desktop-watchdog" "${label}" "-" "${start_utc}" "${state_file}"; then
    return 0
  fi

  if ! "${LAUNCHCTL_CMD}" kickstart "gui/${UID_NUM}/${label}"; then
    echo "[error] alphaengine-desktop-watchdog: kickstart ${label} failed" >&2
    return 1
  fi

  while true; do
    if check_service_health "alphaengine-desktop-watchdog" "${label}" "-" "${start_utc}" "${state_file}"; then
      return 0
    fi
    now_epoch="$(date +%s)"
    if [[ "${now_epoch}" -ge "${deadline_epoch}" ]]; then
      echo "[error] alphaengine-desktop-watchdog: health check failed for ${label} (state=${state_file})" >&2
      return 1
    fi
    "${helper_py}" -c "import time; time.sleep(${HEALTH_INTERVAL_S})" 2>/dev/null || sleep 1
  done
}

restore_old_service_in_place() {
  local name="$1"
  local old_label="$2"
  local port="$3"
  local old_plist="${LAUNCH_AGENTS_DIR}/${old_label}.plist"
  local start_utc
  start_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  if ! "${LAUNCHCTL_CMD}" enable "gui/${UID_NUM}/${old_label}"; then
    echo "[error] ${name}: failed to enable ${old_label}" >&2
    return 1
  fi
  if [[ "${name}" == "alphaengine-desktop-watchdog" ]]; then
    sleep 1
    if launchd_label_loaded "${old_label}"; then
      start_and_verify_watchdog "${old_label}" "${old_plist}" "${start_utc}" 0 || return 1
    else
      if [[ ! -f "${old_plist}" ]]; then
        echo "[error] ${name}: missing legacy plist ${old_plist}" >&2
        return 1
      fi
      start_and_verify_watchdog "${old_label}" "${old_plist}" "${start_utc}" 1 || return 1
    fi
    return 0
  fi

  if launchd_label_loaded "${old_label}"; then
    if ! "${LAUNCHCTL_CMD}" kickstart -k "gui/${UID_NUM}/${old_label}"; then
      echo "[error] ${name}: failed to kickstart ${old_label}" >&2
      return 1
    fi
  else
    if [[ ! -f "${old_plist}" ]]; then
      echo "[error] ${name}: missing legacy plist ${old_plist}" >&2
      return 1
    fi
    if ! "${LAUNCHCTL_CMD}" bootstrap "gui/${UID_NUM}" "${old_plist}"; then
      echo "[error] ${name}: failed to bootstrap ${old_plist}" >&2
      return 1
    fi
    if ! "${LAUNCHCTL_CMD}" kickstart -k "gui/${UID_NUM}/${old_label}"; then
      echo "[error] ${name}: failed to kickstart ${old_label}" >&2
      return 1
    fi
  fi
  if ! wait_for_service_health "${name}" "${old_label}" "${port}" "${start_utc}"; then
    echo "[error] ${name}: health check failed after restoring ${old_label}" >&2
    return 1
  fi
  return 0
}

rollback_one_service() {
  local name="$1"
  local old_label="$2"
  local new_label="$3"
  local port="$4"
  local old_plist="${LAUNCH_AGENTS_DIR}/${old_label}.plist"
  local start_utc=""

  if launchd_label_loaded "${new_label}"; then
    if ! "${LAUNCHCTL_CMD}" bootout "gui/${UID_NUM}/${new_label}"; then
      echo "[error] ${name}: bootout ${new_label} failed" >&2
      return 1
    fi
    if ! wait_unloaded_and_port_released "${new_label}" "${port}"; then
      echo "[error] ${name}: ${new_label} did not unload or release port ${port} within ${PORT_RELEASE_TIMEOUT_S}s" >&2
      return 1
    fi
  fi

  if ! "${LAUNCHCTL_CMD}" disable "gui/${UID_NUM}/${new_label}"; then
    echo "[error] ${name}: disable ${new_label} failed" >&2
    return 1
  fi

  if [[ ! -f "${old_plist}" ]]; then
    echo "[error] ${name}: missing legacy plist ${old_plist}" >&2
    return 1
  fi

  if ! "${LAUNCHCTL_CMD}" enable "gui/${UID_NUM}/${old_label}"; then
    echo "[error] ${name}: enable ${old_label} failed" >&2
    return 1
  fi

  start_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if [[ "${name}" == "alphaengine-desktop-watchdog" ]]; then
    sleep 1
    if launchd_label_loaded "${old_label}"; then
      start_and_verify_watchdog "${old_label}" "${old_plist}" "${start_utc}" 0 || return 1
    else
      start_and_verify_watchdog "${old_label}" "${old_plist}" "${start_utc}" 1 || return 1
    fi
    return 0
  fi

  if ! launchd_label_loaded "${old_label}"; then
    if ! "${LAUNCHCTL_CMD}" bootstrap "gui/${UID_NUM}" "${old_plist}"; then
      echo "[error] ${name}: bootstrap ${old_plist} failed" >&2
      return 1
    fi
  fi

  if ! "${LAUNCHCTL_CMD}" kickstart -k "gui/${UID_NUM}/${old_label}"; then
    echo "[error] ${name}: kickstart ${old_label} failed" >&2
    return 1
  fi

  if ! wait_for_service_health "${name}" "${old_label}" "${port}" "${start_utc}"; then
    echo "[error] ${name}: post-rollback health check failed for ${old_label}" >&2
    return 1
  fi

  return 0
}

run_plan() {
  echo "=== Dalton Host Services Switch Plan (dry-run) ==="
  echo "BRIDGE_ENV_FILE:   ${BRIDGE_ENV_FILE} (${BRIDGE_ENV_STATUS})"
  echo "DETECTED_MODE:     ${RUNTIME_MODE}"
  echo "DATA_ROOT:         ${DATA_ROOT}"
  echo "PLIST_RUNTIME_ENV: ${RUNTIME_ENV_KEY}=${RUNTIME_ENV_VAL}"
  echo "REPO_ROOT:         ${REPO_ROOT}"
  echo "LAUNCH_AGENTS_DIR: ${LAUNCH_AGENTS_DIR}"
  echo "LOGS_DIR:          ${LOGS_DIR}"
  echo "NODE_BIN:          ${NODE_BIN}"
  echo "PYTHON_BIN:        ${PYTHON_BIN}"
  echo ""
  echo "Service data paths (no file copying performed by this script):"
  echo "  - alphaengine-mcp (port ${ALPHAENGINE_PORT}):"
  echo "      ${RUNTIME_ENV_KEY}=${RUNTIME_ENV_VAL}"
  echo "      downloads=${ALPHAENGINE_DOWNLOADS_DIR}"
  echo "  - alphaengine-desktop-watchdog (StartInterval 120s):"
  echo "      ${RUNTIME_ENV_KEY}=${RUNTIME_ENV_VAL}"
  echo "      state_file=${WATCHDOG_STATE_FILE} ($(file_size_bytes "${WATCHDOG_STATE_FILE}"))"
  echo "  - guidepoint-mcp-proxy (port ${GUIDEPOINT_PORT}):"
  echo "      ${RUNTIME_ENV_KEY}=${RUNTIME_ENV_VAL}"
  echo "      GUIDEPOINT_ENV_FILE=${GUIDEPOINT_ENV_PATH} ($(file_size_bytes "${GUIDEPOINT_ENV_PATH}"))"
  echo "      GUIDEPOINT_TOKENS_FILE=${GUIDEPOINT_TOKENS_PATH} ($(file_size_bytes "${GUIDEPOINT_TOKENS_PATH}"))"
  echo ""
  echo "Step 1: Render launchd plist templates and run plutil -lint:"
  local entry name old_label new_label port tpl_path dst_plist old_plist
  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name old_label new_label port <<< "${entry}"
    tpl_path="${REPO_ROOT}/scripts/dalton/launchd/${new_label}.plist.template"
    dst_plist="${LAUNCH_AGENTS_DIR}/${new_label}.plist"
    echo "  - ${tpl_path} -> ${dst_plist}"
  done
  echo ""
  echo "Step 2: Stop-one / start-one sequence (old plists preserved; port release <=${PORT_RELEASE_TIMEOUT_S}s; health+PID check <=${HEALTH_TIMEOUT_S}s):"
  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name old_label new_label port <<< "${entry}"
    echo "  - [${name}] bootout ${old_label} -> verify unloaded & port ${port} released -> disable ${old_label} -> enable & bootstrap ${new_label} -> kickstart & health check"
  done
  echo ""
  echo "Current launchd status (gui/${UID_NUM}):"
  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name old_label new_label port <<< "${entry}"
    old_plist="${LAUNCH_AGENTS_DIR}/${old_label}.plist"
    dst_plist="${LAUNCH_AGENTS_DIR}/${new_label}.plist"
    echo "  - ${name}:"
    echo "      old: ${old_label} ($(launchd_label_status "${old_label}"), plist=$(if [[ -f "${old_plist}" ]]; then echo present; else echo missing; fi))"
    echo "      new: ${new_label} ($(launchd_label_status "${new_label}"), plist=$(if [[ -f "${dst_plist}" ]]; then echo present; else echo missing; fi))"
  done
  echo ""
  echo "Rollback command (old plists are preserved):"
  echo "  bash scripts/dalton/ops/host_services_switch.sh --rollback"
}

run_apply() {
  # The bridge env file decides legacy vs new data paths; without it we cannot tell, so stop.
  if [[ "${BRIDGE_ENV_STATUS}" != "ok" ]]; then
    echo "ERROR: bridge env file ${BRIDGE_ENV_FILE} is ${BRIDGE_ENV_STATUS}; cannot tell legacy from new mode" >&2
    exit 1
  fi
  echo "=== Dalton Host Services Switch (--apply, mode=${RUNTIME_MODE}) ==="
  mkdir -p "${LAUNCH_AGENTS_DIR}" "${LOGS_DIR}" "${DATA_ROOT}/state"

  echo "[step 1] Rendering and linting plist templates (${RUNTIME_ENV_KEY}=${RUNTIME_ENV_VAL})..."
  local entry name old_label new_label port tpl_path dst_plist
  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name old_label new_label port <<< "${entry}"
    tpl_path="${REPO_ROOT}/scripts/dalton/launchd/${new_label}.plist.template"
    dst_plist="${LAUNCH_AGENTS_DIR}/${new_label}.plist"
    render_plist_template "${tpl_path}" "${dst_plist}"
    "${PLUTIL_CMD}" -lint "${dst_plist}"
  done

  echo "[step 2] Switching services sequentially..."
  local switched=()
  local restored=()
  local failed_restore=()
  local switch_start_utc=""

  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name old_label new_label port <<< "${entry}"
    dst_plist="${LAUNCH_AGENTS_DIR}/${new_label}.plist"

    echo "[switch] ${name}: (1) bootout ${old_label}"
    if launchd_label_loaded "${old_label}"; then
      if ! "${LAUNCHCTL_CMD}" bootout "gui/${UID_NUM}/${old_label}"; then
        echo "[error] ${name}: bootout ${old_label} returned non-zero" >&2
      fi
    fi

    echo "[switch] ${name}: (2) verify ${old_label} unloaded & port ${port} released (timeout ${PORT_RELEASE_TIMEOUT_S}s)"
    if ! wait_unloaded_and_port_released "${old_label}" "${port}"; then
      echo "[error] ${name}: old service ${old_label} failed to unload or release port ${port}; aborting without starting ${new_label}" >&2
      if restore_old_service_in_place "${name}" "${old_label}" "${port}"; then
        restored+=("${name}:${old_label}")
      else
        failed_restore+=("${name}:${old_label}")
      fi
      echo "已切换 (switched): [${switched[*]:-none}]" >&2
      echo "已恢复的服务 (restored): [${restored[*]:-none}]" >&2
      echo "恢复失败的服务 (failed): [${failed_restore[*]:-none}]" >&2
      exit 1
    fi

    # Capture switch_start_utc only AFTER old service is confirmed unloaded so stale writes cannot pass.
    switch_start_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    echo "[switch] ${name}: (3) disable ${old_label}"
    if ! "${LAUNCHCTL_CMD}" disable "gui/${UID_NUM}/${old_label}"; then
      echo "[error] ${name}: disable ${old_label} failed; restoring ${old_label}" >&2
      if restore_old_service_in_place "${name}" "${old_label}" "${port}"; then
        restored+=("${name}:${old_label}")
      else
        failed_restore+=("${name}:${old_label}")
      fi
      echo "已切换 (switched): [${switched[*]:-none}]" >&2
      echo "已恢复的服务 (restored): [${restored[*]:-none}]" >&2
      echo "恢复失败的服务 (failed): [${failed_restore[*]:-none}]" >&2
      exit 1
    fi

    echo "[switch] ${name}: (4) enable & bootstrap ${new_label}"
    local start_ok=1
    if ! "${LAUNCHCTL_CMD}" enable "gui/${UID_NUM}/${new_label}"; then
      echo "[error] ${name}: enable ${new_label} failed" >&2
      start_ok=0
    fi
    if [[ "${start_ok}" -eq 1 ]] && launchd_label_loaded "${new_label}"; then
      if ! "${LAUNCHCTL_CMD}" bootout "gui/${UID_NUM}/${new_label}" || ! wait_unloaded_and_port_released "${new_label}" "${port}"; then
        echo "[error] ${name}: failed clearing existing ${new_label}" >&2
        start_ok=0
      fi
    fi
    if [[ "${start_ok}" -eq 1 ]]; then
      if [[ "${name}" == "alphaengine-desktop-watchdog" ]]; then
        sleep 1
        echo "[switch] ${name}: (5) bootstrap & verify ${new_label} (timeout ${HEALTH_TIMEOUT_S}s)"
        if ! start_and_verify_watchdog "${new_label}" "${dst_plist}" "${switch_start_utc}" 1; then
          start_ok=0
        fi
      else
        if ! "${LAUNCHCTL_CMD}" bootstrap "gui/${UID_NUM}" "${dst_plist}"; then
          echo "[error] ${name}: bootstrap ${dst_plist} failed" >&2
          start_ok=0
        fi
        if [[ "${start_ok}" -eq 1 ]] && ! "${LAUNCHCTL_CMD}" kickstart -k "gui/${UID_NUM}/${new_label}"; then
          echo "[error] ${name}: kickstart ${new_label} failed" >&2
          start_ok=0
        fi
        if [[ "${start_ok}" -eq 1 ]]; then
          echo "[switch] ${name}: (5) health check ${new_label} (timeout ${HEALTH_TIMEOUT_S}s)"
          if ! wait_for_service_health "${name}" "${new_label}" "${port}" "${switch_start_utc}"; then
            echo "[error] ${name}: health check failed for ${new_label}" >&2
            start_ok=0
          fi
        fi
      fi
    fi

    if [[ "${start_ok}" -eq 0 ]]; then
      echo "[rollback] ${name}: reverting ${new_label} -> ${old_label}..." >&2
      if rollback_one_service "${name}" "${old_label}" "${new_label}" "${port}"; then
        restored+=("${name}:${old_label}")
      else
        failed_restore+=("${name}:${old_label}")
      fi
      echo "已切换 (switched): [${switched[*]:-none}]" >&2
      echo "已恢复的服务 (restored): [${restored[*]:-none}]" >&2
      echo "恢复失败的服务 (failed): [${failed_restore[*]:-none}]" >&2
      exit 1
    fi

    echo "[ok] ${name} switched to ${new_label}"
    switched+=("${name}:${new_label}")
  done

  echo "All services switched successfully: [${switched[*]}]"
}

run_rerender_only() {
  # The bridge env file decides legacy vs new data paths; without it we cannot tell, so stop.
  if [[ "${BRIDGE_ENV_STATUS}" != "ok" ]]; then
    echo "ERROR: bridge env file ${BRIDGE_ENV_FILE} is ${BRIDGE_ENV_STATUS}; cannot tell legacy from new mode" >&2
    exit 1
  fi
  echo "=== Dalton Host Services Rerender (--apply --rerender-only, mode=${RUNTIME_MODE}) ==="
  mkdir -p "${LAUNCH_AGENTS_DIR}" "${LOGS_DIR}" "${DATA_ROOT}/state"

  local entry name old_label new_label port tpl_path dst_plist
  local loaded_names=()
  local loaded_labels=()
  local loaded_ports=()
  local loaded_plists=()
  local loaded_snapshots=()

  # Snapshot all currently loaded com.dalton.* service plists upfront before modifying anything.
  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name old_label new_label port <<< "${entry}"
    if ! launchd_label_loaded "${new_label}"; then
      echo "[skip] ${name}: ${new_label} is not loaded"
      continue
    fi
    dst_plist="${LAUNCH_AGENTS_DIR}/${new_label}.plist"
    if [[ ! -f "${dst_plist}" ]]; then
      echo "ERROR: ${name}: ${new_label} is loaded but plist is missing: ${dst_plist}" >&2
      exit 1
    fi
    local snap
    snap="$(mktemp)"
    TMP_ARTIFACTS+=("${snap}")
    cp -p "${dst_plist}" "${snap}"
    loaded_names+=("${name}")
    loaded_labels+=("${new_label}")
    loaded_ports+=("${port}")
    loaded_plists+=("${dst_plist}")
    loaded_snapshots+=("${snap}")
  done

  if [[ "${#loaded_names[@]}" -eq 0 ]]; then
    echo "Rerender completed: [none]"
    return 0
  fi

  local updated=()
  local rendered_indices=()
  local restored=()
  local failed_restore=()
  local idx

  for idx in "${!loaded_names[@]}"; do
    name="${loaded_names[$idx]}"
    new_label="${loaded_labels[$idx]}"
    port="${loaded_ports[$idx]}"
    dst_plist="${loaded_plists[$idx]}"
    tpl_path="${REPO_ROOT}/scripts/dalton/launchd/${new_label}.plist.template"

    local step_ok=1
    local switch_start_utc=""
    rendered_indices+=("${idx}")

    if ! render_plist_template "${tpl_path}" "${dst_plist}"; then
      step_ok=0
    elif ! "${PLUTIL_CMD}" -lint "${dst_plist}"; then
      step_ok=0
    fi

    if [[ "${step_ok}" -eq 1 ]]; then
      if ! "${LAUNCHCTL_CMD}" bootout "gui/${UID_NUM}/${new_label}"; then
        echo "[error] ${name}: bootout ${new_label} failed" >&2
        step_ok=0
      fi
    fi
    if [[ "${step_ok}" -eq 1 ]]; then
      if ! wait_unloaded_and_port_released "${new_label}" "${port}"; then
        echo "[error] ${name}: ${new_label} failed to unload or release port ${port}" >&2
        step_ok=0
      fi
    fi
    if [[ "${step_ok}" -eq 1 ]]; then
      switch_start_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
      if ! "${LAUNCHCTL_CMD}" enable "gui/${UID_NUM}/${new_label}"; then
        echo "[error] ${name}: enable ${new_label} failed" >&2
        step_ok=0
      fi
    fi
    if [[ "${step_ok}" -eq 1 ]]; then
      if [[ "${name}" == "alphaengine-desktop-watchdog" ]]; then
        sleep 1
        if ! start_and_verify_watchdog "${new_label}" "${dst_plist}" "${switch_start_utc}" 1; then
          step_ok=0
        fi
      else
        if ! "${LAUNCHCTL_CMD}" bootstrap "gui/${UID_NUM}" "${dst_plist}"; then
          echo "[error] ${name}: bootstrap ${dst_plist} failed" >&2
          step_ok=0
        fi
        if [[ "${step_ok}" -eq 1 ]] && ! "${LAUNCHCTL_CMD}" kickstart -k "gui/${UID_NUM}/${new_label}"; then
          echo "[error] ${name}: kickstart ${new_label} failed" >&2
          step_ok=0
        fi
        if [[ "${step_ok}" -eq 1 ]]; then
          if ! wait_for_service_health "${name}" "${new_label}" "${port}" "${switch_start_utc}"; then
            echo "[error] ${name}: health check failed after rerender" >&2
            step_ok=0
          fi
        fi
      fi
    fi

    if [[ "${step_ok}" -eq 0 ]]; then
      echo "[restore] rerender failed at ${name}; restoring all ${#rendered_indices[@]} rerendered service(s) to pre-switch snapshots..." >&2
      local r_idx r_name r_label r_port r_plist r_snap r_tmp restore_ok restore_start_utc
      for r_idx in "${rendered_indices[@]}"; do
        r_name="${loaded_names[$r_idx]}"
        r_label="${loaded_labels[$r_idx]}"
        r_port="${loaded_ports[$r_idx]}"
        r_plist="${loaded_plists[$r_idx]}"
        r_snap="${loaded_snapshots[$r_idx]}"
        restore_ok=1

        r_tmp="${r_plist}.restore.$$"
        TMP_ARTIFACTS+=("${r_tmp}")
        if ! cp -p "${r_snap}" "${r_tmp}" || ! mv -f "${r_tmp}" "${r_plist}"; then
          restore_ok=0
        fi
        if [[ "${restore_ok}" -eq 1 ]] && launchd_label_loaded "${r_label}"; then
          "${LAUNCHCTL_CMD}" bootout "gui/${UID_NUM}/${r_label}" || restore_ok=0
          wait_unloaded_and_port_released "${r_label}" "${r_port}" || restore_ok=0
        fi
        if [[ "${restore_ok}" -eq 1 ]]; then
          restore_start_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
          "${LAUNCHCTL_CMD}" enable "gui/${UID_NUM}/${r_label}" || restore_ok=0
        fi
        if [[ "${restore_ok}" -eq 1 ]]; then
          if [[ "${r_name}" == "alphaengine-desktop-watchdog" ]]; then
            sleep 1
            start_and_verify_watchdog "${r_label}" "${r_plist}" "${restore_start_utc}" 1 || restore_ok=0
          else
            "${LAUNCHCTL_CMD}" bootstrap "gui/${UID_NUM}" "${r_plist}" || restore_ok=0
            if [[ "${restore_ok}" -eq 1 ]]; then
              "${LAUNCHCTL_CMD}" kickstart -k "gui/${UID_NUM}/${r_label}" || restore_ok=0
            fi
            if [[ "${restore_ok}" -eq 1 ]]; then
              wait_for_service_health "${r_name}" "${r_label}" "${r_port}" "${restore_start_utc}" || restore_ok=0
            fi
          fi
        fi
        if [[ "${restore_ok}" -eq 1 ]]; then
          restored+=("${r_name}:${r_label}")
        else
          failed_restore+=("${r_name}:${r_label}")
        fi
      done
      echo "已恢复的服务 (restored): [${restored[*]:-none}]" >&2
      echo "恢复失败的服务 (failed): [${failed_restore[*]:-none}]" >&2
      exit 1
    fi

    echo "[ok] ${name} rerendered and healthy (${new_label})"
    updated+=("${name}:${new_label}")
  done

  echo "Rerender completed: [${updated[*]:-none}]"
}

run_rollback() {
  echo "=== Dalton Host Services Rollback (--rollback) ==="
  local entry name old_label new_label port
  local restored=()
  local failed=()

  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name old_label new_label port <<< "${entry}"
    if rollback_one_service "${name}" "${old_label}" "${new_label}" "${port}"; then
      echo "[ok] ${name} rolled back to ${old_label}"
      restored+=("${name}:${old_label}")
    else
      echo "[error] ${name} rollback failed" >&2
      failed+=("${name}:${old_label}")
    fi
  done

  echo "已恢复的服务 (restored): [${restored[*]:-none}]"
  echo "恢复失败的服务 (failed): [${failed[*]:-none}]"
  if [[ "${#failed[@]}" -gt 0 ]]; then
    exit 1
  fi
  exit 0
}

case "${MODE}" in
  plan)
    run_plan
    ;;
  apply)
    if [[ "${RERENDER_ONLY}" -eq 1 ]]; then
      run_rerender_only
    else
      run_apply
    fi
    ;;
  rollback)
    run_rollback
    ;;
esac
