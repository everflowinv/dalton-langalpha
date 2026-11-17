# OpenClaw 退役现状与核对清单

这页对照当前代码仓和 `scripts/dalton/` 的真实实现，盘点原 OpenClaw 已经迁进 Dalton 的能力、还在只读复用或兼容保留的目录与桥接，以及下线宿主机残留进程前的核对清单。

## 1. 已彻底迁出并由 Dalton 接管的核心能力

| 原 OpenClaw 组件 / 能力 | Dalton 现行替代实现 | 代码位置 |
|---|---|---|
| **OpenClaw 模型代理聊天（`ocb-*`）** | 全部下线，改由 LangAlpha / Dalton 原生 OAuth 和直连模型接口接管；只保留 `GET /api/v1/dalton/openclaw/health`（需 `CurrentUserId` 鉴权）上报构建指纹 | `src/dalton/services/openclaw_admin.py`、`src/dalton/app/router.py`、`agent_config.dalton.yaml` |
| **Web Search（原经 OpenClaw 网关）** | 改由 `gemini_grounding` 直连 Gemini Google Search Grounding（读 `.env` 里的 `GEMINI_API_KEY`） | `agent_config.dalton.yaml`（`search_api: "gemini_grounding"`）、`src/dalton/web_search/gemini_grounding.py` |
| **X / Twitter 检索（原走 broker `/x-search`）** | 改由直连 xAI Responses 接口完成 | `src/dalton/web_search/x_search_direct.py` |
| **周期分析报告类定时任务** | 迁成 Dalton 技能（`plugins/dalton/skills/dalton-*`）+ 公司自动化 + `automation_report` 工具 + `dalton-channel-gateway` 飞书/Discord 卡片和纯文本投递。典型代表：周播客 `dalton-podcast-weekly`、AI 应用周报 `dalton-ai-app-weekly-monitor`、腾讯会议纪要 `dalton-tmeet-weekly-minutes`、Discord `#wiki` 入库 `dalton-wiki-auto-ingest`、Richard 财报扫描与覆盖周报、Mark 新能源/中概周报、13F/Put Skew/Short Screen/BDC 技能等 | `plugins/dalton/skills/`、`src/dalton/database/automation_reports.py`、`src/dalton/services/company_automations.py` |
| **确定性抓取与静态看板生成** | 迁入 Dalton 数据任务层，由统一调度器按批启停管理 | `src/dalton/data_jobs/manifest.yaml` |
| **PM 驾驶舱、A 股盘中、港股动量 3 个本机看板** | 迁入 Dalton 监控面板平台 + 宿主机 `FeedDaemon` | `scripts/dalton/monitor_feeds/`、`src/dalton/monitors/` |
| **外部群消息通知与交互通道** | 独立网关 `dalton-channel-gateway` 接管飞书、Discord 和微信 iLink，脱离 OpenClaw 通道运行时 | `integrations/dalton-channel-gateway/` |

## 2. 当前仍在代码或宿主机中保留的关联项（不可贸然删除）

下面这五处路径在当前代码里还有活跃读写或只读依赖，清理旧环境时**必须保留或先迁走依赖**：

1. **`var/dalton/openclaw-data/` 与 `scripts/dalton/openclaw_overlays/`（及 `seed_openclaw_data.sh` 对 `~/.openclaw/workspace` 的依赖）**：
   - OpenClaw 守护进程已经停掉，但市场情绪看板（`sentiment-dashboard`）、多个 `data_jobs` 脚本工作目录和 `podcast_weekly_store.py`（`resolve_host_openclaw_data_root`）都还在用这个目录当文件根目录。
   - 注意 `scripts/dalton/seed_openclaw_data.sh` 默认要求 `~/.openclaw/workspace` 存在（找不到直接退出码 1）。每次 `--apply` 除了拷贝 `openclaw_overlays/`，还会从 `~/.openclaw/workspace` 同步白名单 `SEED_SCRIPTS`（25 个脚本）、`SEED_CONFIGS`（4 个配置）、`SEED_SKILLS`（13 个技能）并补齐缺失的 `SEED_STATE_PATHS` 状态文件。因此，光是留住 `build_coverage.py` 的 4 个源文件，还不够维持 `seed_openclaw_data.sh` 默认可用。
2. **`scripts/dalton/build_coverage.py`（只读依赖 `~/.openclaw/workspace`）**：
   - 运行 `uv run python scripts/dalton/build_coverage.py` 重建 `config/dalton/coverage.yaml` 时，脚本依然会去读 `~/.openclaw/workspace/skills/coverage-update-richard/...`、等待列表和 `richard_earnings_tracker_cli.py` 并比对 SHA-256。得等 `config/dalton/coverage.yaml` 脱离这些外部脚本维护或做好备份归档，才能删掉源文件。
3. **`scripts/dalton/conversation_archive.py`（历史会话导出与卡片归档）**：
   - `scripts/dalton/conversations/export_openclaw.py` 和每日归档作业还在运行期读增量片段、抽认知卡片入库。停掉历史 OpenClaw 会话前，要确认历史会话都导完并存进了 `var/dalton/conversations/`。
4. **`scripts/dalton/researchdata_bridge.py` 外部命令行路由**：
   - `researchdata_bridge.py` 还在宿主机向沙箱暴露 `xueqiu`、`employee`、`last30days`、`x-timeline` 等桥接工具调用和 AlphaEngine 检索缓存，必须让 `com.dalton.researchdata-bridge` 一直在线。
5. **停用组件的兼容源码保留**：
   - `integrations/openclaw/dalton-langalpha-broker/` 插件代码已经停用，但它的单元测试（`node --test integrations/openclaw/dalton-langalpha-broker/test/*.test.mjs`）还在 `scripts/dalton/pr_baseline.sh` 四项门禁里，所以目录暂时留着。
   - `scripts/dalton/web_search_bridge.py`（`com.dalton.web-search-bridge`）只在手动切回旧模式时才用；默认 `gemini_grounding` 模式下由后端直连联网检索。

## 3. 安全停用残留 OpenClaw 进程的操作核对单

确认宿主机彻底关停 OpenClaw 前，运维人员按顺序核完下面四项：

1. **核对定时任务交接**：查一遍新迁入的 [automations.md](./automations.md) 和 [data-jobs.md](./data-jobs.md) 目标任务，确认都已经在 Dalton 里启用，而且至少跑完一轮真实运行并成功投递到飞书或 Discord，避免双发或漏发。
2. **防止上游接口并发抢占（关键）**：一定要**先**停掉原 OpenClaw 本机的东方财富 / A 股盘中抓包和情绪看板定时命令，**再**正式放量运行 `com.dalton.monitor-feeds`，免得双跑把同一宿主机出口 IP 打进东方财富 502 黑名单（见 [monitor-feeds.md](./monitor-feeds.md)）。
3. **检查历史目录迁移**：确认 `build_coverage.py` 依赖的 4 个源文件已经只读留存或迁入独立数据源；如果还要跑 `scripts/dalton/seed_openclaw_data.sh`，得留好 `~/.openclaw/workspace` 里的白名单脚本、配置、技能和状态文件（或者用 `--source` 指向备份副本后再停原目录），并确认 `var/dalton/openclaw-data` 已经在每周 `dalton_data_backup.sh` 的备份清单里。
4. **卸载旧 LaunchAgent / 守护进程**：确认旧的 OpenClaw gateway 彻底停用后，用 `bootout` 摘掉它的 launchd 任务；**千万别**误摘了 `com.dalton.researchdata-bridge`、`com.dalton.channel-gateway` 和 `com.dalton.monitor-feeds`。
