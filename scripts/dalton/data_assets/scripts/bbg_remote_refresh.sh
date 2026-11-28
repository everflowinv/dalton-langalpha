#!/bin/bash
# Refresh Bloomberg data on Windows and validate the downloaded exports.
# Usage: bash scripts/bbg_remote_refresh.sh [--pull-only]
# --pull-only downloads existing exports without starting a refresh.
# Try the CSV first, then the workbook. Replace the local CSV only after validation.
#
# 配置项（优先读环境变量，其次读 $DALTON_DATA_TASKS_CONFIG 或 <workspace>/config/data_tasks.env）：
#   DALTON_DATA_TASK_BBG_HOST         Windows BBG 机器 SSH 目标（兼容 DALTON_BBG_HOST，必填）
#   DALTON_DATA_TASK_BBG_CSV_REMOTE   Windows 端 BBG 导出 CSV 路径（兼容 DALTON_BBG_CSV_REMOTE，必填）
#   DALTON_DATA_TASK_BBG_XLSX_REMOTE  Windows 端 BBG 工作簿路径（兼容 DALTON_BBG_XLSX_REMOTE，必填）
#   DALTON_DATA_TASK_BBG_REFRESH_CMD  Windows 端刷新命令（兼容 DALTON_BBG_REFRESH_CMD，非 --pull-only 必填）
#   DALTON_DATA_TASK_WORKSPACE        运行时工作区（兼容 DALTON_WORKSPACE / OPENCLAW_WORKSPACE / WORKDIR，默认当前目录）

set -euo pipefail

