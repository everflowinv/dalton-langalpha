# Dalton 系统架构

本文说明 Dalton 在上游 LangAlpha 之上的叠加层架构、所有 `src/dalton/*` 子模块职责、前后端请求与数据流、Dalton 自有数据库表及 `d001`–`d039` 迁移序列、宿主机常驻服务与外部依赖；理解系统边界、定位模块入口或评估上游同步影响时应先阅读本文。结论上，Dalton 通过配置覆盖（overlay）与 `DALTON-HOOK` 最小侵入挂载在上游后端与前端之上，核心研究编排复用 LangAlpha 的沙箱智能体引擎，而团队认证、预算控制、知识治理、监控面板、自动化报告与外部数据源全部收敛在独立目录及独立 Alembic 迁移链中。

## 1. 与上游 LangAlpha 的关系

Dalton 不分叉重写 LangAlpha 主干，而是以**配置叠加 + 目录隔离 + Hook 标记守卫**三层边界维持可持续同步上游的能力：

- **配置叠加（YAML Overlay）**：设置环境变量 `APP_ENV=dalton` 后，配置读取器（`src/ptc_agent/config/file_utils.py:load_yaml_config`）自动把 `agent_config.dalton.yaml`、`config.dalton.yaml` 叠加在根目录基准配置 `agent_config.yaml`、`config.yaml` 之上；注意 YAML 映射按层级合并，而列表整体替换（例如 `config.dalton.yaml` 中的 `market_data.providers` 完整替换上游默认链）。编排叠加使用 `docker-compose.yml` 配合 `docker-compose.dalton.yml`。Dalton 专属行为默认值集中在 `src/dalton/dalton.yaml`，经 `src/dalton/settings.py:get_dalton_config` 读取。
- **目录所有权（`dalton_paths.txt`）**：`scripts/dalton/dalton_paths.txt` 登记全部归 Dalton 独占的文件与目录（例如 `src/dalton/`、`plugins/dalton/`、`plugins/dalton_data/`、`scripts/dalton/`、`config/dalton/`、`deploy/dalton/`、`docs/dalton/`、`web/src/pages/Settings/panels/dalton/`、`web/src/components/dalton/` 等），这些路径内无需标注 `DALTON-HOOK`。
- **上游文件挂载（`DALTON-HOOK`）**：凡在非独占上游文件中改动逻辑，均需在改动行上下 ±25 行内标注 `# DALTON-HOOK(<id>)`（前端使用 JS/TS 注释形式），并且 `<id>` 必须在 `scripts/dalton/check_hooks.sh:ALLOWED_HOOKS` 白名单内。`scripts/dalton/check_hooks.sh --strict --audit` 以 `git merge-base HEAD upstream/main` 为基准校验全部改动与必需关键 hook（`P0_REQUIRED_HOOKS`），并阻止修改上游受保护模型清单与基础配置。

## 2. 模块地图（`src/dalton/*`）

`src/dalton/` 下按职能划分为 21 个子包与若干顶层辅助模块，共同构成 Dalton 业务层：

### 2.1 应用入口、权限与配置基础

- `src/dalton/app/`：后端装配入口。`dalton_router`（`src/dalton/app/__init__.py`）组合知识、发布、覆盖、预算、监控、自动化、渠道与健康路由，由 `src/server/app/setup.py` 导入并在上游 `oauth_router` 之前挂载至 FastAPI 主应用；`internal_llm_router` 等内部路由供宿主机桥接或自动化流程安全调用。
- `src/dalton/settings.py` & `src/dalton/dalton.yaml`：Dalton 配置与开关单一权威源（`get_dalton_config`、`get_dalton_settings`），涵盖时区（`Asia/Shanghai`）、`credentials.sharing: team`、功能总开关（`artifact_publishing`、`computer_cli_tokens`、`problem_reports`、`coverage_units`、`data_jobs`、`colleague_dispatch`）、代理网络 fake-IP 放行与 DoH 真实地址解析等。
- `src/dalton/access_tiers.py` & `src/dalton/auth.py` & `src/dalton/invites.py` & `src/dalton/sharing.py`：团队三层权限模型（`admin` / `internal` / `external`）实现，对接 Supabase JWT 认证（`DALTON_AUTH_MODE=supabase`），管理 `dalton_invites` 邀请注册、工作区分享（`dalton_workspace_sharing`）与用户共享。
- `src/dalton/credentials.py`：团队凭据共享逻辑，使拥有者授权的 OAuth / API Key 可安全服务团队成员而不暴露明文。

