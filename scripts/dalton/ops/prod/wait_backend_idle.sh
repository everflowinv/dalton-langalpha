#!/bin/bash
# Wait until backend work is idle.
#
# Default (no arguments):
#   Unchanged routine-deploy behavior: polls `conversation_threads` and
#   `subagent_runs` up to 720 times x 60s, requiring 2 consecutive idle checks.
#
# Maintenance mode (any explicit flag):
#   --max-wait-seconds N     Total wait budget in seconds (default: 600)
#   --check-bridge-healthz   Also require legacy bridge :8960/healthz limiter idle
#   --check-bridge-drain     Also require :8960/admin/drain inflight/queued/ingest_inflight == 0
#   Always includes claimed `dalton_data_task_slot` & active `dalton_data_task_runs`
#   (host + container tasks) in the 2-consecutive-zero idle check.
#   Self-contained (does not source external files) so it can run from recovery-bin/.

DOCKER_CMD="${DOCKER_BIN:-docker}"
PG_CONTAINER="${DALTON_POSTGRES_CONTAINER:-${POSTGRES_CONTAINER:-dalton-langalpha-postgres-1}}"

# ---------------------------------------------------------------------------
# Routine no-argument mode (preserve exact existing behavior & output strings)
# ---------------------------------------------------------------------------
if [ "$#" -eq 0 ]; then
  default_rounds="${WAIT_IDLE_DEFAULT_MAX_ROUNDS:-720}"
  default_poll_s="${WAIT_IDLE_DEFAULT_POLL_S:-60}"
  ok=0
  for i in $(seq 1 "$default_rounds"); do
    t=$("$DOCKER_CMD" exec "$PG_CONTAINER" psql -U postgres -d postgres -Atc "select count(*) from conversation_threads where current_status='in_progress'")
    s=$("$DOCKER_CMD" exec "$PG_CONTAINER" psql -U postgres -d postgres -Atc "select count(*) from subagent_runs where status='in_progress'")
    if [ "$t" = "0" ] && [ "$s" = "0" ]; then ok=$((ok+1)); else ok=0; fi
    if [ $ok -ge 2 ]; then echo "idle after $i min ($(date))"; exit 0; fi
    [ $((i % 30)) -eq 1 ] && echo "$(date '+%T') threads=$t subagents=$s"
    sleep "$default_poll_s"
  done
  echo still_busy; exit 1
fi

# ---------------------------------------------------------------------------
# Maintenance mode
# ---------------------------------------------------------------------------
set -uo pipefail

MAX_WAIT_SECONDS=600
CHECK_BRIDGE_HEALTHZ=0
CHECK_BRIDGE_DRAIN=0
POLL_S="${WAIT_IDLE_POLL_S:-10}"
CURL_CMD="${CURL_BIN:-curl}"
BRIDGE_URL="${DALTON_BRIDGE_LOCAL_URL:-${BRIDGE_URL:-http://127.0.0.1:8960}}"
BRIDGE_ENV_FILE="${DALTON_BRIDGE_ENV_FILE:-${HOME:-}/.config/dalton/researchdata-bridge.env}"
PY_CMD="${PYTHON_BIN:-python3}"

usage() {
  cat <<'EOF'
Usage: wait_backend_idle.sh [--max-wait-seconds N] [--check-bridge-healthz] [--check-bridge-drain]

Without arguments, runs legacy routine deploy idle wait (720 x 60s).
With any flag, enters maintenance mode and also waits for claimed dalton_data_task_slot
and active dalton_data_task_runs (host & container tasks) to drain to 0 twice in a row.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --max-wait-seconds)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --max-wait-seconds requires an integer value" >&2
        exit 2
      fi
      MAX_WAIT_SECONDS="$2"
      shift 2
      ;;
    --max-wait-seconds=*)
      MAX_WAIT_SECONDS="${1#--max-wait-seconds=}"
      shift
      ;;
    --check-bridge-healthz)
      CHECK_BRIDGE_HEALTHZ=1
      shift
      ;;
    --check-bridge-drain)
      CHECK_BRIDGE_DRAIN=1
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

case "$MAX_WAIT_SECONDS" in
  ''|*[!0-9]*)
    echo "ERROR: --max-wait-seconds must be a non-negative integer (got '$MAX_WAIT_SECONDS')" >&2
    exit 2
    ;;
esac

_run_psql_count() {
  local sql="$1"
  local out=""
  local rc=0
  out="$("$DOCKER_CMD" exec "$PG_CONTAINER" psql -U postgres -d postgres -Atc "$sql" 2>/dev/null)" || rc=$?
  out="$(printf '%s' "$out" | tr -d '[:space:]')"
  if [ "$rc" -ne 0 ]; then
    printf 'err'
    return 1
  fi
  case "$out" in
    ''|*[!0-9]*)
      printf 'err'
      return 1
      ;;
  esac
  printf '%s' "$out"
  return 0
}

_read_bridge_admin_token() {
  if [ ! -f "$BRIDGE_ENV_FILE" ]; then
    return 1
  fi
  "$PY_CMD" - "$BRIDGE_ENV_FILE" <<'PY'
import sys
path = sys.argv[1]
val = ""
try:
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            k, v = line.split("=", 1)
            if k.strip() == "DALTON_BRIDGE_ADMIN_TOKEN":
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                val = v
except OSError:
    sys.exit(1)
if not val:
    sys.exit(1)
sys.stdout.write(val)
PY
}

