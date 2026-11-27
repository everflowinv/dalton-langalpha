#!/usr/bin/env bash
# 跑一条输出 JSON 的采集命令，把 `ok:false` 也算作失败。
#
# 用途：那几个原来靠 agentTurn「跑脚本 + 发飞书」的 cron，现在只保留采集这一半，payload 改成
# command。command 的成败只看退出码，而这些 skill 的 CLI 在部分失败路径上仍然退出 0、只把
# ok:false 写进 JSON——直接换成 command 会得到一个天天报 ok 的哑巴 cron，正是 BBG 那次
# 三天没人发现的同一个坑。所以这里额外校验最后一行 JSON 的 ok 字段。
#
# 用法: bash scripts/run_json_task.sh <命令...>
# 退出码: 0 = 命令退出 0 且 JSON 没说 ok:false；1 = 其余情况。stdout 原样透传。
#
# 配置项（优先读环境变量，其次读 $DALTON_DATA_TASKS_CONFIG 或 <runtime_workspace>/config/data_tasks.env）：
#   DALTON_DATA_TASK_WORKSPACE  数据任务工作目录（兼容 DALTON_WORKSPACE / OPENCLAW_WORKSPACE / WORKDIR；均未设置时使用当前运行目录 $PWD）
set -uo pipefail

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

cd "$WORKSPACE" || exit 1

out="$("$@" 2>&1)"
code=$?
printf '%s\n' "$out"
if [ "$code" -ne 0 ]; then
  echo "run_json_task: 命令退出码 $code — $*" >&2
  exit 1
fi

printf '%s' "$out" | python3 -c '
import json, sys
# 只看最后一行合法 JSON：脚本前面可能打了进度或 warning，那些不是结果。
last = None
for line in sys.stdin.read().splitlines():
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        last = json.loads(line)
    except Exception:
        continue
if last is None:
    # 没有 JSON 输出不算失败：有些采集脚本本来就只打人类可读文本，退出码已经判过了。
    raise SystemExit(0)
if last.get("ok") is False:
    print("run_json_task: JSON 返回 ok:false — %s" % (last.get("error") or last.get("hint") or ""), file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(0)
'
