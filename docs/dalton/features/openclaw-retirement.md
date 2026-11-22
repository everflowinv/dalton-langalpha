# OpenClaw 退役完成与现行运行时结构

OpenClaw 已于 2026-11-19 全部停用，宿主机 `~/.openclaw` 已归档移除；Dalton 作为独立完整系统运行，联网搜索、研究运行时、渠道网关、数据资产与会话能力全部由 Dalton 原生模块承担。

## 1. 现行原生替代实现总览

| 原 OpenClaw 能力 | Dalton 现行唯一实现 | 代码位置 |
|---|---|---|
| **模型调用与 OAuth 接口** | LangAlpha / Dalton 原生 OAuth 与直连模型提供商 | `agent_config.dalton.yaml`、`src/dalton/services/native_admin.py` |
| **Web Search** | `gemini_grounding` 直连 Gemini Google Search Grounding（配置为 `openclaw` 时启动直接报配置错误） | `src/dalton/web_search/gemini_grounding.py`、`src/dalton/web_search_spec.py` |
| **X / Twitter 检索** | `x_search_direct.py` 直连 xAI Responses 接口 | `src/dalton/web_search/x_search_direct.py` |
| **研究运行时与桥接** | `DALTON_RESEARCH_CODE`（代码与 `venv`）+ `DALTON_RESEARCH_HOME`（运行态数据与配置）双目录模式，`researchdata_bridge.py` 提供 `xueqiu`/`employee`/`last30days`/`x-timeline` 与 AlphaEngine 缓存 | `scripts/dalton/researchdata_bridge.py`、`scripts/dalton/research_runtime/` |
| **周期分析报告与投递** | Dalton 技能（`plugins/dalton/skills/dalton-*`）+ 公司自动化 + `automation_report` + `dalton-channel-gateway` 飞书/Discord 投递 | `plugins/dalton/skills/`、`src/dalton/services/company_automations.py` |
| **外部群消息交互与通道** | 独立网关 `dalton-channel-gateway` 承担飞书（含互动卡片）、Discord 与微信 iLink | `integrations/dalton-channel-gateway/` |
| **数据资产与静态看板** | 仓库内置 `scripts/dalton/data_assets/`，经 `seed_openclaw_data.sh` 安装进 `var/dalton/openclaw-data/`，由 `src/dalton/data_jobs/` 与 `FeedDaemon` 驱动 | `scripts/dalton/data_assets/`、`src/dalton/data_jobs/manifest.yaml`、`scripts/dalton/monitor_feeds/` |

## 2. 已移除的 OpenClaw 专用组件

下列组件与兼容路径已在正式版清理中全部移除：

1. **Web Search OpenClaw 路径**：`integrations/openclaw/dalton-langalpha-broker/`、`scripts/dalton/web_search_bridge.py`、`src/dalton/web_search/{provider.py,x_search.py,broker_protocol.py}`、`com.dalton.web-search-bridge` 及 `DALTON-HOOK(web-search-openclaw)`。
2. **Gateway 消息兜底**：`researchdata_bridge.py` 中 `_invoke_openclaw_gateway` 等 gateway 通知路径已移除（飞书走卡片脚本，Discord 走 `dalton-channel-gateway`）。
3. **旧单目录运行模式**：`DALTON_OCW_ROOT` / `legacy_ocw_mode` 与 `host_services_switch.sh --rollback` 已移除，`DALTON_RESEARCH_CODE` + `DALTON_RESEARCH_HOME` 为唯一支持模式。
4. **一次性迁移工具与历史会话导出**：`import_openclaw_skill_state.py`、`migrate_legacy_knowledge.py`、`wiki_coverage_report.py`、`build_coverage.py`、`check_skill_refs.py`、`data_tasks_parity.py`、`scripts/dalton/conversations/export_openclaw.py`、`scripts/dalton/conversation_archive.py`、`reextract_conversations.py` 及 `com.dalton.conversation-archive` 已移除。
5. **`seed_openclaw_data.sh` 旧同步路径与 `openclaw_overlays/`**：`openclaw_overlays/` 中的活跃脚本已迁至 `scripts/dalton/data_assets/`，`seed_openclaw_data.sh` 仅从仓库 `data_assets/` 安装到 `var/dalton/openclaw-data/`。

## 3. 沿用历史命名的在用组件

以下名称沿用早期迁移阶段的历史命名，运行时完全基于 Dalton 自有进程与受管目录执行：

- `src/dalton/services/openclaw_admin.py` 与 `GET /api/v1/dalton/openclaw/health`（提供构建指纹与健康检查，用于部署巡检与 `pr_baseline.sh`）。
- `src/dalton/database/openclaw_sync.py` 与 `dalton_openclaw_sync_runs` 数据库表（原生模型巡检台账）。
- 数据任务的 `OPENCLAW_WORKSPACE` 环境变量、容器内固定挂载点 `/Users/everflow/.openclaw/workspace` 以及宿主机数据卷目录 `var/dalton/openclaw-data`。
- `plugins/dalton/skills/dalton-bullbear-tracker` 与 `scripts/dalton/ai_finance*` 中用于确保运行目录限定在受管工作区内的路径校验逻辑。
- 历史团队对话归档（`dalton_conversation_cards`，截至 2026-11-18 共 42,842 张卡片）：不再运行导出和每日提取，保留只读检索供 `conversation_search`（团队记忆）使用；`src/dalton/dalton.yaml` 中的 `conversations.enabled` 控制检索开关，不要为“停止提取”而关闭它。

## 4. 数据目录的备份与重建

`var/dalton/openclaw-data/` 里的数据任务脚本、技能和运行状态有两个来源：仓库 `scripts/dalton/data_assets/` 中的文件由 `seed_openclaw_data.sh --apply` 安装，其余脚本、技能环境和状态数据只存在于生产数据目录及其备份中（每周数据包与外接盘归档）。在新机器上恢复时，先从最近一次数据包还原 `var/dalton/openclaw-data/`，再运行 `seed_openclaw_data.sh --apply` 覆盖为仓库里的最新版本。
