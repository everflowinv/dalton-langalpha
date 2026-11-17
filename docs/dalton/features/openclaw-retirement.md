# OpenClaw 退役现状与核对清单

本文基于当前代码仓库与 `scripts/dalton/` 的真实实现，盘点原 OpenClaw 体系哪些能力已经迁入 Dalton、哪些底层目录或桥接仍被只读复用或处于兼容保留状态，以及彻底下线宿主机残留 OpenClaw 进程的操作核对步骤；清理宿主机旧服务或判断是否允许删除某段 `~/.openclaw` 资源时应阅读本文。

## 1. 已彻底迁出并由 Dalton 接管的核心能力

| 原 OpenClaw 组件 / 能力 | Dalton 现行替代实现 | 代码位置 |
|---|---|---|
| **OpenClaw 模型代理聊天（`ocb-*`）** | 全部下线；改由 LangAlpha / Dalton 原生 OAuth 与直连模型接口接管，仅保留 `GET /api/v1/dalton/openclaw/health`（需 `CurrentUserId` 鉴权）路径上报构建指纹 | `src/dalton/services/openclaw_admin.py`、`src/dalton/app/router.py`、`agent_config.dalton.yaml` |
| **Web Search（原经 OpenClaw 网关）** | 改由 `gemini_grounding` 直连 Gemini Google Search Grounding（读取 `.env` 中的 `GEMINI_API_KEY`） | `agent_config.dalton.yaml`（`search_api: "gemini_grounding"`）、`src/dalton/web_search/gemini_grounding.py` |
| **X / Twitter 检索（原走 broker `/x-search`）** | 改由直连 xAI Responses 接口完成 | `src/dalton/web_search/x_search_direct.py` |
| **周期分析报告类定时任务** | 迁为 Dalton 技能（`plugins/dalton/skills/dalton-*`）+ 公司自动化 + `automation_report` 工具 + `dalton-channel-gateway` 飞书/Discord 卡片及纯文本投递。已迁代表：周播客 `dalton-podcast-weekly`、AI 应用周报 `dalton-ai-app-weekly-monitor`、腾讯会议纪要 `dalton-tmeet-weekly-minutes`、Discord `#wiki` 入库 `dalton-wiki-auto-ingest`、Richard 财报扫描与覆盖周报、Mark 新能源/中概周报、13F/Put Skew/Short Screen/BDC 技能等 | `plugins/dalton/skills/`、`src/dalton/database/automation_reports.py`、`src/dalton/services/company_automations.py` |
| **确定性抓取与静态看板生成** | 迁入 Dalton 数据任务层，由统一调度器按批启停管理 | `src/dalton/data_jobs/manifest.yaml` |
| **PM 驾驶舱、A 股盘中、港股动量 3 个本机看板** | 迁入 Dalton 监控面板平台 + 宿主机 `FeedDaemon` | `scripts/dalton/monitor_feeds/`、`src/dalton/monitors/` |
| **外部群消息通知与交互通道** | 独立网关 `dalton-channel-gateway` 接管飞书、Discord、微信 iLink，不再依赖 OpenClaw 通道运行时 | `integrations/dalton-channel-gateway/` |

## 2. 当前仍在代码或宿主机中保留的关联项（不可贸然删除）

以下路径在当前代码中仍有活跃读写或只读依赖，清理旧环境时**必须保留或先迁移依赖**：

1. **`var/dalton/openclaw-data/` 与 `scripts/dalton/openclaw_overlays/`（及 `seed_openclaw_data.sh` 对 `~/.openclaw/workspace` 的依赖）**：
   - 虽然不再运行 OpenClaw 守护进程，但市场情绪看板（`sentiment-dashboard`）、多个 `data_jobs` 脚本工作目录以及 `podcast_weekly_store.py`（`resolve_host_openclaw_data_root`）均将该目录作为文件存放根目录。
   - 注意 `scripts/dalton/seed_openclaw_data.sh` 默认要求 `~/.openclaw/workspace` 存在（否则直接退出码 1），且每次 `--apply` 不仅拷贝 `openclaw_overlays/`，还会从 `~/.openclaw/workspace` 同步白名单 `SEED_SCRIPTS`（25 个脚本）、`SEED_CONFIGS`（4 个配置）、`SEED_SKILLS`（13 个技能）并补齐缺失的 `SEED_STATE_PATHS` 状态文件。因此**仅保留 `build_coverage.py` 的 4 个源文件不足以维持 `seed_openclaw_data.sh` 默认可用**。
