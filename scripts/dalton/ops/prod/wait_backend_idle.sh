#!/bin/bash
# Wait until no chat turn and no subagent run is in progress (host archive runs are ignored:
# a backend restart does not touch them). Needs 2 consecutive idle checks 60s apart.
# Stale automation threads (>15m since last update whose associated automation executions are
# all in terminal status and have no live conversation_responses row) do not count as busy.
SLEEP_SECONDS="${WAIT_BACKEND_IDLE_SLEEP_SECONDS:-60}"
MAX_CHECKS="${WAIT_BACKEND_IDLE_MAX_CHECKS:-720}"
POSTGRES_CONTAINER="${WAIT_BACKEND_IDLE_POSTGRES_CONTAINER:-dalton-langalpha-postgres-1}"
THREAD_BUSY_SQL="SELECT count(*) FROM conversation_threads ct WHERE ct.current_status = 'in_progress' AND NOT (ct.updated_at < NOW() - INTERVAL '15 minutes' AND EXISTS (SELECT 1 FROM automation_executions ae WHERE ae.conversation_thread_id = ct.conversation_thread_id) AND NOT EXISTS (SELECT 1 FROM automation_executions ae WHERE ae.conversation_thread_id = ct.conversation_thread_id AND ae.status NOT IN ('completed', 'failed', 'skipped', 'cancelled', 'timeout')) AND NOT EXISTS (SELECT 1 FROM conversation_responses cr WHERE cr.conversation_thread_id = ct.conversation_thread_id AND cr.status = 'in_progress'))"
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
