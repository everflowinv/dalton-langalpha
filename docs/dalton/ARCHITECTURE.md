# Dalton 系统架构

Dalton 叠在上游 LangAlpha 之上运行，由自有应用包 `src/dalton/*`、双链路 Alembic 迁移（`d001`–`d039`）与宿主机 `launchd` 服务组成；运行时模块职责、数据流、自有数据库表与常驻服务清单如下。

Dalton 用配置覆盖（overlay）和 `DALTON-HOOK` 挂在上游前后端之上，核心研究编排复用 LangAlpha 的沙箱智能体引擎。团队认证、预算控制、知识治理、监控面板、自动化报告和外部数据源全部收在独立目录和独立 Alembic 迁移链里。

## 1. 与上游 LangAlpha 的关系

Dalton 靠**配置叠加 + 目录隔离 + Hook 标记守卫**三层边界，保住持续同步上游的能力：

- **配置叠加（YAML Overlay）**：设好环境变量 `APP_ENV=dalton` 后，配置读取器（`src/ptc_agent/config/file_utils.py:load_yaml_config`）会自动把 `agent_config.dalton.yaml` 和 `config.dalton.yaml` 叠在根目录基准配置 `agent_config.yaml`、`config.yaml` 上。YAML 映射按层级合并，列表则整体替换（比如 `config.dalton.yaml` 里的 `market_data.providers` 会完整换掉上游默认链）。编排叠加用 `docker-compose.yml` 配 `docker-compose.dalton.yml`。Dalton 专属行为默认值收在 `src/dalton/dalton.yaml`，通过 `src/dalton/settings.py:get_dalton_config` 读取。
- **目录所有权（`dalton_paths.txt`）**：`scripts/dalton/dalton_paths.txt` 登记了所有 Dalton 独占的文件和目录，比如 `src/dalton/`、`plugins/dalton/`、`plugins/dalton_data/`、`scripts/dalton/`、`config/dalton/`、`deploy/dalton/`、`docs/dalton/`、`web/src/pages/Settings/panels/dalton/`、`web/src/components/dalton/` 等。这些路径里不用标 `DALTON-HOOK`。
- **上游文件挂载（`DALTON-HOOK`）**：改到非独占上游文件时，要在改动行上下 ±25 行内标上 `# DALTON-HOOK(<id>)`（前端用 JS/TS 注释形式），而且 `<id>` 必须在 `scripts/dalton/check_hooks.sh:ALLOWED_HOOKS` 白名单里。`scripts/dalton/check_hooks.sh --strict --audit` 以 `git merge-base HEAD upstream/main` 为基准，校验全部改动和必需的关键 hook（`P0_REQUIRED_HOOKS`），同时拦住对上游受保护模型清单和基础配置的修改。

## 2. 模块地图（`src/dalton/*`）

`src/dalton/` 下按职责分成 21 个子包和若干顶层辅助模块，共同组成 Dalton 业务层：

### 2.1 应用入口、权限与配置基础

- `src/dalton/app/`：后端装配入口。`dalton_router`（`src/dalton/app/__init__.py`）把知识、发布、覆盖、预算、监控、自动化、渠道和健康路由组合在一起，由 `src/server/app/setup.py` 导入并在上游 `oauth_router` 之前挂到 FastAPI 主应用。`internal_llm_router` 等内部路由供宿主机桥接或自动化流程安全调用。
- `src/dalton/settings.py` & `src/dalton/dalton.yaml`：Dalton 配置和开关的单一权威源（`get_dalton_config`、`get_dalton_settings`），涵盖时区（`Asia/Shanghai`）、`credentials.sharing: team`、功能总开关（`artifact_publishing`、`computer_cli_tokens`、`problem_reports`、`coverage_units`、`data_jobs`、`colleague_dispatch`）、代理网络 fake-IP 放行和 DoH 真实地址解析等。
- `src/dalton/access_tiers.py` & `src/dalton/auth.py` & `src/dalton/invites.py` & `src/dalton/sharing.py`：实现团队三层权限模型（`admin` / `internal` / `external`），对接 Supabase JWT 认证（`DALTON_AUTH_MODE=supabase`），管理 `dalton_invites` 邀请注册、工作区分享（`dalton_workspace_sharing`）和用户共享。
- `src/dalton/credentials.py`：团队凭据共享逻辑，让拥有者授权的 OAuth / API Key 可以安全服务团队成员，又不暴露明文。

### 2.2 研究资产、知识治理与覆盖

