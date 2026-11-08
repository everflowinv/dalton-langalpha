# 第 1 阶段 19 个任务基线（lead 用脚本从 `openclaw/phase1_cron.json` 生成，逐字段为准，不得改写）

编号取自 `OpenClaw迁移对照表_20261105.md` 中与任务名同一行的编号。`—` 表示 JSON 没有该字段。

| 编号 | 任务名 | cron | 时区 | 总超时 s | 无输出超时 s | cwd | 命令 |
|---|---|---|---|---|---|---|---|
| T1 | `bbg-data-pull-asia` | `0 10,17 * * 1-5` | Asia/Hong_Kong | 900 | 420 | `/Users/everflow/.openclaw/workspace` | `bash /Users/everflow/.openclaw/workspace/scripts/bbg_remote_refresh.sh` |
| T5 | `cn-leverage-daily` | `45 17 * * 1-5` | Asia/Shanghai | 900 | — | `/Users/everflow/.openclaw/workspace` | `bash /Users/everflow/.openclaw/workspace/skills/cn-leverage-monitor/run.sh update && bash /Users/everflow/.openclaw/workspace/skills/cn-leverage-monitor/run.sh dashboard` |
| T7 | `jp-leverage-daily` | `35 19 * * 1-5` | Asia/Tokyo | 900 | 300 | `/Users/everflow/.openclaw/workspace` | `bash /Users/everflow/.openclaw/workspace/skills/jp-leverage-monitor/run.sh run` |
| T8 | `kr-leverage-daily` | `40 5 * * 1-5` | America/New_York | — | — | `/Users/everflow/.openclaw/workspace` | `bash /Users/everflow/.openclaw/workspace/skills/kr-leverage-monitor/run.sh run` |
| T10 | `index-crowding-html-daily` | `30 19 * * 1-5` | Asia/Hong_Kong | 900 | 900 | `/Users/everflow/.openclaw/workspace` | `set -euo pipefail
cd /Users/everflow/.openclaw/workspace
mkdir -p temp
bash skills/index-crowding-research/scripts/run.sh --json self-test > temp/index-crowding-cron-selftest.json
python3 -m json.tool temp/index-crowding-cron-selftest.json >/dev/null
bash skills/index-crowding-research/scripts/run.sh --json mirror > temp/index-crowding-cron-mirror-metadata.json
python3 -m json.tool temp/index-crowding-cron-mirror-metadata.json >/dev/null
bash skills/index-crowding-research/scripts/run.sh --json fetch-history --workers 8 --refresh > temp/index-crowding-cron-history.json
python3 -m json.tool temp/index-crowding-cron-history.json >/dev/null
bash skills/index-crowding-research/scripts/run.sh --json mirror > temp/index-crowding-cron-mirror-chart.json
python3 -m json.tool temp/index-crowding-cron-mirror-chart.json >/dev/null
bash skills/index-crowding-research/scripts/run.sh --json optimize-recent > temp/index-crowding-cron-optimize.json
python3 -m json.tool temp/index-crowding-cron-optimize.json >/dev/null
trade_date=$(python3 -c 'import json; print(json.load(open("research-outputs/index-crowding/official-all.json"))["trade_date"])')
test -n "$trade_date"
echo "index-crowding refreshed: $trade_date"` |
| T13 | `sentiment-dashboard-digest` | `30 20 * * 1-5` | Asia/Hong_Kong | 1200 | 420 | `/Users/everflow/.openclaw/workspace` | `bash scripts/sentiment_dashboard_digest.sh` |
| T18 | `megacap-relative-signal-monitor` | `30 8 * * 1-5` | America/New_York | 600 | — | `/Users/everflow/.openclaw/workspace` | `bash scripts/run_json_task.sh python3 scripts/megacap_relative_signal_monitor.py --json` |
| T21 | `us-bottom-monitor` | `5 22 * * 1-5` | Asia/Hong_Kong | 600 | — | `/Users/everflow/.openclaw/workspace` | `bash scripts/run_json_task.sh bash skills/us-bottom-monitor/run.sh --json run` |
| T22 | `us-top-monitor` | `10 22 * * 1-5` | Asia/Hong_Kong | 600 | — | `/Users/everflow/.openclaw/workspace` | `bash scripts/run_json_task.sh bash skills/us-top-monitor/run.sh --json run` |
| T25 | `sentiment-dashboard-refresh` | `20 10,22 * * 1-5` | Asia/Hong_Kong | 900 | 300 | `/Users/everflow/.openclaw/workspace` | `bash skills/sentiment-dashboard/scripts/run.sh self-test --json && bash skills/sentiment-dashboard/scripts/run.sh build --no-cache --json && [REDACTED] scripts/sentiment_dashboard_cos_upload.py` |
| T26 | `us-topbottom-dm-v23` | `25 22 * * 1-5` | Asia/Hong_Kong | 600 | 600 | `/Users/everflow/.openclaw/workspace` | `python3 scripts/cron_command_tasks.py us-topbottom-dm-v23` |
| T27 | `asia-leverage-crossview-daily` | `30 22 * * 1-5` | Asia/Hong_Kong | 900 | — | `/Users/everflow/.openclaw/workspace` | `bash scripts/run_json_task.sh python3 scripts/asia_leverage_crossview.py --charts` |
| T28 | `tw-leverage-daily` | `40 9 * * 1-5` | America/New_York | 3600 | — | `/Users/everflow/.openclaw/workspace` | `bash /Users/everflow/.openclaw/workspace/skills/tw-leverage-monitor/run.sh update --quiet --json` |
| T29 | `bbg-data-pull-us` | `0 10 * * 1-5` | America/New_York | 900 | 420 | `/Users/everflow/.openclaw/workspace` | `bash /Users/everflow/.openclaw/workspace/scripts/bbg_remote_refresh.sh` |
| T32 | `quantum-ew-monitor` | `27 10 * * 1-5` | America/New_York | 600 | — | `/Users/everflow/.openclaw/workspace` | `bash scripts/run_json_task.sh bash scripts/quantum_ew_monitor.sh` |
| T40 | `ah-premium-am` | `5 10 * * 1-5` | Asia/Hong_Kong | 600 | — | `/Users/everflow/.openclaw/workspace` | `bash scripts/run_json_task.sh bash skills/ah-premium-monitor/run.sh --json monitor` |
| T44 | `ah-premium-pm` | `40 21 * * 1-5` | —（未填） | 600 | — | `/Users/everflow/.openclaw/workspace` | `bash scripts/run_json_task.sh bash skills/ah-premium-monitor/run.sh --json monitor` |
| T52 | `softbank-nav-monitor` | `0 16 * * 1-5` | Asia/Hong_Kong | 600 | 600 | `/Users/everflow/.openclaw/workspace` | `python3 scripts/cron_command_tasks.py softbank-nav-monitor` |
| T54 | `tsmc-adr-premium` | `15 16 * * 1-5` | Asia/Hong_Kong | 120 | 120 | `/Users/everflow/.openclaw/workspace` | `python3 scripts/cron_command_tasks.py tsmc-adr-premium` |


注：T26/T52/T54 的 argv 是 `python3 scripts/cron_command_tasks.py <任务名>`，三者都有 `outputMaxBytes: 50000`。T25 命令里的 `[REDACTED]` 是 OpenClaw 导出时自己打的码，原样保留。
