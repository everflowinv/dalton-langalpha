#!/bin/bash
# Wait until no chat turn and no subagent run is in progress (host archive runs are ignored:
# a backend restart does not touch them). Needs 2 consecutive idle checks 60s apart.
# Stale automation threads (>15m since last update whose associated automation executions are
# all in terminal status and have no live conversation_responses row) do not count as busy.
SLEEP_SECONDS="${WAIT_BACKEND_IDLE_SLEEP_SECONDS:-60}"
MAX_CHECKS="${WAIT_BACKEND_IDLE_MAX_CHECKS:-720}"
POSTGRES_CONTAINER="${WAIT_BACKEND_IDLE_POSTGRES_CONTAINER:-dalton-langalpha-postgres-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
THREAD_BUSY_SQL_FILE="${WAIT_BACKEND_IDLE_SQL_FILE:-${SCRIPT_DIR}/backend_busy_threads.sql}"
if [ ! -f "$THREAD_BUSY_SQL_FILE" ]; then
  echo "ERROR: missing ${THREAD_BUSY_SQL_FILE}" >&2
  exit 1
fi
THREAD_BUSY_SQL="$(tr '\n' ' ' < "$THREAD_BUSY_SQL_FILE" | sed 's/  */ /g; s/^ //; s/ $//')"
SUBAGENT_BUSY_SQL="SELECT count(*) FROM subagent_runs WHERE status='in_progress'"
ok=0
for i in $(seq 1 "$MAX_CHECKS"); do
  t=$(docker exec "$POSTGRES_CONTAINER" psql -U postgres -d postgres -Atc "$THREAD_BUSY_SQL")
  s=$(docker exec "$POSTGRES_CONTAINER" psql -U postgres -d postgres -Atc "$SUBAGENT_BUSY_SQL")
  if [ "$t" = "0" ] && [ "$s" = "0" ]; then ok=$((ok+1)); else ok=0; fi
  if [ $ok -ge 2 ]; then echo "idle after $i min ($(date))"; exit 0; fi
  [ $((i % 30)) -eq 1 ] && echo "$(date '+%T') threads=$t subagents=$s"
  sleep "$SLEEP_SECONDS"
done
echo still_busy; exit 1
