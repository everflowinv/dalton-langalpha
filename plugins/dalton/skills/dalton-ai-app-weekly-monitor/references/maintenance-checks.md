# 维护与回归验收

仅在修改脚本、门槛、数据库结构或应用技能更新时读取。本文件列的是维护检查，不要求每周重复跑整套回归。命令相对 `--workspace-dir` 给出的工作区；技能内 `references/`、`scripts/` 和 `templates/` 路径以技能根目录为基准，不从当前参考目录再拼接。

## 自检与验收

修改脚本、门槛、数据库结构或应用技能更新后，必须通过以下检查：

- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> --help`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> self-test`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> plan`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> universe`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> universe --scan-tier 1`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> universe --purity native`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> retier`（试算，不得写文件）
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> screen`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> screen --primary-only`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> ai-monetization --by yoy`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> ai-monetization --by qoq`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> rank --by yoy`（旧名仍可用）
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> headline-monetization`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> screen-embedded`（旧名仍可用）
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> coverage`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> usage`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> migrate-db`（试算，不得写文件）
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> profiles`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> coverage-ledger`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> backfill-plan`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> backfill-plan --layer expert`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> contradictions --all`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> snapshot`（不带 --write 不得写文件）
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> diff`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> export --tickers NET`
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> board --out state/ai-app-weekly/temp/ai-app-weekly/board-selftest.html`（渲染后文件须存在且 > 200 KB）
- `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace> validate --file .agents/skills/dalton-ai-app-weekly-monitor/templates/weekly-report.md`

改动排序逻辑后另外必须重跑：`ai-monetization --by yoy` 的主榜条目集合不得因为存储层改动而变化，`headline-monetization` 的分档计数不得变化。这两条是迁移正确性的判据。

原话与专家两层还必须验这四条拒收：无 `--anchor` 的立场话（AI is a huge opportunity for us）必须被拒；verbatim 超 20 词必须被拒；`--alignment contradicts` 缺 `--management-ref` 必须被拒；`reconcile` 对没有新增也没有 `mark-gap` 的公司必须报「未交代」。

`state/ai-app-weekly/runs/<日期>/doc.failed.json` 损坏时，人工删除该文件即可重置计次。

## 回归用例：Cloudflare

改动筛选逻辑后必须重跑这一条。Cloudflare 不单列任何 AI 收入——最近三份 10-Q 里 "Workers AI" 零命中，"artificial intelligence" 全部出现在风险因素和业务描述里——所以它在通道一永远是 0 条记录。但它是 AI 应用最典型的受益方：总收入同比从 2025Q1 的 26.5% 一路升到 2026Q2 的 36.0%，是少数在加速而不是减速的软件公司，CEO 在业绩新闻稿里把这个加速直接归到 agent 驱动的机器间流量上。

**它必须出现在 `screen-embedded` 的加速档。如果没出现，是筛选逻辑坏了，不是 Cloudflare 不够格。**

同理，任何一家「明显被 AI 推动但不单列 AI 分项」的公司筛不出来，都应当先怀疑门槛设计，再怀疑公司。

## 拟定 cron

见 [拟定调度](cron-spec.yaml)。补库每周二 16:00 Asia/Hong_Kong，周报每周五 15:00 Asia/Hong_Kong。成功回复 `NO_REPLY`。财报密集期可加一条周二只刷新筛选榜的补充跑，需明确要求后才建。