### 2.2 研究资产、知识治理与覆盖

- `src/dalton/coverage/`：解析标准覆盖名单 `config/dalton/coverage.yaml`（Core 27 + Watch 78，共 105 家）与行业覆盖单元（`dalton_coverage_units`、`dalton_sector_members`），提供标的/工作区身份解析（`identity.py`）。
- `src/dalton/knowledge/`：结构化公司与行业研究知识层，维护带版本号（`dalton_knowledge_versions`）与跨模型审阅意见（`dalton_verdicts`）的研究记忆及只读回溯。
- `src/dalton/publish/`：正式研究发布治理门，校验来源追溯完备性与独立模型家族审阅后生成不可变发布档案（`dalton_publish_requests`、`dalton_publications`）。
- `src/dalton/provenance/`：声明来源解析（`declared_sources.py`）与 SEC/研报定位追溯，将 MCP 工具返回的 `_declared_sources` 元数据转为结构化来源卡片。

### 2.3 智能体工具、中间件与外部数据

- `src/dalton/tools/`：注册给智能体的 Dalton LangChain 工具，包括知识读写、发布提交、自主任务、公司/部门协同请求、周播客读写（`podcast_weekly_tools.py`）及自动化报告提交工具（`automation_report_tools.py` 中的 `automation_report`）。
- `src/dalton/middleware/`：在 LangGraph 智能体创建前注入 Dalton 系统指令、工作区方法论技能索引与声明来源钩子。
- `src/dalton/llm/` & `src/dalton/budget/`：管理模型偏好校验（`validate_dalton_model_preferences`）、通知路由校验与本地美元预算控制（`LocalBudgetPort`，按租约分片预留并发额度并在结束后对账结算）。
- `src/dalton/extended_hours.py`、`src/dalton/market_data_fallback.py`、`src/dalton/market_data/` & `src/dalton/data_map/` & `src/dalton/web_search/`：顶层模块 `extended_hours.py` 与 `market_data_fallback.py` 分别提供**美股**盘前盘后扩展时段行情补全（`XNYS` 日历，显式排除非美股与指数标的）及 `yfinance` 优先 / FMP 兜底回退；`src/dalton/market_data/`（`calendar_fallback.py`）提供日历回退；`data_map/health.py` 负责数据源健康探测与路由规则校准；`web_search/` 提供 Gemini Grounding（`gemini_grounding.py`）与直连 xAI X 检索（`x_search_direct.py`）联网能力。
- `src/dalton/crawl_service/`：独立的网页抓取微服务入口（`src/dalton/crawl_service/app.py`），运行于隔离 Docker 网络 `crawl`，通过 `INTERNAL_SERVICE_TOKEN` 鉴权隔离沙箱直接触网风险。

### 2.4 自主运行、数据任务、监控面板与推送

