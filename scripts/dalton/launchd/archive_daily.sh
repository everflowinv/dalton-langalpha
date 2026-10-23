#!/usr/bin/env bash
# Launchd entrypoint for Dalton daily OpenClaw conversation archive.
# Checks the bridge-managed state file before running; when disabled, logs "已关闭" and exits 0.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${DALTON_REPO_ROOT:-$(cd "${SCRIPT_DIR}/../../.." && pwd)}"
STATE_FILE="${DALTON_ARCHIVE_STATE_FILE:-${HOME}/.config/dalton/conversation-archive-state.json}"
STATUS_FILE="${DALTON_ARCHIVE_STATUS_FILE:-${HOME}/.cache/dalton/conversation-archive-status.json}"
PID_FILE="${DALTON_ARCHIVE_PID_FILE:-$(dirname "${STATUS_FILE}")/conversation-archive.pid}"
ARCHIVE_SCRIPT="${DALTON_CONVERSATION_ARCHIVE_PY:-${REPO_ROOT}/scripts/dalton/conversation_archive.py}"

if [[ -x "${DALTON_REPO_PYTHON:-}" ]]; then
    PYTHON_BIN="${DALTON_REPO_PYTHON}"
elif [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
elif [[ -x "/opt/homebrew/bin/python3" ]]; then
    PYTHON_BIN="/opt/homebrew/bin/python3"
else
    PYTHON_BIN="$(command -v python3 || echo python3)"
fi

write_status_file() {
    local started_at="$1"
    local finished_at="$2"
    local exit_code="$3"
    local last_result="${4:-}"
    if [[ -z "${STATUS_FILE:-}" ]]; then
        return 0
    fi
    mkdir -p "$(dirname "${STATUS_FILE}")" || echo "[WARN] archive_daily: failed mkdir for ${STATUS_FILE}" >&2
    "${PYTHON_BIN}" - "${STATUS_FILE}" "${started_at}" "${finished_at}" "${exit_code}" "${last_result}" <<'PY'
import fcntl
import json
import os
import sys
import uuid
from pathlib import Path

status_path = Path(sys.argv[1])
started_at = sys.argv[2]
finished_at = sys.argv[3]
exit_code = int(sys.argv[4])
explicit_result = sys.argv[5] if len(sys.argv) > 5 else ""

if explicit_result:
    last_result = explicit_result
elif exit_code == 0:
    last_result = "成功 (exit_code=0)"
elif exit_code == 2:
    last_result = "已在运行，跳过"
elif exit_code == 3:
    last_result = "部分未完成 (exit_code=3)"
else:
    last_result = f"失败 (exit_code={exit_code})"

payload = {
    "last_run_at": started_at,
    "finished_at": finished_at,
    "exit_code": exit_code,
    "last_result": last_result,
}
try:
    status_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = status_path.with_name(f"{status_path.name}.lock")
    tmp_path = status_path.with_name(f".{status_path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp_path, status_path)
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
except Exception as exc:
    print(f"[WARN] archive_daily: failed writing status file {status_path.name}: {type(exc).__name__}", file=sys.stderr)
    sys.exit(1)
PY
}

# Check bridge-managed schedule state file unless forced by manual run_now
if [[ "${DALTON_ARCHIVE_FORCE_RUN:-0}" != "1" ]] && [[ -e "${STATE_FILE}" || -L "${STATE_FILE}" ]]; then
    if ! IS_DISABLED="$("${PYTHON_BIN}" - "${STATE_FILE}" <<'PY'
import json
import sys
from pathlib import Path

p = Path(sys.argv[1])
try:
    raw = p.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError("empty state file")
    low = raw.lower()
    if low in ("false", "0", "off", "disabled"):
        print("1")
    elif low in ("true", "1", "on", "enabled"):
        print("0")
    else:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("enabled"), bool):
            raise ValueError("invalid state file schema")
        print("1" if data["enabled"] is False else "0")
except Exception as exc:
    print(f"[ERROR] archive_daily: failed reading state file {p.name}: {type(exc).__name__}", file=sys.stderr)
    sys.exit(1)
PY
)"; then
        NOW_ISO="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
        ERR_MSG="${NOW_ISO} [ERROR] conversation_archive: 读取状态文件失败或文件已损坏 (${STATE_FILE})，拒绝运行归档"
        echo "${ERR_MSG}" >&2
        if [[ -n "${DALTON_ARCHIVE_LOG_FILE:-}" ]]; then
            mkdir -p "$(dirname "${DALTON_ARCHIVE_LOG_FILE}")" 2>/dev/null || true
            echo "${ERR_MSG}" >> "${DALTON_ARCHIVE_LOG_FILE}" 2>/dev/null || true
        fi
        write_status_file "${NOW_ISO}" "${NOW_ISO}" "1" "状态文件损坏或读取失败" || true
        exit 1
    fi
    if [[ "${IS_DISABLED}" == "1" ]]; then
        NOW_ISO="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
        MSG="${NOW_ISO} [INFO] conversation_archive: 已关闭（状态文件 ${STATE_FILE} 为 disabled，跳过定时运行）"
        echo "${MSG}"
        if [[ -n "${DALTON_ARCHIVE_LOG_FILE:-}" ]]; then
            mkdir -p "$(dirname "${DALTON_ARCHIVE_LOG_FILE}")" 2>/dev/null || true
            echo "${MSG}" >> "${DALTON_ARCHIVE_LOG_FILE}" 2>/dev/null || true
        fi
        write_status_file "${NOW_ISO}" "${NOW_ISO}" "0" "已关闭" || true
        exit 0
    fi