_read_cfg_key() {
    local key="$1" file="$2" line lhs val last_val="" found=0
    [[ -f "$file" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        line="${line#"${line%%[![:space:]]*}"}"
        [[ -z "$line" || "$line" == \#* ]] && continue
        if [[ "$line" == export\ * ]]; then
            line="${line#export }"
            line="${line#"${line%%[![:space:]]*}"}"
        fi
        [[ "$line" == *=* ]] || continue
        lhs="${line%%=*}"
        lhs="${lhs%"${lhs##*[![:space:]]}"}"
        if [[ "$lhs" == "$key" ]]; then
            val="${line#*=}"
            val="${val#"${val%%[![:space:]]*}"}"
            val="${val%"${val##*[![:space:]]}"}"
            if [[ ${#val} -ge 2 ]]; then
                if [[ "${val:0:1}" == '"' && "${val: -1}" == '"' ]]; then
                    val="${val:1:${#val}-2}"
                elif [[ "${val:0:1}" == "'" && "${val: -1}" == "'" ]]; then
                    val="${val:1:${#val}-2}"
                fi
            fi
            last_val="$val"
            found=1
        fi
    done < "$file"
    if [[ "$found" -eq 1 ]]; then
        printf '%s' "$last_val"
    fi
}

_read_first_cfg_key() {
    local file="$1"
    shift
    local k val
    for k in "$@"; do
        val="$(_read_cfg_key "$k" "$file")"
        if [[ -n "$val" ]]; then
            printf '%s' "$val"
            return 0
        fi
    done
}

RUNTIME_PWD="${PWD}"
BOOT_CFG="${DALTON_DATA_TASKS_CONFIG:-$RUNTIME_PWD/config/data_tasks.env}"
WORKSPACE="${DALTON_DATA_TASK_WORKSPACE:-${DALTON_WORKSPACE:-${OPENCLAW_WORKSPACE:-${WORKDIR:-$(_read_first_cfg_key "$BOOT_CFG" "DALTON_DATA_TASK_WORKSPACE" "DALTON_WORKSPACE")}}}}"
WORKSPACE="${WORKSPACE:-$RUNTIME_PWD}"
CFG_FILE="${DALTON_DATA_TASKS_CONFIG:-$WORKSPACE/config/data_tasks.env}"

BBG_HOST="${DALTON_DATA_TASK_BBG_HOST:-${DALTON_BBG_HOST:-$(_read_first_cfg_key "$CFG_FILE" "DALTON_DATA_TASK_BBG_HOST" "DALTON_BBG_HOST")}}"
BBG_CSV_REMOTE="${DALTON_DATA_TASK_BBG_CSV_REMOTE:-${DALTON_BBG_CSV_REMOTE:-$(_read_first_cfg_key "$CFG_FILE" "DALTON_DATA_TASK_BBG_CSV_REMOTE" "DALTON_BBG_CSV_REMOTE")}}"
BBG_XLSX_REMOTE="${DALTON_DATA_TASK_BBG_XLSX_REMOTE:-${DALTON_BBG_XLSX_REMOTE:-$(_read_first_cfg_key "$CFG_FILE" "DALTON_DATA_TASK_BBG_XLSX_REMOTE" "DALTON_BBG_XLSX_REMOTE")}}"
BBG_REFRESH_CMD="${DALTON_DATA_TASK_BBG_REFRESH_CMD:-${DALTON_BBG_REFRESH_CMD:-$(_read_first_cfg_key "$CFG_FILE" "DALTON_DATA_TASK_BBG_REFRESH_CMD" "DALTON_BBG_REFRESH_CMD")}}"
INCOMING_CSV="$WORKSPACE/temp/bbg_data_remote.incoming.csv"
INCOMING_XLSX="$WORKSPACE/temp/bbg_data_remote.incoming.xlsx"
LOCAL_XLSX="$WORKSPACE/temp/bbg_data_remote.xlsx"
LOCAL_CSV="$WORKSPACE/temp/bbg_data_latest.csv"
CONVERTER="$WORKSPACE/scripts/bbg_export_to_csv.py"
DEFAULT_PYTHON="/opt/homebrew/bin/python3.14"
[[ -x "$DEFAULT_PYTHON" ]] || DEFAULT_PYTHON="$(command -v python3 || echo python3)"
PYTHON_BIN="${DALTON_PYTHON_BIN:-$DEFAULT_PYTHON}"
LOG="$WORKSPACE/temp/bbg_remote_refresh.log"
SSH_OPTS="-o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=3"

TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")

log() {
    echo "[$TIMESTAMP] $*" | tee -a "$LOG"
}

if [[ -z "$BBG_HOST" ]]; then
    echo "ERROR: 未配置 DALTON_DATA_TASK_BBG_HOST（兼容 DALTON_BBG_HOST；请设置环境变量或在 config/data_tasks.env 中配置）" >&2
    exit 1
fi
if [[ -z "$BBG_CSV_REMOTE" ]]; then
    echo "ERROR: 未配置 DALTON_DATA_TASK_BBG_CSV_REMOTE（兼容 DALTON_BBG_CSV_REMOTE；请设置环境变量或在 config/data_tasks.env 中配置）" >&2
    exit 1
fi
if [[ -z "$BBG_XLSX_REMOTE" ]]; then
    echo "ERROR: 未配置 DALTON_DATA_TASK_BBG_XLSX_REMOTE（兼容 DALTON_BBG_XLSX_REMOTE；请设置环境变量或在 config/data_tasks.env 中配置）" >&2
    exit 1
fi
if [[ "${1:-}" != "--pull-only" && -z "$BBG_REFRESH_CMD" ]]; then
    echo "ERROR: 未配置 DALTON_DATA_TASK_BBG_REFRESH_CMD（兼容 DALTON_BBG_REFRESH_CMD；请设置环境变量或在 config/data_tasks.env 中配置）" >&2
    exit 1
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
    log "ERROR: Python not found: $PYTHON_BIN"
    exit 1
fi

# Check connectivity
if ! ssh $SSH_OPTS "$BBG_HOST" "echo ok" >/dev/null 2>&1; then
    log "ERROR: Cannot reach BBG Windows machine ($BBG_HOST)"
    exit 1
fi

if [[ "${1:-}" != "--pull-only" ]]; then
    log "Triggering BBG refresh on Windows..."
    # Run the refresh script. It opens Excel, refreshes BBG data, exports CSV.
    # This may take 2-5 minutes depending on Bloomberg response time.
    if ! ssh $SSH_OPTS "$BBG_HOST" "$BBG_REFRESH_CMD" 2>&1 | tee -a "$LOG"; then
        log "WARNING: BBG refresh reported failure (may still have exported); continuing to pull"
    fi
    log "BBG refresh completed."
fi

# Pull both artifacts; a missing/unreachable one just loses its turn.
log "Pulling exports..."
rm -f "$INCOMING_CSV" "$INCOMING_XLSX"
scp $SSH_OPTS "$BBG_HOST:$BBG_CSV_REMOTE" "$INCOMING_CSV" 2>&1 | tee -a "$LOG" || log "WARNING: could not pull $BBG_CSV_REMOTE"
scp $SSH_OPTS "$BBG_HOST:$BBG_XLSX_REMOTE" "$INCOMING_XLSX" 2>&1 | tee -a "$LOG" || log "WARNING: could not pull $BBG_XLSX_REMOTE"

INSTALLED=""
CONVERT_JSON=""
for CANDIDATE in "$INCOMING_CSV" "$INCOMING_XLSX"; do
    [[ -s "$CANDIDATE" ]] || continue
    if CONVERT_JSON=$("$PYTHON_BIN" "$CONVERTER" --input "$CANDIDATE" --output "$LOCAL_CSV" 2>>"$LOG"); then
        INSTALLED="$CANDIDATE"
        break
    fi
    log "REJECTED $(basename "$CANDIDATE"): failed validation (see error above)"
done

[[ -s "$INCOMING_XLSX" ]] && mv "$INCOMING_XLSX" "$LOCAL_XLSX"

if [[ -z "$INSTALLED" ]]; then
    log "ERROR: no valid Bloomberg export; local CSV left untouched at $(date -r "$LOCAL_CSV" '+%Y-%m-%d %H:%M' 2>/dev/null || echo missing)"
    echo "{\"ok\": false, \"error\": \"no valid Bloomberg export\", \"hint\": \"Check the BBG terminal is logged in on the Windows box, then rerun\", \"timestamp\": \"$TIMESTAMP\"}"
    exit 1
fi

rm -f "$INCOMING_CSV"
LINES=$(wc -l < "$LOCAL_CSV" | tr -d ' ')
log "OK: installed $LINES lines from $(basename "$INSTALLED") -> $LOCAL_CSV"
log "$CONVERT_JSON"

echo "{\"ok\": true, \"lines\": $LINES, \"source\": \"$(basename "$INSTALLED")\", \"path\": \"$LOCAL_CSV\", \"timestamp\": \"$TIMESTAMP\", \"detail\": $CONVERT_JSON}"
