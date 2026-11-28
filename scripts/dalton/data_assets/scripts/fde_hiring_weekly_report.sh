#!/usr/bin/env bash
# FDE 招聘周报：出 markdown 报告落盘 + 打一段 Discord 摘要到 stdout。
# stderr 全部转进日志文件，cron announce 只投递 stdout，避免 bootstrap 噪声混进正文。
set -uo pipefail
cd "$(dirname "$0")/.."

DATE="$(date +%F)"
OUT="research-outputs/fde-hiring/${DATE}.md"
ERRLOG="temp/fde_hiring_weekly.err"
mkdir -p research-outputs/fde-hiring temp

if python3 scripts/fde_hiring_discord_digest.py \
     --out "$OUT" --lookback 7 --top 60 --digest-top 10 2>"$ERRLOG"; then
  exit 0
fi

echo "⚠️ FDE 周报生成失败（${DATE}），最后 15 行错误："
tail -15 "$ERRLOG"
exit 1
