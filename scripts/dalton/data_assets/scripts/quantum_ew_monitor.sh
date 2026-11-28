#!/bin/bash
# quantum-ew-monitor: Calculate EW index gain from recent low and send daily alert
# Called by cron or heartbeat
#
# 配置项（优先读环境变量，其次读 $DALTON_DATA_TASKS_CONFIG 或 <runtime_workspace>/config/data_tasks.env）：
#   DALTON_DATA_TASK_WORKSPACE  数据任务工作目录（兼容 DALTON_WORKSPACE / OPENCLAW_WORKSPACE / WORKDIR；均未设置时使用当前运行目录 $PWD）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_PWD="${PWD}"
CFG_FILE="${DALTON_DATA_TASKS_CONFIG:-$RUNTIME_PWD/config/data_tasks.env}"

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

WORKSPACE="${DALTON_DATA_TASK_WORKSPACE:-${DALTON_WORKSPACE:-${OPENCLAW_WORKSPACE:-${WORKDIR:-}}}}"
if [[ -z "$WORKSPACE" ]]; then
  WORKSPACE="$(_read_cfg_key "DALTON_DATA_TASK_WORKSPACE" "$CFG_FILE")"
fi
if [[ -z "$WORKSPACE" ]]; then
  WORKSPACE="$(_read_cfg_key "DALTON_WORKSPACE" "$CFG_FILE")"
fi
if [[ -z "$WORKSPACE" ]]; then
  WORKSPACE="$RUNTIME_PWD"
fi

SCRIPT="$WORKSPACE/scripts/quantum_ew_daily_v4.py"
if [[ ! -f "$SCRIPT" && -f "${SCRIPT_DIR}/quantum_ew_daily_v4.py" ]]; then
  SCRIPT="${SCRIPT_DIR}/quantum_ew_daily_v4.py"
fi

# Run the calculation script
python3 "$SCRIPT" 2>&1
