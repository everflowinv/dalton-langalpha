#!/bin/bash
# bbg_remote_refresh.sh — 远程触发 Windows BBG 刷新，拉回导出结果并校验后落盘
# 用法: bash scripts/bbg_remote_refresh.sh [--pull-only]
#   --pull-only: 只拉取，不触发刷新（用于刷新已在 Windows 完成的场景）
#
# Windows 侧两个产物都可能坏，坏法不一样，所以两个都拉、都过同一道校验，谁先通过用谁：
#   1) output/bbg_data_latest.csv —— 等 BDH 落定 30 秒后导出的，正常情况下最可靠；
#      BBG 连接断掉时会退化成 8 行 #N/A Connection（2026-08-09）。
#   2) data.xlsx —— 同一次刷新保存的工作簿；保存前脚本会把 BDH 公式还原，Excel 随即重算，
#      缓存值常停在 #N/A Requesting Data...（2026-08-09 起一直如此，CSV 因此三天没更新）。
# 两个都不过就非零退出，本地 CSV 保持原样，绝不用坏数据覆盖。

set -euo pipefail

BBG_HOST="everflow@100.119.172.87"  # Windows BBG machine via Tailscale
BBG_CSV_REMOTE="D:/Eve/output/bbg_data_latest.csv"
BBG_XLSX_REMOTE="D:/Eve/data.xlsx"
WORKSPACE="${OPENCLAW_WORKSPACE:-/Users/everflow/.openclaw/workspace}"
INCOMING_CSV="$WORKSPACE/temp/bbg_data_remote.incoming.csv"
INCOMING_XLSX="$WORKSPACE/temp/bbg_data_remote.incoming.xlsx"
LOCAL_XLSX="$WORKSPACE/temp/bbg_data_remote.xlsx"
LOCAL_CSV="$WORKSPACE/temp/bbg_data_latest.csv"
CONVERTER="$WORKSPACE/scripts/bbg_export_to_csv.py"
PYTHON_BIN="${DALTON_PYTHON_BIN:-/opt/homebrew/bin/python3.14}"
LOG="$WORKSPACE/temp/bbg_remote_refresh.log"
SSH_OPTS="-o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=3"

TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")

log() {
    echo "[$TIMESTAMP] $*" | tee -a "$LOG"
}

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
    if ! ssh $SSH_OPTS "$BBG_HOST" "cmd /c D:\\Eve\\run.bat" 2>&1 | tee -a "$LOG"; then
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