- `src/dalton/coverage/`：解析标准覆盖名单 `config/dalton/coverage.yaml`（Core 27 + Watch 78，共 105 家）和行业覆盖单元（`dalton_coverage_units`、`dalton_sector_members`），提供标的和工作区身份解析（`identity.py`）。
- `src/dalton/knowledge/`：结构化公司与行业研究知识层，维护带版本号（`dalton_knowledge_versions`）和跨模型审阅意见（`dalton_verdicts`）的研究记忆及只读回溯。
- `src/dalton/publish/`：正式研究发布治理门，校验来源追溯完备性和独立模型家族审阅，通过后生成不可变发布档案（`dalton_publish_requests`、`dalton_publications`）。
- `src/dalton/provenance/`：声明来源解析（`declared_sources.py`）和 SEC/研报定位追溯，把 MCP 工具返回的 `_declared_sources` 元数据转成结构化来源卡片。

### 2.3 智能体工具、中间件与外部数据

- `src/dalton/tools/`：注册给智能体的 Dalton LangChain 工具，包括知识读写、发布提交、自主任务、公司/部门协同请求、周播客读写（`podcast_weekly_tools.py`）以及自动化报告提交工具（`automation_report_tools.py` 里的 `automation_report`）。
- `src/dalton/middleware/`：在 LangGraph 智能体创建前注入 Dalton 系统指令、工作区方法论技能索引和声明来源钩子。
- `src/dalton/llm/` & `src/dalton/budget/`：管模型偏好校验（`validate_dalton_model_preferences`）、通知路由校验和本地美元预算控制（`LocalBudgetPort`，按租约分片预占并发额度，结束后对账结算）。
- `src/dalton/extended_hours.py`、`src/dalton/market_data_fallback.py`、`src/dalton/market_data/` & `src/dalton/data_map/` & `src/dalton/web_search/`：顶层模块 `extended_hours.py` 和 `market_data_fallback.py` 分别负责**美股**盘前盘后扩展时段行情补全（`XNYS` 日历，显式排除非美股和指数标的）及 `yfinance` 优先 / FMP 兜底回退。`src/dalton/market_data/`（`calendar_fallback.py`）提供日历回退；`data_map/health.py` 负责数据源健康探测与路由规则校准；`web_search/` 提供 Gemini Grounding（`gemini_grounding.py`）和直连 xAI X 检索（`x_search_direct.py`）联网能力。
- `src/dalton/crawl_service/`：独立的网页抓取微服务入口（`src/dalton/crawl_service/app.py`），跑在隔离的 Docker 网络 `crawl` 里，靠 `INTERNAL_SERVICE_TOKEN` 鉴权，隔离沙箱直接触网的风险。

### 2.4 自主运行、数据任务、监控面板与推送

- `src/dalton/autonomy/`：包含系统任务聚合视图（`system_jobs.py`）、定时和股价异动唤醒（`schedule.py`、`price_trigger.py`）、前瞻预检日志（`dalton_preflight_log`）和报告自动恢复投递（`report.py`）。
- `src/dalton/data_jobs/`：确定性定时数据任务层，解析 `src/dalton/data_jobs/manifest.yaml`，按容器内（`container_runner.py`）或宿主机桥接（`host_runner.py`）执行脚本任务，管调度循环和内存水位检查（`scheduler.py`）。
- `src/dalton/monitors/`：团队监控面板平台（[monitors.md](./features/monitors.md)），包含四档权限控制（`permissions.py`）、数据任务注册表（`MONITOR_DATA_JOB_REGISTRY`）、静态文件边界 Containment 与实时数据写入、HMAC 短时访问授权（`grants.py`）和版本历史。
- `src/dalton/notify/`：通知与外部渠道投递核心（`dispatch.py`），把自动化报告、失败告警、飞书文档授权和系统告警路由到本地渠道网关。
- `src/dalton/services/` & `src/dalton/workflows/` & `src/dalton/conversations/` & `src/dalton/database/`：`services/` 管自动化配置生命周期与乐观锁（`company_automations.py`）、原生模型与部署健康态查询（`openclaw_admin.py`、`native_admin.py`）；`conversations/` 负责对话归档抽卡；`database/` 统一管 Dalton 的原始 `psycopg3` 异步 SQL 查询和事务锁。
- **历史命名沿用（运行时完全解耦）**：`src/dalton/services/openclaw_admin.py`（提供 `GET /api/v1/dalton/openclaw/health` 构建指纹检查）、`src/dalton/database/openclaw_sync.py`、`dalton_openclaw_sync_runs` 表、数据任务环境变量 `OPENCLAW_WORKSPACE` 与容器内挂载路径 `/Users/everflow/.openclaw/workspace`（宿主机对应目录 `var/dalton/openclaw-data`）均沿用早期迁移阶段的历史命名，运行时完全由 Dalton 自有进程与受管存储执行；`plugins/dalton/skills/dalton-bullbear-tracker` 与 `scripts/dalton/ai_finance*` 中的路径检查则用于校验执行目录限定在受管目录之内（详见 [openclaw-retirement.md](./features/openclaw-retirement.md)）。

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
1. **容器与宿主机隔离**：后端和沙箱不直接碰宿主机敏感凭据和外部商业数据库二进制。所有宿主机桥接都走回环端口，带 `INTERNAL_SERVICE_TOKEN`、`DALTON_BRIDGE_TOKEN`（MCP 路径）或 `DALTON_BRIDGE_ADMIN_TOKEN`（宿主机数据任务等管理路由，通过 `Authorization: Bearer` 或 `X-Dalton-Bridge-Token` 发送）通信。`researchdata_bridge.py` 启动子进程前会用 `bridge_subprocess_env.py` 剥掉无关密钥。
2. **监控面板实时推送**：宿主机 `FeedDaemon`（`scripts/dalton/monitor_feeds/run_feeds.py`）抓行情并算完快照后，走 `PUT /api/v1/dalton/monitors/internal/{slug}/data/{key}`（带 `X-Service-Token`）写入 `dalton_monitor_data`。前端看面板时，拿带过期时间的 `grant`（HMAC 签名）拉取静态 HTML 和最新 JSON，每次读取都会实时重查成员权限。