- `src/dalton/autonomy/`：包含系统任务聚合视图（`system_jobs.py`）、定时与股价异动唤醒（`schedule.py`、`price_trigger.py`）、前瞻预检日志（`dalton_preflight_log`）与报告自动恢复投递（`report.py`）。
- `src/dalton/data_jobs/`：确定性定时数据任务层，解析 `src/dalton/data_jobs/manifest.yaml`，按容器内（`container_runner.py`）或宿主机桥接（`host_runner.py`）执行脚本任务，统筹调度循环与内存水位检查（`scheduler.py`）。
- `src/dalton/monitors/`：团队监控面板平台（[monitors.md](./features/monitors.md)），包含权限四档控制（`permissions.py`）、数据任务注册表（`MONITOR_DATA_JOB_REGISTRY`）、静态文件边界 Containment 与实时数据写入、HMAC 短时访问授权（`grants.py`）及版本历史。
- `src/dalton/notify/`：通知与外部渠道投递核心（`dispatch.py`），将自动化报告、失败告警、飞书文档权限授予与系统告警路由至本地渠道网关。
- `src/dalton/services/` & `src/dalton/workflows/` & `src/dalton/conversations/` & `src/dalton/database/`：`services/` 承担自动化配置生命周期与乐观锁（`company_automations.py`）、原生模型与部署健康态查询（`openclaw_admin.py`、`native_admin.py`）；`conversations/` 负责对话归档抽取卡片；`database/` 统一管理 Dalton 的原始 `psycopg3` 异步 SQL 查询与事务锁。

## 3. 请求与数据流

```
浏览器 / Desktop (React 19)
   │ HTTPS (Tailscale Funnel :8443 → Caddy edge 127.0.0.1:8088)
   ▼
FastAPI backend (127.0.0.1:8000, APP_ENV=dalton)
   ├── 业务与鉴权：Supabase JWT / access_tiers / LocalBudgetPort / company_automations / monitors
   ├── 持久化：Postgres (dalton-postgres:18-pgvector) + Redis + MinIO (storage profile)
   ├── 网页抓取：→ crawler 容器 (:crawl 网络, X-Service-Token 鉴权)
   ├── 消息投递：→ dalton-channel-gateway (127.0.0.1:8962) → 飞书 / Discord / 微信
   └── 智能体引擎 (PTCAgent / Docker Sandbox)
          ├── stdio MCP：plugins/dalton_data/dalton_sec_mcp_server.py (SEC EDGAR)
          └── HTTP MCP：→ host.docker.internal:8960 (researchdata_bridge.py)
                             ├── AlphaEngine SQLite WAL 缓存 + 单飞合并
                             └── 雪球 / 社区 / 外部研报 / 宿主机受控脚本执行
```

数据流关键约束：
1. **容器与宿主机隔离**：后端与沙箱不直接持有宿主机敏感凭据与外部商业数据库二进制，所有宿主机桥接均通过回环端口 + `INTERNAL_SERVICE_TOKEN`、`DALTON_BRIDGE_TOKEN`（MCP 路径）或 `DALTON_BRIDGE_ADMIN_TOKEN`（宿主机数据任务等管理路由，通过 `Authorization: Bearer` 或 `X-Dalton-Bridge-Token` 发送）通信，`researchdata_bridge.py` 启动子进程前会通过 `bridge_subprocess_env.py` 剥离无关密钥。
2. **监控面板实时推送**：宿主机 `FeedDaemon`（`scripts/dalton/monitor_feeds/run_feeds.py`）抓取行情并计算快照后，经 `PUT /api/v1/dalton/monitors/internal/{slug}/data/{key}`（携带 `X-Service-Token`）写入 `dalton_monitor_data`，前端查看面板时使用带过期时间的 `grant`（HMAC 签名）拉取静态 HTML 与最新 JSON，每次读取实时重查成员权限。

## 4. 数据库表与迁移编号

Dalton 在上游库之外维护独立的 Alembic 版本表 `alembic_version_dalton`（配置文件 `src/dalton/alembic.ini`），迁移脚本位于 `src/dalton/migrations/versions/`。全部分支共有 **38 个**迁移脚本（编号 `d001`–`d039`，**无 `d034`**），且存在非线性串联：`d031 -> d035 -> d036 -> d032 -> d033 -> d037 -> d038 -> d039`。