_check_bridge_healthz_idle() {
  local body=""
  local rc=0
  body="$("$CURL_CMD" -fsS --max-time 5 "${BRIDGE_URL%/}/healthz" 2>/dev/null)" || rc=$?
  if [ "$rc" -ne 0 ] || [ -z "$body" ]; then
    printf 'err(http)'
    return 1
  fi
  "$PY_CMD" - "$body" <<'PY'
import json, sys
try:
    d = json.loads(sys.argv[1])
    if not isinstance(d, dict):
        raise ValueError("not_dict")
except Exception:
    print("err(json)")
    sys.exit(1)

def _int_field(name, required=True):
    if name not in d:
        return (None if required else 0)
    v = d[name]
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        return None
    return v

inflight = _int_field("inflight", required=True)
queued = _int_field("queued", required=True)
ingest = _int_field("ingest_inflight", required=False)
if inflight is None or queued is None or ingest is None:
    print(f"err(fields:inflight={inflight},queued={queued},ingest={ingest})")
    sys.exit(1)
print(f"inflight={inflight},queued={queued},ingest_inflight={ingest}")
sys.exit(0 if (inflight == 0 and queued == 0 and ingest == 0) else 1)
PY
}

_check_bridge_drain_idle() {
  local token=""
  if ! token="$(_read_bridge_admin_token 2>/dev/null)"; then
    printf 'err(no_admin_token)'
    return 1
  fi
  local body=""
  local rc=0
  body="$("$CURL_CMD" -fsS --max-time 5 -H "Authorization: Bearer ${token}" "${BRIDGE_URL%/}/admin/drain" 2>/dev/null)" || rc=$?
  if [ "$rc" -ne 0 ] || [ -z "$body" ]; then
    printf 'err(http)'
    return 1
  fi
  "$PY_CMD" - "$body" <<'PY'
import json, sys
try:
    d = json.loads(sys.argv[1])
    if not isinstance(d, dict):
        raise ValueError("not_dict")
except Exception:
    print("err(json)")
    sys.exit(1)

vals = {}
for name in ("inflight", "queued", "ingest_inflight"):
    v = d.get(name)
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        print(f"err(field:{name}={v!r})")
        sys.exit(1)
    vals[name] = v

print(
    f"inflight={vals['inflight']},queued={vals['queued']},ingest_inflight={vals['ingest_inflight']}"
)
sys.exit(
    0
    if (vals["inflight"] == 0 and vals["queued"] == 0 and vals["ingest_inflight"] == 0)
    else 1
)
PY
}

SQL_THREADS="select count(*) from conversation_threads where current_status='in_progress'"
SQL_SUBAGENTS="select count(*) from subagent_runs where status='in_progress'"
SQL_DATA_SLOTS="select count(*) from dalton_data_task_slot where slot_state <> 'idle' or active_run_id is not null"
SQL_DATA_RUNS="select count(*) from dalton_data_task_runs where status in ('starting', 'running')"

start_epoch="$(date +%s)"
deadline_epoch=$((start_epoch + MAX_WAIT_SECONDS))
consecutive_ok=0
checks=0

last_t="err"
last_s="err"
last_ds="err"
last_dr="err"
last_bh="skip"
last_bd="skip"

while true; do
  checks=$((checks + 1))
  all_zero=1

  last_t="$(_run_psql_count "$SQL_THREADS")" || all_zero=0
  last_s="$(_run_psql_count "$SQL_SUBAGENTS")" || all_zero=0
  last_ds="$(_run_psql_count "$SQL_DATA_SLOTS")" || all_zero=0
  last_dr="$(_run_psql_count "$SQL_DATA_RUNS")" || all_zero=0

  if [ "$last_t" != "0" ] || [ "$last_s" != "0" ] || [ "$last_ds" != "0" ] || [ "$last_dr" != "0" ]; then
    all_zero=0
  fi

  if [ "$CHECK_BRIDGE_HEALTHZ" -eq 1 ]; then
    last_bh="$(_check_bridge_healthz_idle)" || all_zero=0
  fi

  if [ "$CHECK_BRIDGE_DRAIN" -eq 1 ]; then
    last_bd="$(_check_bridge_drain_idle)" || all_zero=0
  fi

  if [ "$all_zero" -eq 1 ]; then
    consecutive_ok=$((consecutive_ok + 1))
  else
    consecutive_ok=0
  fi

  if [ "$consecutive_ok" -ge 2 ]; then
    echo "idle (maintenance mode, checks=${checks}) threads=${last_t} subagents=${last_s} data_slots=${last_ds} data_runs=${last_dr} bridge_healthz=${last_bh} bridge_drain=${last_bd}"
    exit 0
  fi

  now_epoch="$(date +%s)"
  if [ "$now_epoch" -ge "$deadline_epoch" ]; then
    echo "still_busy (maintenance mode, max_wait=${MAX_WAIT_SECONDS}s, checks=${checks}) threads=${last_t} subagents=${last_s} data_slots=${last_ds} data_runs=${last_dr} bridge_healthz=${last_bh} bridge_drain=${last_bd}"
    exit 1
  fi

  remaining=$((deadline_epoch - now_epoch))
  sleep_s="$POLL_S"
  # Support integer or fractional WAIT_IDLE_POLL_S cleanly
  "$PY_CMD" -c "import time; time.sleep(min(float('$sleep_s'), float('$remaining')))" 2>/dev/null || sleep 1
done