2. **`scripts/dalton/build_coverage.py`（只读依赖 `~/.openclaw/workspace`）**：
   - 若执行 `uv run python scripts/dalton/build_coverage.py` 重新生成 `config/dalton/coverage.yaml`，脚本依然从 `~/.openclaw/workspace/skills/coverage-update-richard/...`、等待列表与 `richard_earnings_tracker_cli.py` 读取源文件并比对 SHA-256。仅在 `config/dalton/coverage.yaml` 脱离这些外部脚本维护或已备份归档后才可清除对应源文件。
3. **`scripts/dalton/conversation_archive.py`（历史会话导出与卡片归档）**：
   - `scripts/dalton/conversations/export_openclaw.py` 与每日归档作业仍在运行期读取增量片段提取认知卡片入库，停掉历史 OpenClaw 会话前应确保历史会话已导出完毕并保存在 `var/dalton/conversations/`。
4. **`scripts/dalton/researchdata_bridge.py` 外部命令行路由**：
   - `researchdata_bridge.py` 仍在宿主机向沙箱暴露 `xueqiu`、`employee`、`last30days` 与 `x-timeline` 等桥接工具调用及 AlphaEngine 检索缓存，必须保持 `com.dalton.researchdata-bridge` 在线。
5. **停用组件的兼容源码保留**：
   - `integrations/openclaw/dalton-langalpha-broker/` 插件代码已明确停用，但因其单元测试（`node --test integrations/openclaw/dalton-langalpha-broker/test/*.test.mjs`）仍包含在 `scripts/dalton/pr_baseline.sh` 四项门禁中，目录暂时保留。
   - `scripts/dalton/web_search_bridge.py`（`com.dalton.web-search-bridge`）仅在手工切回旧模式时才会启用；默认 `gemini_grounding` 下无需依赖。

## 3. 安全停用残留 OpenClaw 进程的操作核对单

在确认宿主机完全关停 OpenClaw 之前，运维人员应按顺序核验：

1. **核对定时任务交接**：检查新迁入的 [automations.md](./automations.md) 与 [data-jobs.md](./data-jobs.md) 目标任务均已在 Dalton 启用并至少完成一轮真实运行且飞书/Discord 投递成功，避免双发或漏发。
2. **防止上游接口并发抢占（关键）**：停用原 OpenClaw 本机的东方财富/A 股盘中抓包与情绪看板定时命令**之后**，再正式放量运行 `com.dalton.monitor-feeds`，以免双跑把同一宿主机出口 IP 打进东方财富 502 黑名单（详见 [monitor-feeds.md](./monitor-feeds.md)）。
3. **检查历史目录迁移**：确认 `build_coverage.py` 依赖的 4 个源文件已确定只读留存或移入版本库独立数据源；若仍需运行 `scripts/dalton/seed_openclaw_data.sh`，需保留 `~/.openclaw/workspace` 中的全部白名单脚本/配置/技能及状态文件（或改用 `--source` 指向备份副本后再停用原目录），并确认 `var/dalton/openclaw-data` 已在每周 `dalton_data_backup.sh` 备份覆盖下。
4. **卸载旧 LaunchAgent / 守护进程**：确认不再运行旧的 OpenClaw gateway 后将其 launchd 任务 `bootout` 摘除；**切勿**误摘除 `com.dalton.researchdata-bridge`、`com.dalton.channel-gateway` 与 `com.dalton.monitor-feeds`。
