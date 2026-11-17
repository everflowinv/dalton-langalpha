#!/bin/bash
# Dalton backend watchdog: restart the backend container after 3 consecutive failed /health checks,
# and alert the PM on Feishu (through the host bridge's /notify) after 5 minutes down, plus once
# more when it recovers. The bridge runs on the host, so the alert still goes out when Docker is down;
# if the bridge is down too, fall back to a macOS notification.
export PATH="/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${DALTON_REPO_ROOT:-$(cd "${SCRIPT_DIR}/../../../.." && pwd)}"
BACKUP_DIR="${DALTON_BACKUP_DIR:-$HOME/Backups/dalton}"
OPS_STATE_DIR="${DALTON_OPS_STATE_DIR:-$HOME/.dalton-ops}"
STATE="${DALTON_WATCHDOG_STATE_FILE:-${BACKUP_DIR}/.watchdog_failures}"
DOWN_SINCE="${OPS_STATE_DIR}/.down_since"      # epoch of the first failed check in this outage
ALERTED="${OPS_STATE_DIR}/.down_alerted"       # present once the outage alert went out
TARGETS="${DALTON_ALERT_TARGETS_FILE:-${OPS_STATE_DIR}/alert_targets.json}"
LOG="${DALTON_WATCHDOG_LOG_FILE:-${BACKUP_DIR}/watchdog.log}"
ENV_FILE="${DALTON_ENV_FILE:-${REPO_ROOT}/.env}"
ALERT_AFTER_S="${DALTON_WATCHDOG_ALERT_AFTER_S:-300}"

GATEWAY_ENV="${DALTON_CHANNEL_GATEWAY_ENV_FILE:-$HOME/.config/dalton/channel-gateway.env}"

notify() {  # $1 = text, $2 = ops alert code (backend_down | backend_recovered)
  local token payload code
  # Preferred path: the Dalton channel gateway's loopback ops alert (fixed text and
  # targets from ~/.config/dalton/ops-alerts.json); no OpenClaw involved.
  token=$(grep -E '^INTERNAL_SERVICE_TOKEN=' "$GATEWAY_ENV" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'")
  code=000
  if [ -n "$token" ] && [ -n "$2" ]; then
    code=$(curl -s -m 20 --noproxy '*' -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8962/internal/ops-alerts \
      -H "Content-Type: application/json" -H "X-Service-Token: $token" --data "{\"code\":\"$2\"}")
  fi
  echo "$(date '+%F %T') ops-alert http=$code: $1" >> "$LOG"
  [ "$code" = "200" ] && return 0
  # Fallback: the host bridge /notify, then a macOS notification.
  token=$(grep -E '^DALTON_BRIDGE_ADMIN_TOKEN=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'")
  payload=$(python3 -I -c 'import json,sys; t=json.load(open(sys.argv[1])).get("targets",[]); print(json.dumps({"targets":t,"text":sys.argv[2]}))' "$TARGETS" "$1" 2>/dev/null)
  code=000
  if [ -n "$token" ] && [ -n "$payload" ]; then
    code=$(curl -s -m 15 --noproxy '*' -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8960/notify \
      -H "Content-Type: application/json" -H "Authorization: Bearer $token" -H "X-Dalton-Bridge-Token: $token" \
      --data "$payload")
  fi
  echo "$(date '+%F %T') notify http=$code: $1" >> "$LOG"
  if [ "$code" != "200" ]; then
    osascript -e "display notification \"$1\" with title \"Dalton\"" >/dev/null 2>&1 || true
  fi
}

if curl -s -m 15 -o /dev/null -w '%{http_code}' --noproxy '*' http://127.0.0.1:8000/health | grep -q '^200$'; then
  echo 0 > "$STATE"
  if [ -f "$ALERTED" ]; then
    since=$(cat "$DOWN_SINCE" 2>/dev/null || date +%s); mins=$(( ($(date +%s) - since) / 60 ))
    notify "✅ Dalton 已恢复（中断约 ${mins} 分钟）。" backend_recovered
  fi
  rm -f "$DOWN_SINCE" "$ALERTED"
  exit 0
fi
[ -f "$DOWN_SINCE" ] || date +%s > "$DOWN_SINCE"
n=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$STATE"
echo "$(date '+%F %T') health check failed ($n/3)" >> "$LOG"
since=$(cat "$DOWN_SINCE"); down_s=$(( $(date +%s) - since ))
if [ "$down_s" -ge "$ALERT_AFTER_S" ] && [ ! -f "$ALERTED" ]; then
  touch "$ALERTED"
  if colima list 2>/dev/null | grep -q Running; then why="后端健康检查失败，看门狗正在尝试重启后端"; else why="Docker（Colima）没有在运行"; fi
  notify "⚠️ Dalton 后端已经 $((down_s / 60)) 分钟不可用：${why}。恢复后会再通知。" backend_down
fi
if [ "$n" -ge 3 ]; then
  # Record the turns this restart will interrupt, so they can be resumed afterwards.
  running=$(docker exec dalton-langalpha-postgres-1 psql -U postgres -Atc \
    "select conversation_thread_id from conversation_threads where current_status='in_progress'" 2>/dev/null | tr '\n' ' ')
  echo "$(date '+%F %T') restarting backend; in-progress threads: ${running:-none}" >> "$LOG"
  # Only the backend container: `docker compose restart backend` also restarts one-off
  # `docker compose run` containers (e.g. a conversation-archive backfill).
  docker restart dalton-langalpha-backend-1 >> "$LOG" 2>&1
  echo 0 > "$STATE"
fi