## 4. 数据库表与迁移编号

Dalton 在上游库之外维护独立的 Alembic 版本表 `alembic_version_dalton`（配置文件 `src/dalton/alembic.ini`），迁移脚本放在 `src/dalton/migrations/versions/`。全部分支共有 **38 个**迁移脚本（编号 `d001`–`d039`，**无 `d034`**），中间有一段非线性串联：`d031 -> d035 -> d036 -> d032 -> d033 -> d037 -> d038 -> d039`。

| 迁移范围 | 核心表与数据结构 | 用途说明 |
|---|---|---|
| `d001`, `d007` | `dalton_budget_config`、`dalton_budget_task_caps`、`dalton_budget_reservations`、`dalton_llm_journal`、`dalton_provider_events` | 本地预算配置和预占账本、LLM 调用与模型事件流水 |
| `d002`, `d012` | `dalton_openclaw_sync_runs`（后增 `source` 列） | 同步与模型巡检运行台账（含 `native` 记录） |
| `d003`–`d005`, `d021`, `d024` | `dalton_knowledge_versions`、`dalton_verdicts`、`dalton_publish_requests`、`dalton_publications`、`dalton_publication_files`、`dalton_report_issues`、修改 `provenance_records` | 知识版本、跨模型审阅、正式发布、行业知识文档和问题上报 |
| `d006`, `d008`, `d013`, `d037` | `dalton_watch_state`、`dalton_signals`、`dalton_preflight_log`（含 `wake_context`）、`dalton_daily_plans`、`dalton_price_move_firings` | 自主研判状态、日计划、唤醒上下文和股价触发去重 |
| `d009`, `d010`, `d017`, `d022`, `d033` | `dalton_conversation_cards`、`dalton_conversation_processed`、`dalton_conversation_runs`、`dalton_wiki_ingests`、`dalton_transcription_jobs`、`dalton_chat_file_uploads` | 对话归档卡片、Wiki 入库、音频转写和聊天大文件登记 |
| `d011`, `d015`, `d023`, `d025`–`d027` | `dalton_data_source_health`、`dalton_data_map_cache`、`dalton_coverage_groups`、`dalton_coverage`、`dalton_earnings_calendar_mirror`、`dalton_coverage_units`、`dalton_sector_members`、`dalton_cross_unit_requests` | 数据地图健康态、覆盖名单、财报镜像和覆盖单元协同 |
| `d016`, `d018`, `d020`, `d028` | `dalton_members`、`dalton_invites`、`dalton_workspace_sharing`、`dalton_user_sharing`、`dalton_workspace_settings`、`dalton_user_settings` | 成员分级、邀请注册、工作区与用户共享，以及个人/工作区设置 |
| `d014`, `d019`, `d029`, `d035` | `dalton_notify_deliveries`、`dalton_feishu_read_scopes`、`dalton_data_task_defs`、`dalton_data_task_slot`、`dalton_data_task_runs`、`dalton_internal_feishu_groups`、`dalton_notify_storage_state`、`dalton_company_migration_batches` | 通知投递台账、飞书授权范围、确定性数据任务和公司自动化迁出批次 |
| `d030`, `d031`, `d036`, `d032` | `dalton_channel_identities`、`dalton_channel_bind_codes`、`dalton_channel_user_settings`、`dalton_channel_chat_bindings`、`dalton_channel_session_generations`、`dalton_channel_thread_watches`、`dalton_channel_inbound_events`、`dalton_channel_background_runs`、`dalton_channel_deliveries`、`dalton_channel_automation_runs`、`dalton_channel_weixin_state`、`dalton_channel_weixin_context_tokens` | 多渠道接入网关身份绑定、会话映射、防重入和微信上下文令牌 |
| `d038`, `d039` | `dalton_monitors`、`dalton_monitor_members`、`dalton_monitor_data`、`server_keys`（`monitor_grant`）、`dalton_monitor_publish_runs`、`dalton_monitor_versions` | 监控面板元数据、成员 ACL、实时数据键值、签名密钥、发布历史和不可变版本 |