| 迁移范围 | 核心表与数据结构 | 说明 |
|---|---|---|
| `d001`, `d007` | `dalton_budget_config`、`dalton_budget_task_caps`、`dalton_budget_reservations`、`dalton_llm_journal`、`dalton_provider_events` | 本地预算配置/预留账本、LLM 调用与模型事件流水 |
| `d002`, `d012` | `dalton_openclaw_sync_runs`（后增 `source` 列） | 同步/模型巡检运行台账（含 `native` 记录） |
| `d003`–`d005`, `d021`, `d024` | `dalton_knowledge_versions`、`dalton_verdicts`、`dalton_publish_requests`、`dalton_publications`、`dalton_publication_files`、`dalton_report_issues`、修改 `provenance_records` | 知识版本、跨模型审阅、正式发布、行业知识文档与问题上报 |
| `d006`, `d008`, `d013`, `d037` | `dalton_watch_state`、`dalton_signals`、`dalton_preflight_log`（含 `wake_context`）、`dalton_daily_plans`、`dalton_price_move_firings` | 自主研判状态、日计划、唤醒上下文与股价触发去重 |
| `d009`, `d010`, `d017`, `d022`, `d033` | `dalton_conversation_cards`、`dalton_conversation_processed`、`dalton_conversation_runs`、`dalton_wiki_ingests`、`dalton_transcription_jobs`、`dalton_chat_file_uploads` | 对话归档卡片、Wiki 入库、音频转写与聊天大文件登记 |
| `d011`, `d015`, `d023`, `d025`–`d027` | `dalton_data_source_health`、`dalton_data_map_cache`、`dalton_coverage_groups`、`dalton_coverage`、`dalton_earnings_calendar_mirror`、`dalton_coverage_units`、`dalton_sector_members`、`dalton_cross_unit_requests` | 数据地图健康态、覆盖名单、财报镜像与覆盖单元协同 |
| `d016`, `d018`, `d020`, `d028` | `dalton_members`、`dalton_invites`、`dalton_workspace_sharing`、`dalton_user_sharing`、`dalton_workspace_settings`、`dalton_user_settings` | 成员分级、邀请注册、工作区/用户共享与个人/工作区设置 |
| `d014`, `d019`, `d029`, `d035` | `dalton_notify_deliveries`、`dalton_feishu_read_scopes`、`dalton_data_task_defs`、`dalton_data_task_slot`、`dalton_data_task_runs`、`dalton_internal_feishu_groups`、`dalton_notify_storage_state`、`dalton_company_migration_batches` | 通知投递台账、飞书授权范围、确定性数据任务与公司自动化迁出批次 |
| `d030`, `d031`, `d036`, `d032` | `dalton_channel_identities`、`dalton_channel_bind_codes`、`dalton_channel_user_settings`、`dalton_channel_chat_bindings`、`dalton_channel_session_generations`、`dalton_channel_thread_watches`、`dalton_channel_inbound_events`、`dalton_channel_background_runs`、`dalton_channel_deliveries`、`dalton_channel_automation_runs`、`dalton_channel_weixin_state`、`dalton_channel_weixin_context_tokens` | 多渠道接入网关身份绑定、会话映射、防重入与微信上下文令牌 |
| `d038`, `d039` | `dalton_monitors`、`dalton_monitor_members`、`dalton_monitor_data`、`server_keys`（`monitor_grant`）、`dalton_monitor_publish_runs`、`dalton_monitor_versions` | 监控面板元数据、成员 ACL、实时数据键值、签名密钥、发布历史与不可变版本 |

## 5. 宿主机常驻进程（`launchd` 服务清单）

macOS 宿主机通过 `launchd` 运行脱离容器网络的本地桥接、采集、归档与备份任务（所有基于 `StartCalendarInterval` 的定时触发均按**宿主机本地日历时区**执行，模板与安装脚本不做时区转换；如需按北京时间触发，宿主机系统时区须为 `Asia/Shanghai`）：

