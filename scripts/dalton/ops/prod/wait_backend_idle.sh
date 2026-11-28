#!/bin/bash
<<<<<<< HEAD
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
#   Every `docker exec psql`, HTTP probe, and sleep shares one monotonic deadline;
#   returning idle re-checks the deadline first and fails on any overrun.
#   Self-contained (does not source external files) so it can run from recovery-bin/.

DOCKER_CMD="${DOCKER_BIN:-docker}"
PG_CONTAINER="${DALTON_POSTGRES_CONTAINER:-dalton-langalpha-postgres-1}"

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
=======
# Wait until no chat turn and no subagent run is in progress (host archive runs are ignored:
# a backend restart does not touch them). Needs 2 consecutive idle checks 60s apart.
# Stale automation threads (>15m since last update whose associated automation executions are
# all in terminal status and have no live conversation_responses row) do not count as busy.
SLEEP_SECONDS="${WAIT_BACKEND_IDLE_SLEEP_SECONDS:-60}"
MAX_CHECKS="${WAIT_BACKEND_IDLE_MAX_CHECKS:-720}"
POSTGRES_CONTAINER="${WAIT_BACKEND_IDLE_POSTGRES_CONTAINER:-dalton-langalpha-postgres-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
THREAD_BUSY_SQL_FILE="${WAIT_BACKEND_IDLE_SQL_FILE:-${SCRIPT_DIR}/backend_busy_threads.sql}"
# The production entry copies only this script and deploy_main.sh to a temp dir; read the shared
# query from the repo then.
if [ ! -f "$THREAD_BUSY_SQL_FILE" ] && [ -z "${WAIT_BACKEND_IDLE_SQL_FILE:-}" ] && [ -n "${DALTON_REPO_ROOT:-}" ]; then
  THREAD_BUSY_SQL_FILE="${DALTON_REPO_ROOT}/scripts/dalton/ops/prod/backend_busy_threads.sql"
fi
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
>>>>>>> refs/lane/106
done

case "$MAX_WAIT_SECONDS" in
  ''|*[!0-9]*)
    echo "ERROR: --max-wait-seconds must be a non-negative integer (got '$MAX_WAIT_SECONDS')" >&2
    exit 2
    ;;
esac

PYTHONDONTWRITEBYTECODE=1 "$PY_CMD" -B - \
  "$DOCKER_CMD" \
  "$PG_CONTAINER" \
  "$CURL_CMD" \
  "$BRIDGE_URL" \
  "$BRIDGE_ENV_FILE" \
  "$MAX_WAIT_SECONDS" \
  "$POLL_S" \
  "$CHECK_BRIDGE_HEALTHZ" \
  "$CHECK_BRIDGE_DRAIN" <<'PY'
import json
import os
import subprocess
import sys
import tempfile
import time

(
    docker_cmd,
    pg_container,
    curl_cmd,
    bridge_url,
    bridge_env_file,
    max_wait_raw,
    poll_s_raw,
    check_healthz_raw,
    check_drain_raw,
) = sys.argv[1:10]

max_wait_seconds = int(max_wait_raw)
try:
    poll_s = float(poll_s_raw)
    if poll_s < 0 or poll_s != poll_s:
        poll_s = 10.0
except ValueError:
    poll_s = 10.0

check_bridge_healthz = check_healthz_raw == "1"
check_bridge_drain = check_drain_raw == "1"

SQL_THREADS = "select count(*) from conversation_threads where current_status='in_progress'"
SQL_SUBAGENTS = "select count(*) from subagent_runs where status='in_progress'"
SQL_DATA_SLOTS = "select count(*) from dalton_data_task_slot where slot_state <> 'idle' or active_run_id is not null"
SQL_DATA_RUNS = "select count(*) from dalton_data_task_runs where status in ('starting', 'running')"

start_mono = time.monotonic()
deadline_mono = start_mono + float(max_wait_seconds)


def remaining_s() -> float:
    return deadline_mono - time.monotonic()


def read_bridge_admin_token(path: str) -> str:
    if not path or not os.path.isfile(path):
        return ""
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
        return ""
    return val


