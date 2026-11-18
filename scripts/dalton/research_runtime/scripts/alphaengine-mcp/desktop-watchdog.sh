#!/usr/bin/env bash
set -euo pipefail

APP_BUNDLE="${ALPHAENGINE_APP_BUNDLE:-/Applications/AlphaEngine.app}"
APP_EXECUTABLE="${ALPHAENGINE_APP_EXECUTABLE:-$APP_BUNDLE/Contents/MacOS/AlphaEngine}"
SERVICE_URL="${ALPHAENGINE_MCP_URL:-http://127.0.0.1:8950}"

if [[ -n "${DALTON_RESEARCH_HOME:-}" ]]; then
  RESEARCH_HOME="$DALTON_RESEARCH_HOME"
else
  RESEARCH_HOME="$HOME/Library/Application Support/Everflow/research"
fi

STATE_DIR="${ALPHAENGINE_WATCHDOG_STATE_DIR:-$RESEARCH_HOME/state}"
STATE_FILE="$STATE_DIR/alphaengine-desktop-watchdog.json"
NOTIFY_FILE="$STATE_DIR/alphaengine-desktop-watchdog.last-notified"
# shlock(1) takes the lock atomically and clears it only when the PID inside has exited,
# so a live run is never displaced and two runs can never both reclaim a stale lock.
LOCK_FILE="$STATE_DIR/alphaengine-desktop-watchdog.pid.lock"
SHLOCK_CMD="${SHLOCK_BIN:-/usr/bin/shlock}"
NOTIFY_COOLDOWN_SECONDS="${ALPHAENGINE_NOTIFY_COOLDOWN_SECONDS:-21600}"

GATEWAY_ENV="${DALTON_CHANNEL_GATEWAY_ENV_FILE:-$HOME/.config/dalton/channel-gateway.env}"
GATEWAY_OPS_ALERTS_URL="${DALTON_OPS_ALERTS_URL:-http://127.0.0.1:8962/internal/ops-alerts}"
CURL_CMD="${CURL_BIN:-/usr/bin/curl}"
JQ_CMD="${JQ_BIN:-/usr/bin/jq}"
PS_CMD="${PS_BIN:-/bin/ps}"
OPEN_CMD="${OPEN_BIN:-/usr/bin/open}"
OSASCRIPT_CMD="${OSASCRIPT_BIN:-/usr/bin/osascript}"
APP_START_SLEEP_S="${ALPHAENGINE_APP_START_SLEEP_S:-2}"

mkdir -p "$STATE_DIR"
CURRENT_HDR_FILE=""
LOCK_ACQUIRED=0

cleanup_on_exit() {
  rm -f "$CURRENT_HDR_FILE" 2>/dev/null || true
  if [[ "$LOCK_ACQUIRED" -eq 1 ]]; then
    local owner=""
    read -r owner <"$LOCK_FILE" 2>/dev/null || owner=""
    if [[ "$owner" == "$$" ]]; then
      rm -f "$LOCK_FILE" 2>/dev/null || true
    fi
    LOCK_ACQUIRED=0
  fi
}

if ! "$SHLOCK_CMD" -f "$LOCK_FILE" -p "$$"; then
  exit 0
fi
LOCK_ACQUIRED=1
trap 'cleanup_on_exit' EXIT
trap 'cleanup_on_exit; exit 129' HUP
trap 'cleanup_on_exit; exit 130' INT
trap 'cleanup_on_exit; exit 143' TERM

app_is_running() {
  local command_line
  while IFS= read -r command_line; do
    if [[ "$command_line" == "$APP_EXECUTABLE"* ]]; then
      return 0
    fi
  done < <("$PS_CMD" -axo command=)
  return 1
}

write_state() {
  local status="$1"
  local app_running="$2"
  local authenticated="$3"
  local checked_at tmp_file
  checked_at="$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)"
  tmp_file="${STATE_FILE}.tmp.$$"
  printf '{"checked_at":"%s","status":"%s","app_running":%s,"mcp_authenticated":%s}\n' \
    "$checked_at" "$status" "$app_running" "$authenticated" >"$tmp_file"
  mv -f "$tmp_file" "$STATE_FILE"
}

notify() {
  local title="$1" body="$2" ops_code="${3:-}"
  local now last=0 token="" http_code=000
  now="$(/bin/date +%s)"
  if [[ -f "$NOTIFY_FILE" ]]; then
    read -r last <"$NOTIFY_FILE" || last=0
  fi
  if (( now - last < NOTIFY_COOLDOWN_SECONDS )); then
    return 0
  fi
  if [[ -n "$ops_code" && -r "$GATEWAY_ENV" ]]; then
    token="$(grep -E '^INTERNAL_SERVICE_TOKEN=' "$GATEWAY_ENV" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'" || true)"
  fi
  if [[ -n "$token" && -n "$ops_code" ]]; then
    local hdr_file
    hdr_file="$(umask 077 && /usr/bin/mktemp)"
    CURRENT_HDR_FILE="$hdr_file"
    printf 'X-Service-Token: %s\n' "$token" >"$hdr_file"
    chmod 600 "$hdr_file" 2>/dev/null || true
    http_code="$("$CURL_CMD" -s -m 20 --noproxy '*' -o /dev/null -w '%{http_code}' -X POST "$GATEWAY_OPS_ALERTS_URL" \
      -H "Content-Type: application/json" -H "@${hdr_file}" --data "{\"code\":\"$ops_code\"}" 2>/dev/null || echo 000)"
    rm -f "$hdr_file"
    CURRENT_HDR_FILE=""
  fi
  if [[ "$http_code" != "200" ]]; then
    "$OSASCRIPT_CMD" -e "display notification \"$body\" with title \"$title\"" \
      >/dev/null 2>&1 || true
  fi
  printf '%s\n' "$now" >"$NOTIFY_FILE"
}

notify_module_page_required() {
  notify "AlphaEngine 需要打开模块页" "登录态正常，但 App 里没有打开的模块页，MCP 读不到 token" "alphaengine_module_page_required"
}

notify_login_required() {
  notify "AlphaEngine 登录失效" "请打开 AlphaEngine Desktop 完成登录" "alphaengine_login_required"
}

if [[ ! -d "$APP_BUNDLE" ]]; then
  write_state "app_missing" false false
  exit 0
fi

if ! app_is_running; then
  "$OPEN_CMD" -gj "$APP_BUNDLE"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    /bin/sleep "$APP_START_SLEEP_S"
    if app_is_running; then
      break
    fi
  done
fi

if ! app_is_running; then
  write_state "app_start_failed" false false
  notify_login_required
  exit 0
fi

health="$("$CURL_CMD" -fsS --max-time 10 "$SERVICE_URL/health" 2>/dev/null || true)"
if [[ -n "$health" ]] && "$JQ_CMD" -e '.ok == true and .authenticated == true' >/dev/null 2>&1 <<<"$health"; then
  write_state "ready" true true
  exit 0
fi

if [[ -z "$health" ]]; then
  write_state "mcp_unreachable" true false
  notify "AlphaEngine MCP 无响应" "127.0.0.1:8950 健康检查失败，检查 alphaengine-mcp 服务"
  exit 0
fi

# token 只随模块页的 webview 子进程下发；App 开着但没开模块页时 renderer 数为 0，
# 这时登录态是好的，别提示重新登录。
if "$JQ_CMD" -e '.desktop_renderer_processes == 0' >/dev/null 2>&1 <<<"$health"; then
  write_state "module_page_required" true false
  notify_module_page_required
  exit 0
fi

write_state "login_required" true false
notify_login_required