## 5. 宿主机常驻进程（`launchd` 服务清单）

macOS 宿主机靠 `launchd` 跑脱离容器网络的本地桥接、采集、归档和备份任务。所有带 `StartCalendarInterval` 的定时触发都按**宿主机本地日历时区**执行，模板和安装脚本不做时区换算；要按北京时间触发，宿主机系统时区必须是 `Asia/Shanghai`。

| Launchd Label | 模板 / 配置位置 | 端口 / 触发方式 | 核心职责 |
|---|---|---|---|
| `com.dalton.researchdata-bridge` | `scripts/dalton/launchd/com.dalton.researchdata-bridge.plist.template` | `127.0.0.1:8960` | 宿主机 MCP 桥接服务、AlphaEngine 搜索缓存、`podcast_weekly`、宿主机 `data_job` 受控执行、`system_jobs` 宿主机探测 |
| `com.dalton.channel-gateway` | `scripts/dalton/launchd/com.dalton.channel-gateway.plist.template` | `127.0.0.1:8962` | 多渠道接入网关（飞书 WebSocket + 卡片/文档授权、Discord、微信 iLink 出入站）与运维告警推送 |
| `com.dalton.monitor-feeds` | `scripts/dalton/monitor_feeds/com.dalton.monitor-feeds.plist` | 交易时段 60s 轮询 / 非交易时段最长 300s 休眠 | A 股盘中监测（`cn-intraday`）与港股动量（`hk-momentum`）实时抓取并推送监控面板数据 |
| `com.dalton.config-git-backup` | `scripts/dalton/ops/launchd/com.dalton.config-git-backup.plist.template` | 每 6 小时（宿主机本地时区 00:15/06:15/12:15/18:15） | 加密备份配置和轻量状态到 GitHub 私有仓库 |
| `com.dalton.db-offsite` | `scripts/dalton/ops/launchd/com.dalton.db-offsite.plist.template` | 每日 03:40（宿主机本地时区） | 流式加密上传最新 `dalton-pg-*.dump` 到 Google Drive |
| `com.dalton.data-backup` | `scripts/dalton/ops/launchd/com.dalton.data-backup.plist.template` | 每周日 05:00（宿主机本地时区） | 流式加密上传 MinIO、沙箱工作区、Redis 快照与 `var/dalton`（含 SQLite 一致副本） |
| `com.dalton.backend-watchdog` | 运行时引用自 `scripts/dalton/researchdata_bridge.py:_inspect_backend_watchdog_job_sync` | 定时巡检 | 宿主机后端与桥接服务健康状态巡检（仓库里不放它的 plist 模板文件） |

## 6. 外部依赖

- **数据库与缓存**：`dalton-postgres:18-pgvector`（pgvector 向量检索）、Redis（SSE 缓冲与并发协调）、MinIO（`storage` profile，Chainguard 镜像，只绑 `127.0.0.1:9000` 供 ASR 和聊天大文件使用，及 `127.0.0.1:9001` 管理控制台）。
- **认证与反向代理**：Supabase Auth（`DALTON_AUTH_MODE=supabase` 线上认证与邀请流）、Caddy `edge`（`caddy:2.9.1-alpine`，托管 `./web/dist` 与反代后端）和 Tailscale Funnel（宿主机 `:8443` 对外转到 Caddy `127.0.0.1:8088`）。
- **金融数据源与 AI 模型**：`ginlix-data`、Tushare、yfinance、FMP、akshare、SEC EDGAR（`dalton_sec`）、腾讯/东方财富/港交所/深交所港股通公开接口、AlphaEngine（走宿主机桥接）、Google Gemini（`gemini_grounding` 与 `gemini-*` 模型）、xAI（`x_search_direct` 与 `grok-*`），以及按团队 OAuth 或 API Key 接入的 Anthropic/OpenAI/DeepSeek/Zhipu 等。
- **团队协同与渠道**：飞书开放平台（卡片、群聊、飞书文档授权与系统告警）、Discord、微信 iLink，以及用来做异地灾备的 GitHub 私有仓库和 Google Drive（`rclone`）。