def run_psql_count(sql: str) -> tuple[str, bool]:
    rem = remaining_s()
    if rem <= 0:
        return "err(timeout)", False
    try:
        proc = subprocess.run(
            [docker_cmd, "exec", pg_container, "psql", "-U", "postgres", "-d", "postgres", "-Atc", sql],
            capture_output=True,
            text=True,
            timeout=rem,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "err(timeout)", False
    except OSError:
        return "err", False
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out.isdigit():
        return "err", False
    return out, True


def _curl_escape_header(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def run_curl_get(url: str, *, bearer_token: str | None = None) -> tuple[str | None, str | None]:
    rem = remaining_s()
    if rem <= 0:
        return None, "err(timeout)"
    max_time = min(5.0, rem)
    cfg_path = None
    cmd = [curl_cmd, "-fsS", "--max-time", f"{max_time:.3f}"]
    try:
        if bearer_token is not None:
            fd, cfg_path = tempfile.mkstemp(prefix=".dalton-curl-", suffix=".cfg")
            try:
                os.fchmod(fd, 0o600)
                header_val = _curl_escape_header(f"Authorization: Bearer {bearer_token}")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(f'header = "{header_val}"\n')
                    fh.flush()
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            cmd.extend(["-K", cfg_path])
        cmd.append(url)
        rem_exec = remaining_s()
        if rem_exec <= 0:
            return None, "err(timeout)"
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=rem_exec,
            check=False,
        )
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return None, "err(http)"
        return proc.stdout, None
    except subprocess.TimeoutExpired:
        return None, "err(timeout)"
    except OSError:
        return None, "err(http)"
    finally:
        if cfg_path:
            try:
                os.unlink(cfg_path)
            except OSError:
                pass


def check_healthz_idle() -> tuple[str, bool]:
    body, err = run_curl_get(f"{bridge_url.rstrip('/')}/healthz")
    if err is not None or body is None:
        return err or "err(http)", False
    try:
        d = json.loads(body)
        if not isinstance(d, dict):
            raise ValueError("not_dict")
    except Exception:
        return "err(json)", False

    def _int_field(name: str, required: bool = True):
        if name not in d:
            return None if required else 0
        v = d[name]
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            return None
        return v

    inflight = _int_field("inflight", required=True)
    queued = _int_field("queued", required=True)
    ingest = _int_field("ingest_inflight", required=False)
    if inflight is None or queued is None or ingest is None:
        return f"err(fields:inflight={inflight},queued={queued},ingest={ingest})", False
    text = f"inflight={inflight},queued={queued},ingest_inflight={ingest}"
    return text, (inflight == 0 and queued == 0 and ingest == 0)


def check_drain_idle() -> tuple[str, bool]:
    token = read_bridge_admin_token(bridge_env_file)
    if not token:
        return "err(no_admin_token)", False
    body, err = run_curl_get(f"{bridge_url.rstrip('/')}/admin/drain", bearer_token=token)
    if err is not None or body is None:
        return err or "err(http)", False
    try:
        d = json.loads(body)
        if not isinstance(d, dict):
            raise ValueError("not_dict")
    except Exception:
        return "err(json)", False

    vals = {}
    for name in ("inflight", "queued", "ingest_inflight"):
        v = d.get(name)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            return f"err(field:{name}={v!r})", False
        vals[name] = v
    text = (
        f"inflight={vals['inflight']},queued={vals['queued']},ingest_inflight={vals['ingest_inflight']}"
    )
    return text, (vals["inflight"] == 0 and vals["queued"] == 0 and vals["ingest_inflight"] == 0)


consecutive_ok = 0
checks = 0
last_t = "err"
last_s = "err"
last_ds = "err"
last_dr = "err"
last_bh = "skip"
last_bd = "skip"


def fail_still_busy() -> None:
    print(
        f"still_busy (maintenance mode, max_wait={max_wait_seconds}s, checks={checks}) "
        f"threads={last_t} subagents={last_s} data_slots={last_ds} data_runs={last_dr} "
        f"bridge_healthz={last_bh} bridge_drain={last_bd}"
    )
    sys.exit(1)


while True:
    if remaining_s() <= 0:
        fail_still_busy()

    checks += 1
    all_zero = True

    last_t, ok_t = run_psql_count(SQL_THREADS)
    if not ok_t or last_t != "0":
        all_zero = False
    if remaining_s() <= 0:
        fail_still_busy()

    last_s, ok_s = run_psql_count(SQL_SUBAGENTS)
    if not ok_s or last_s != "0":
        all_zero = False
    if remaining_s() <= 0:
        fail_still_busy()

    last_ds, ok_ds = run_psql_count(SQL_DATA_SLOTS)
    if not ok_ds or last_ds != "0":
        all_zero = False
    if remaining_s() <= 0:
        fail_still_busy()

    last_dr, ok_dr = run_psql_count(SQL_DATA_RUNS)
    if not ok_dr or last_dr != "0":
        all_zero = False
    if remaining_s() <= 0:
        fail_still_busy()

    if check_bridge_healthz:
        last_bh, ok_bh = check_healthz_idle()
        if not ok_bh:
            all_zero = False
        if remaining_s() <= 0:
            fail_still_busy()

    if check_bridge_drain:
        last_bd, ok_bd = check_drain_idle()
        if not ok_bd:
            all_zero = False
        if remaining_s() <= 0:
            fail_still_busy()

    if all_zero:
        consecutive_ok += 1
    else:
        consecutive_ok = 0

    # Re-check deadline BEFORE returning success: any overrun fails closed.
    if remaining_s() <= 0:
        fail_still_busy()

    if consecutive_ok >= 2:
        print(
            f"idle (maintenance mode, checks={checks}) "
            f"threads={last_t} subagents={last_s} data_slots={last_ds} data_runs={last_dr} "
            f"bridge_healthz={last_bh} bridge_drain={last_bd}"
        )
        sys.exit(0)

    rem = remaining_s()
    if rem <= 0:
        fail_still_busy()
    time.sleep(min(poll_s, rem))
PY