fi

# Atomically check existing PID and claim PID file under the shared status-file directory flock
CLAIM_RESULT="$("${PYTHON_BIN}" - "${STATUS_FILE}" "${PID_FILE}" "$$" <<'PY'
import fcntl
import os
import sys
import uuid
from pathlib import Path

status_path = Path(sys.argv[1])
pid_path = Path(sys.argv[2])
my_pid = int(sys.argv[3])


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


status_path.parent.mkdir(parents=True, exist_ok=True)
pid_path.parent.mkdir(parents=True, exist_ok=True)
lock_path = status_path.with_name(f"{status_path.name}.lock")
with lock_path.open("a+", encoding="utf-8") as lock_fh:
    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
    try:
        if pid_path.is_file():
            try:
                raw = pid_path.read_text(encoding="utf-8").strip()
                existing_pid = int(raw)
                if existing_pid != my_pid and pid_alive(existing_pid):
                    print(f"BUSY:{existing_pid}")
                    sys.exit(0)
            except Exception:
                pass
        tmp_path = pid_path.with_name(f".{pid_path.name}.{my_pid}.{uuid.uuid4().hex[:8]}.tmp")
        tmp_path.write_text(f"{my_pid}\n", encoding="utf-8")
        os.replace(tmp_path, pid_path)
        print(f"CLAIMED:{my_pid}")
    finally:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
PY
)" || exit 1

if [[ "${CLAIM_RESULT}" == BUSY:* ]]; then
    EXISTING_PID="${CLAIM_RESULT#BUSY:}"
    NOW_ISO="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    echo "${NOW_ISO} [INFO] conversation_archive: 已在运行 (pid=${EXISTING_PID})，跳过"
    write_status_file "${NOW_ISO}" "${NOW_ISO}" "2" "已在运行，跳过" || true
    exit 0
fi

cleanup_pid_file() {
    "${PYTHON_BIN}" - "${STATUS_FILE}" "${PID_FILE}" "$$" <<'PY' 2>/dev/null || true
import fcntl
import sys
from pathlib import Path

status_path = Path(sys.argv[1])
pid_path = Path(sys.argv[2])
my_pid = str(sys.argv[3]).strip()
if not pid_path.is_file():
    sys.exit(0)
lock_path = status_path.with_name(f"{status_path.name}.lock")
lock_path.parent.mkdir(parents=True, exist_ok=True)
with lock_path.open("a+", encoding="utf-8") as lock_fh:
    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
    try:
        if pid_path.is_file() and pid_path.read_text(encoding="utf-8").strip() == my_pid:
            pid_path.unlink(missing_ok=True)
    finally:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
PY
}
trap cleanup_pid_file EXIT

# Load runtime environment files if present
if [[ -f "${REPO_ROOT}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${REPO_ROOT}/.env" 2>/dev/null || true
    set +a
fi

BRIDGE_ENV="${DALTON_BRIDGE_ENV_FILE:-${HOME}/.config/dalton/researchdata-bridge.env}"
if [[ -f "${BRIDGE_ENV}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${BRIDGE_ENV}" 2>/dev/null || true
    set +a
fi

STARTED_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
"${PYTHON_BIN}" "${ARCHIVE_SCRIPT}" run "$@"
EXIT_CODE=$?
FINISHED_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

write_status_file "${STARTED_AT}" "${FINISHED_AT}" "${EXIT_CODE}" || true

exit "${EXIT_CODE}"
