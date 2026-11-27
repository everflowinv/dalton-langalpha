#!/usr/bin/env bash
# 情绪看板每日速览：重建看板 → 渲染速览卡片 → 发飞书。
#
# 这一条替代了原来六组各自发飞书的监控 cron（AH 溢价、亚洲四地杠杆、量子等权、美股顶部、
# 美股底部、六大科技相对信号）。那些 cron 的采集照跑，只是不再各发一张卡。
#
# 卡片里的每个数字都来自 build 落下的 signals.json，不另取行情：卡片和线上页面因此同源，
# 对不上就是 build 的问题，不会是文案漂移。任何一步失败就非零退出，绝不发半份卡片。
#
# 配置项（优先读环境变量，其次读 $DALTON_DATA_TASKS_CONFIG 或 <runtime_workspace>/config/data_tasks.env）：
#   DALTON_DATA_TASK_FEISHU_SENTIMENT_CHAT_ID  飞书接收群 chat_id（兼容 DALTON_SENTIMENT_DIGEST_CHAT_ID / SENTIMENT_DIGEST_CHAT_ID；必填）
#   DALTON_DATA_TASK_WORKSPACE                 数据任务工作目录（兼容 DALTON_WORKSPACE / OPENCLAW_WORKSPACE / WORKDIR；默认当前运行目录 $PWD）
set -euo pipefail

RUNTIME_PWD="${PWD}"
BOOT_CFG="${DALTON_DATA_TASKS_CONFIG:-$RUNTIME_PWD/config/data_tasks.env}"

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

WS="${DALTON_DATA_TASK_WORKSPACE:-${DALTON_WORKSPACE:-${OPENCLAW_WORKSPACE:-${WORKDIR:-$(_read_first_cfg_key "$BOOT_CFG" "DALTON_DATA_TASK_WORKSPACE" "DALTON_WORKSPACE")}}}}"
WS="${WS:-$RUNTIME_PWD}"

CFG_FILE="${DALTON_DATA_TASKS_CONFIG:-$WS/config/data_tasks.env}"
CHAT_ID="${DALTON_DATA_TASK_FEISHU_SENTIMENT_CHAT_ID:-${DALTON_SENTIMENT_DIGEST_CHAT_ID:-${SENTIMENT_DIGEST_CHAT_ID:-$(_read_first_cfg_key "$CFG_FILE" "DALTON_DATA_TASK_FEISHU_SENTIMENT_CHAT_ID" "DALTON_SENTIMENT_DIGEST_CHAT_ID" "SENTIMENT_DIGEST_CHAT_ID")}}}"
if [[ -z "$CHAT_ID" ]]; then
  echo "ERROR: 未配置 DALTON_DATA_TASK_FEISHU_SENTIMENT_CHAT_ID（兼容 DALTON_SENTIMENT_DIGEST_CHAT_ID；请设置环境变量或在 config/data_tasks.env 中配置）" >&2
  exit 1
fi
OUT="$WS/temp/sentiment-dashboard"
cd "$WS"
mkdir -p "$OUT"

echo "[$(date '+%F %T')] self-test"
bash skills/sentiment-dashboard/scripts/run.sh self-test --json > "$OUT/selftest.json"
python3 -c "import json,sys; d=json.load(open('$OUT/selftest.json')); sys.exit(0 if d.get('ok') else 1)" \
  || { echo "self-test 未通过，看 $OUT/selftest.json" >&2; exit 1; }

echo "[$(date '+%F %T')] build --no-cache"
bash skills/sentiment-dashboard/scripts/run.sh build --no-cache --json > "$OUT/build.json"
python3 - "$OUT/build.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
if not d.get("ok"):
    print("build 失败: " + str(d.get("error")), file=sys.stderr)
    raise SystemExit(1)
# warnings 不阻断投递：缺哪块数据会由 signals.json 带到卡片末尾的 ⚠️ 行，让读者看见，而不是整条不发。
for w in d.get("warnings", []):
    print("WARN " + w, file=sys.stderr)
PY

echo "[$(date '+%F %T')] digest"
bash skills/sentiment-dashboard/scripts/run.sh digest --json > "$OUT/digest.json"
TITLE="$(python3 -c "
import json,sys
d=json.load(open('$OUT/digest.json'))
if not d.get('ok'):
    print('digest 失败: %s | %s' % (d.get('error'), d.get('hint')), file=sys.stderr); raise SystemExit(1)
print(d['title'])
")"
# 2026-09-07 起 digest 同时落 digest.card.json（Card Schema 2.0：顶部/底部各成一节、信号着色、无信号读数缩小字）。
# 有它就发这份卡片；skill 还是旧版没落 card.json 时，退回 markdown 转卡片的老路，不让 cron 失败。
CARD_PATH="$(python3 -c "
import json,os
d=json.load(open('$OUT/digest.json'))
p=d.get('card_path') or ''
print(p if p and os.path.exists(p) else '')
")"

echo "[$(date '+%F %T')] send feishu → $CHAT_ID"
if [[ -n "$CARD_PATH" ]]; then
  python3 scripts/feishu_send_card.py --chat-id "$CHAT_ID" --card-json-file "$CARD_PATH"
else
  echo "digest 未落 card.json，退回 markdown 卡片" >&2
  python3 scripts/feishu_send_card.py --chat-id "$CHAT_ID" --title "$TITLE" --file "$OUT/digest.md"
fi
echo "[$(date '+%F %T')] done"