| Launchd Label | 模板 / 配置位置 | 端口 / 触发方式 | 核心职责 |
|---|---|---|---|
| `com.dalton.researchdata-bridge` | `scripts/dalton/launchd/com.dalton.researchdata-bridge.plist.template` | `127.0.0.1:8960` | 宿主机 MCP 桥接服务、AlphaEngine 搜索缓存、`podcast_weekly`、宿主机 data_job 受控执行、system_jobs 宿主机探测 |
| `com.dalton.channel-gateway` | `scripts/dalton/launchd/com.dalton.channel-gateway.plist.template` | `127.0.0.1:8962` | 多渠道接入网关（飞书 WebSocket + 卡片/文档授权、Discord、微信 iLink 出入站）与运维告警推送 |
| `com.dalton.monitor-feeds` | `scripts/dalton/monitor_feeds/com.dalton.monitor-feeds.plist` | 交易时段 60s 轮询 / 非交易时段最长 300s 休眠 | A 股盘中监测（`cn-intraday`）与港股动量（`hk-momentum`）实时抓取并推送监控面板数据 |
| `com.dalton.conversation-archive` | `scripts/dalton/launchd/com.dalton.conversation-archive.plist.template` | 每日 21:15（宿主机本地时区） | 执行 `scripts/dalton/conversation_archive.py run` 抽取历史会话认知卡片 |
| `com.dalton.web-search-bridge` | `scripts/dalton/launchd/com.dalton.web-search-bridge.plist.template` | 本地桥接端口（可选兼容） | 为旧式 `search_api: openclaw` 路径提供主机检索代理 |
| `com.dalton.config-git-backup` | `scripts/dalton/ops/launchd/com.dalton.config-git-backup.plist.template` | 每 6 小时（宿主机本地时区 00:15/06:15/12:15/18:15） | 加密备份配置与轻量状态到 GitHub 私有仓库 |
| `com.dalton.db-offsite` | `scripts/dalton/ops/launchd/com.dalton.db-offsite.plist.template` | 每日 03:40（宿主机本地时区） | 流式加密上传最新 `dalton-pg-*.dump` 到 Google Drive |
| `com.dalton.data-backup` | `scripts/dalton/ops/launchd/com.dalton.data-backup.plist.template` | 每周日 05:00（宿主机本地时区） | 流式加密上传 MinIO、沙箱工作区、Redis 快照与 `var/dalton`（含 SQLite 一致副本） |
| `com.dalton.backend-watchdog` | 运行时引用自 `scripts/dalton/researchdata_bridge.py:_inspect_backend_watchdog_job_sync` | 定时巡检 | 宿主机后端与桥接服务健康状态巡检（仓库内未存放其 plist 模板文件） |

## 6. 外部依赖

- **数据库与缓存**：`dalton-postgres:18-pgvector`（pgvector 向量检索）、Redis（SSE 缓冲与并发协调）、MinIO（`storage` profile，Chainguard 镜像，仅绑定 `127.0.0.1:9000` 供 ASR 与聊天大文件使用，及 `127.0.0.1:9001` 管理控制台）。
- **认证与反向代理**：Supabase Auth（`DALTON_AUTH_MODE=supabase` 线上认证与邀请流）、Caddy `edge`（`caddy:2.9.1-alpine`，托管 `./web/dist` 与反代后端）与 Tailscale Funnel（宿主机 `:8443` 对外转发至 Caddy `127.0.0.1:8088`）。
- **金融数据源与 AI 模型**：`ginlix-data`、Tushare、yfinance、FMP、akshare、SEC EDGAR（`dalton_sec`）、腾讯/东方财富/港交所/深交所港股通公开接口、AlphaEngine（经宿主机桥接）、Google Gemini（`gemini_grounding` 与 `gemini-*` 模型）、xAI（`x_search_direct` 与 `grok-*`）、Anthropic/OpenAI/DeepSeek/Zhipu 等按团队 OAuth 或 API Key 接入。
- **团队协同与渠道**：飞书开放平台（卡片、群聊、飞书文档授权与系统告警）、Discord、微信 iLink，以及用于异地灾备的 GitHub 私有仓库与 Google Drive（`rclone`）。
