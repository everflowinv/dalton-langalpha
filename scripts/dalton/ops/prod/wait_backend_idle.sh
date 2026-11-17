#!/bin/bash
# Wait until no chat turn and no subagent run is in progress (host archive runs are ignored:
# a backend restart does not touch them). Needs 2 consecutive idle checks 60s apart.
ok=0
for i in $(seq 1 720); do
  t=$(docker exec dalton-langalpha-postgres-1 psql -U postgres -d postgres -Atc "select count(*) from conversation_threads where current_status='in_progress'")
  s=$(docker exec dalton-langalpha-postgres-1 psql -U postgres -d postgres -Atc "select count(*) from subagent_runs where status='in_progress'")
  if [ "$t" = "0" ] && [ "$s" = "0" ]; then ok=$((ok+1)); else ok=0; fi
  if [ $ok -ge 2 ]; then echo "idle after $i min ($(date))"; exit 0; fi
  [ $((i % 30)) -eq 1 ] && echo "$(date '+%T') threads=$t subagents=$s"
  sleep 60
done
echo still_busy; exit 1
