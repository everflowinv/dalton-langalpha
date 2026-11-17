#!/usr/bin/env bash
# 情绪看板每日速览：重建看板 → 渲染速览卡片 → 发飞书。
#
# 这一条替代了原来六组各自发飞书的监控 cron（AH 溢价、亚洲四地杠杆、量子等权、美股顶部、
# 美股底部、六大科技相对信号）。那些 cron 的采集照跑，只是不再各发一张卡。
#
# 卡片里的每个数字都来自 build 落下的 signals.json，不另取行情：卡片和线上页面因此同源，
# 对不上就是 build 的问题，不会是文案漂移。任何一步失败就非零退出，绝不发半份卡片。
set -euo pipefail

WS=/Users/everflow/.openclaw/workspace
CHAT_ID="${SENTIMENT_DIGEST_CHAT_ID:-oc_a6b59323f034cb246c80e1b3684e71d1}"
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
