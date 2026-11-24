# 研究数据桥按职责拆分为连接器（独立核心版本第 ② 阶段）

> 状态：设计稿（2026-11-24）
> 前置文档：[standalone-core.md](./standalone-core.md)、[alphaengine-cache.md](./alphaengine-cache.md)、[data-jobs.md](./data-jobs.md)
> 关联代码：`scripts/dalton/researchdata_bridge.py`、`src/dalton/capabilities.py`、`src/dalton/content_assembly.py`、`plugins/dalton_sec/`

## 0. 背景与既定前提

当前宿主机单进程服务 `scripts/dalton/researchdata_bridge.py`（共 26,636 行，默认监听 `127.0.0.1:8960`）同时承载内部研究检索、向量嵌入、语音转写、飞书通知与文档代理、宿主任务执行及生产运维巡检。在独立核心版本第 ① 阶段（见 `docs/dalton/features/standalone-core.md:54-92`）中，`alphaengine`、`guidepoint`、`company_wiki`、`sales_notes`、`tmtb`、`funda_ai`、`feishu_docs`、`transcription` 八项能力均通过 `depends_on=(CapabilityId.RESEARCHDATA_BRIDGE,)` 绑定在总开关 `researchdata_bridge` 上（`src/dalton/capabilities.py:95-159`），独立核心部署只能整体关闭整座桥，缺少按需单独启用某项数据源或转写服务的能力。

本阶段设计遵循以下已确定的架构前提（2026-11-24 拍板）：

1. **Everflow 生产固定运行于当前 Mac 宿主**：AlphaEngine 连接器仅面向 macOS 设计，继续读取 macOS 钥匙串（`security` CLI）与 `/Applications/AlphaEngine.app` 桌面端登录态；核心安装在 Linux 宿主上将 `alphaengine` 保持为关闭态。
2. **研究检索来源逐一拆分为独立 MCP 连接器插件**：采用与 `plugins/dalton_sec/` 一致的 Agent Plugins 1.0.0 规范（见 `plugins/README.md:1-60` 与 `src/ptc_agent/config/plugins.py:278-355`）；商业与基金私有连接器代码放入私有仓库，公开仓库保留能力枚举、接口契约、装配规则与开关。
3. **向量嵌入与语音转写各自独立**：`/embed` 收归后端内置嵌入模块（支持可选外部嵌入服务 URL 覆盖）；`/transcriptions*` 拆为独立的转写连接器服务。
4. **通知与飞书代理移交渠道网关**：`/notify` 与 `/feishu/*` 在本文明确接口边界与交接条件，具体合并工作归入第 ③ 阶段 `integrations/dalton-channel-gateway/`。
5. **生产宿主巡检归入生产运维目录**：`/jobs/ops` 归入 Everflow 专属生产运维脚本目录 `scripts/dalton/ops/prod/`。
6. **落地前冻结桥外耦合**：在本设计各批次落地前，针对现行桥的维护仅限于 `scripts/dalton/researchdata_bridge.py` 内部实现调整，保持外部契约稳定。

---

## 1. 现状清单

### 1.1 HTTP 顶层路由组清单

`scripts/dalton/researchdata_bridge.py` 在 `_handle_client`（`scripts/dalton/researchdata_bridge.py:25344-26167`）中分发全部 HTTP 请求。除 `/healthz` 与 `/mcp` 外，其余路径统一在 `scripts/dalton/researchdata_bridge.py:25622` 通过 `_require_admin_token`（`scripts/dalton/researchdata_bridge.py:25316-25342`）校验 `DALTON_BRIDGE_ADMIN_TOKEN`。（注：下表中 `<DALTON_RESEARCH_CODE>`、`<DALTON_RESEARCH_HOME>`、`~/Library/`、`/Applications/AlphaEngine.app` 等宿主机外部目录取自桥配置定义，仓库外本机实际文件排布待核实。）

| 路由前缀与方法 | 代码入口 | 职责说明 | 调用方（后端 / 沙箱技能 / 运维脚本） | 依赖的本机资源 | 当前对应能力 |
|---|---|---|---|---|---|
| `GET /healthz` | `scripts/dalton/researchdata_bridge.py:25419-25453` | 返回服务版本、并发限流器状态、AlphaEngine 当日读文计数与熔断器状态、`xreach`/`tmeet` CLI 可用性快照 | 运维检查脚本：`scripts/dalton/check_bridge.sh:32-36`、`scripts/dalton/install_bridge.sh:51-90`、`scripts/dalton/realrun/check_env.py:835`、`scripts/dalton/export_review_window.sh:222` | 内存并发状态、AlphaEngine SQLite 计数库、本机 `xreach` 与 `tmeet` 可执行文件 | `researchdata_bridge` |
| `POST /mcp`<br>`POST /mcp/<token>` | `scripts/dalton/researchdata_bridge.py:25456-25619`；分发函数 `handle_tools_call` 于 `:13541-15264` | Streamable HTTP JSON-RPC 端点，暴露 `catalog`、`research`、`finance`、`macro`、`community` 5 个 MCP 工具，校验 `DALTON_BRIDGE_TOKEN` 与沙箱每次调用签发的 `X-Dalton-Caller-Token`（`:25560-25589`） | 1. 沙箱 PTC 包装器：`plugins/dalton_data/mcp.json:4-10` + `src/ptc_agent/core/sandbox/mcp_client_runtime.py:2202-2220`<br>2. 后端数据地图探针：`src/dalton/data_map/health.py:173-235`、`:790-1060`<br>3. 后端自治探针：`src/dalton/autonomy/probes/cnhk.py:184-310`<br>4. 沙箱技能脚本（详列于 1.2 节） | 按子路由分别依赖 `:8950`、`:8943`、本地 SQLite 向量库、宿主机 CLI（详列于 1.2 节） | `researchdata_bridge` 及 `BRIDGE_ROUTE_CAPABILITY_MAP` 子能力（`src/dalton/capabilities.py:775-786`） |
| `POST /embed` | `scripts/dalton/researchdata_bridge.py:25626-25642`；处理函数 `handle_embed` 于 `:15419-15489` | 调用 `company_wiki_cli.py` 子进程请求 Gemini Embedding API（默认 `gemini-embedding-2-preview`，3072 维），支持批量 100 条文档或单条查询向量化 | 后端对话归档模块：`src/dalton/conversations/embedder.py:75-207`（`embed_texts_via_bridge`、`embed_query_via_bridge`），由 `src/dalton/conversations/search.py:259` 检索调用 | `<DALTON_RESEARCH_CODE>/skills/company-wiki/scripts/company_wiki_cli.py`、宿主机环境变量 `GEMINI_API_KEY` | 代码内硬编码检查 `researchdata_bridge`（`src/dalton/conversations/embedder.py:150`），业务归属 `conversation_archive` |
| `GET /jobs/ops` | `scripts/dalton/researchdata_bridge.py:25645-25651`；处理函数 `handle_get_ops_jobs` 于 `:15766-15794` | 读取 macOS `launchctl` 状态与日志文件，汇总 `com.dalton.pg-backup` 与 `com.dalton.backend-watchdog` 运行状态 | 后端系统任务视图：`src/dalton/autonomy/system_jobs.py:261-315`（`_fetch_ops_jobs` -> `_call_host_bridge` 于 `:232-259`） | macOS `/bin/launchctl`、`~/Library/LaunchAgents/*.plist`、`~/Library/Logs/dalton-pg-backup.log`、`~/Backups/dalton/.watchdog_failures` | `researchdata_bridge`（`src/dalton/autonomy/system_jobs.py:241,265`） |
| `POST /notify` | `scripts/dalton/researchdata_bridge.py:25659-25675`；处理函数 `handle_notify` 于 `:15796-15983` | 调用 `feishu_send_card.py` 发送飞书交互卡片通知 | 后端通知分发器：`src/dalton/notify/dispatch.py:1985-2080`、`:5148`（当 `DALTON_NOTIFY_VIA_GATEWAY` 关闭时走桥，见 `src/dalton/settings.py:1473-1486`） | `<DALTON_RESEARCH_CODE>/scripts/feishu_send_card.py`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET` | `channel_feishu` / `researchdata_bridge` |
| `POST /wiki/ingest` | `scripts/dalton/researchdata_bridge.py:25679-25699`；处理函数 `handle_wiki_ingest` 于 `:17050-17850` | 校验 Markdown 载荷、幂等去重、加文件锁并调用 `company-wiki/run.sh` 写入 Wiki Markdown 文件及更新 SQLite 向量索引，记录审计日志 | 后端直连工具：`src/dalton/tools/wiki_tools.py:447-485`、`:676`（`ingest_company_wiki`）；统一入库工具 `src/dalton/tools/ingest_tools.py:615`、`:661-672`（`ingest_research`） | `<DALTON_RESEARCH_HOME>/wiki/` 目录与 `wiki/vectors.db`、`var/dalton/wiki_ingest_audit.jsonl`、`GEMINI_API_KEY` | `company_wiki`（`src/dalton/tools/__init__.py:189-205`） |
| `POST /sales-notes/ingest` | `scripts/dalton/researchdata_bridge.py:25703-25723`；处理函数 `handle_sales_notes_ingest` 于 `:18052-18430` | 校验销售笔记载荷并调用 `market-digest/run.sh add-note` 写入原始 JSON/文本清单与 SQLite 向量分块库 | 后端直连工具：`src/dalton/tools/ingest_tools.py:72-105`、`:261`、`:578-589`（`ingest_sales_note`、`ingest_research`） | `<DALTON_RESEARCH_HOME>/state/sales-notes/manifest.jsonl`、`skills/market-digest/vectors.db`、`GEMINI_API_KEY` | `sales_notes`（`src/dalton/tools/__init__.py:194-205`） |
| `POST /feishu/*`（共 8 个子路径） | `scripts/dalton/researchdata_bridge.py:25728-25768`；处理函数 `:18469-19024`；客户端 `FeishuClient` 于 `:22528-23369` | 代理飞书开放平台 API：`doc/read`、`wiki/nodes`、`wiki/node`、`doc/create`、`doc/append`、`doc/grant`、`bot-info`、`chats` | 1. 飞书文档工具封装：`src/dalton/feishu.py:106-167`（`post_bridge_feishu`），供 `src/dalton/tools/feishu_tools.py:69-81,236,346,373,401,521,684` 调用<br>2. 后端管理路由：`src/dalton/app/router.py:2526`（`/feishu/spaces`）、`:2563`（`/notify/feishu-bot`）<br>3. 群组列表服务：`src/dalton/notify/groups.py:28-56` | 宿主机环境变量 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、飞书公网 API | 文档读写归 `feishu_docs`；`bot-info` 与 `chats` 归 `channel_feishu` |
| `POST /transcriptions`<br>`GET /transcriptions/{id}`<br>`GET /transcriptions/{id}/result`<br>`GET /transcriptions/{id}/result/raw`<br>`POST /transcriptions/{id}/retry`<br>`POST /transcriptions/{id}/cancel` | `scripts/dalton/researchdata_bridge.py:25773-25890`；处理函数 `:19030-19113`；任务引擎 `scripts/dalton/asr_jobs.py:1-160` 与 `scripts/dalton/seed_asr_client.py:1-160` | 接收音视频文件引用，调用 `ffprobe`/`ffmpeg` 转码切分，上传火山引擎 TOS 对象存储并轮询豆包 Seed-ASR 2.0 获取带说话人分离的逐字稿 | 1. 后端转写服务：`src/dalton/services/transcriptions.py:461-584`（供 REST 路由 `src/dalton/app/transcriptions.py:100`、Agent 工具 `src/dalton/tools/transcription_tools.py:410`、后台对账循环 `src/server/app/setup.py:909-919` 调用）<br>2. 沙箱转写技能通过后端 API 间接调用：`plugins/dalton/skills/dalton-transcript-polish/scripts/dalton_asr.py:49,382-588` | 本机 `ffmpeg`/`ffprobe`、`var/dalton/asr_jobs/` 工作目录、火山引擎 `VOLC_TOS_*` 与 `SEED_ASR_*` 凭证 | `transcription`（`src/dalton/capabilities.py:151-159`） |
| `POST /internal/market-digest/{prepare,read,output/get,output/save,output/finalize}` | `scripts/dalton/researchdata_bridge.py:25894-25954`、`:26082-26112`；处理函数 `:19770-19974` + `scripts/dalton/market_digest_access.py` | 为每日市场摘要任务拉取邮件/电报增量材料、读取分块、保存与定稿输出文件 | 后端内部摘要工具：`src/dalton/tools/market_digest_tools.py:42-43,286-289`、`src/dalton/tools/market_digest_output_tools.py:471-480` | 宿主机邮件与电报拉取脚本、`<DALTON_RESEARCH_HOME>/skills/market-digest/` 目录与专属 Python 虚拟环境 | `fund_automations`（`src/dalton/tools/__init__.py:216-245`） |
| `POST /internal/ai-finance/{prepare,read,signals,report}` | `scripts/dalton/researchdata_bridge.py:25958-26078`；处理函数 `:19531-19769` + `scripts/dalton/ai_finance_access.py` | 为 AI 金融跟踪任务准备输入材料、读写信号库与生成报告快照 | 后端内部工具：`src/dalton/tools/ai_finance_inputs_tools.py:309`、`src/dalton/tools/ai_finance_signals_tools.py:706`、`src/dalton/tools/ai_finance_report_tools.py:779` | `DALTON_AI_FINANCE_CONFIG` 指向的宿主机目录与本地状态库 | `fund_automations`（`src/dalton/tools/__init__.py:246-275`） |
| `POST /internal/data-tasks/{host-run,host-kill}` | `scripts/dalton/researchdata_bridge.py:26116-26141`；处理函数 `:19426-19530`、`:19975-20342` | 在宿主机受控执行 `manifest.yaml` 中标记为 `runner: "host"` 的定时采集任务（含进程组监控、超时终止、BBG 镜像同步与敏感词脱敏） | 后端数据任务宿主执行器：`src/dalton/data_jobs/host_runner.py:23-29,108-181`；对应任务为 `src/dalton/data_jobs/manifest.yaml` 中的 `T1`（`:5-26`）、`T29`（`:335-356`）、`T58`（`:549-577`）、`T34`（`:871-895`）、`T60`（`:897-936`）、`T61`（`:939-964`）、`T87`（`:1045-1070`） | 宿主机 `var/dalton/openclaw-data`、SSH 远端 Windows BBG 跳板机、`~/.local/share/everflow-tools/venvs/`、进程组信号 | `data_jobs`（`src/dalton/capabilities.py:199-205`） |

### 1.2 `/mcp` 内部工具与子路由清单

`/mcp` 暴露的工具及合法路由表定义在 `TOOL_VALID_ROUTES`（`scripts/dalton/researchdata_bridge.py:3212-3230`）与 `scripts/dalton/research_runtime/config/data_sources.json:1-473`。

| MCP 工具 | `route` 子路由 | 代码定义与分发位置 | 职责说明 | 主要调用方（精确位置） | 本机资源与外部依赖 | 对应能力 |
|---|---|---|---|---|---|---|
| `catalog` | 全部或按 `route` 过滤 | `scripts/dalton/researchdata_bridge.py:2160-2219`、`:13580-13650` | 返回可用工具、路由参数规范与调用示例 | 沙箱 `ExecuteCode` 首轮自省（见 `plugins/dalton/agents/dalton-evidence.md:22`、`plugins/dalton/skills/dalton-bullbear/SKILL.md:29`） | 内存静态 schema | `researchdata_bridge` |
| `research` | `alphaengine` | `scripts/dalton/researchdata_bridge.py:2517-2659`、`:13930-14041`；上游 `scripts/dalton/research_runtime/scripts/alphaengine-mcp/server.mjs:1-220` | 卖方研报、机构/个人会议纪要、录制任务状态与分类词典检索及分页正文读取；含 SQLite v4 短时缓存、并发合并、熔断器与软配额提示 | 1. 沙箱技能脚本：`plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:569-646,855-925`、`plugins/dalton/skills/dalton-bullbear-tracker/scripts/collect.py:600,2733`、`plugins/dalton/skills/dalton-sector-delta/scripts/collect_sector.py:89-90`、`plugins/dalton/skills/dalton-mark-weekly-newenergy/scripts/rd_transport.py:50-115`、`plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/richard_cov_transport.py:12-45`<br>2. 数据地图探针：`src/dalton/data_map/health.py:860-920` | 本机端口 `8950`（`com.dalton.alphaengine-mcp`）、`/Applications/AlphaEngine.app`（`scripts/dalton/research_runtime/scripts/alphaengine-mcp/server.mjs:112`）、macOS 钥匙串 `dalton-alphaengine`（`scripts/dalton/research_runtime/scripts/alphaengine-mcp/server.mjs:113-116`）、缓存库 `~/Library/Caches/dalton-researchdata-bridge/alphaengine_search_cache.sqlite3`（`scripts/dalton/alphaengine_search_cache.py`）、日志 `~/Library/Logs/alphaengine_usage.jsonl` | `alphaengine` |
| `research` | `transcript` | `scripts/dalton/researchdata_bridge.py:2704-2738`、`scripts/dalton/research_runtime/scripts/data_gateway.py:43` | 基于 AlphaEngine `IND_SECOND_5FA442AE` 类别定向检索业绩电话会逐字稿，共享 AlphaEngine 缓存与熔断器 | 技能脚本与工作流：`plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:880-920`、`plugins/dalton/skills/dalton-earnings-flash/SKILL.md:42`、`plugins/dalton/skills/dalton-full-coverage/references/branch-c-primary.md:21` | 与 `alphaengine` 相同（`:8950` + 钥匙串 + SQLite 缓存库） | `alphaengine`（`src/dalton/capabilities.py:778`） |
| `research` | `guidepoint` | `scripts/dalton/researchdata_bridge.py:2660-2703`；代理 `scripts/dalton/research_runtime/scripts/guidepoint_mcp_proxy.py:1-217` | 检索 Guidepoint 专家访谈问答摘录（关键词过滤、时间窗口与近期权重排序） | 技能脚本：`plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:855-925`、`dalton-bullbear-tracker/scripts/collect.py:2733`；健康探针 `src/dalton/data_map/health.py:921-965` | 本机 `127.0.0.1:8943`（`com.dalton.guidepoint-mcp-proxy`）、`guidepoint_mcp_tokens.json`、`GUIDEPOINT_CLIENT_ID`/`GUIDEPOINT_CLIENT_SECRET`、Auth0 刷新端点与 `https://clapi.guidepoint.io/mcp-server/v1/mcp` | `guidepoint` |
| `research` | `company-wiki` | `scripts/dalton/researchdata_bridge.py:2739-2791`、`scripts/dalton/research_runtime/scripts/research_library.py:162,170` | 内部公司 Wiki 目录列举、混合语义检索与 Markdown/配图只读读取 | 技能脚本：`plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:855-925`、`dalton-wiki-auto-ingest/scripts/wiki_ingest.py:93`；健康探针 `src/dalton/data_map/health.py:795-859` | `<DALTON_RESEARCH_HOME>/wiki/` Markdown 文件、`wiki/vectors.db` SQLite 库、`state/research-images/assets`、`GEMINI_API_KEY`（查询嵌入） | `company_wiki` |
| `research` | `sales-notes`（含兼容别名 `sales-mail`） | `scripts/dalton/researchdata_bridge.py:2792-2883`、`scripts/dalton/research_runtime/scripts/sales_notes_store.py:18-78` | 检索与精读交易台销售纪要、邮件分块与买方预期摘要 | 技能脚本：`plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:855-925`、`dalton-mark-weekly-newenergy/scripts/rd_transport.py:50-115`、`dalton-richard-coverage-weekly/scripts/richard_cov_transport.py:12-45` | `<DALTON_RESEARCH_HOME>/skills/market-digest/vectors.db`、`state/sales-notes/manifest.jsonl`、`GEMINI_API_KEY` | `sales_notes` |
| `research` | `tmtb` | `scripts/dalton/researchdata_bridge.py:2884-2929`；同步脚本 `scripts/dalton/research_runtime/scripts/tmtb_alphaengine_sync.py:1-160` | 检索 TMT 每日焦点摘要库（由同步任务从 AlphaEngine 抓取入库） | 技能脚本：`plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:855-925`、`dalton-bullbear-tracker/scripts/collect.py:2733` | `<DALTON_RESEARCH_HOME>/skills/market-digest/vectors.db`（`message_id LIKE 'alphaengine:%'`）、`GEMINI_API_KEY` | `tmtb` |
| `research` | `funda-ai` | `scripts/dalton/researchdata_bridge.py:2930-2975`、`scripts/dalton/research_runtime/scripts/funda_ai.py:1-160` | 检索与读取 FUNDA AI 独立研究纪要索引库 | 技能脚本：`plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:855-925`、`plugins/dalton/skills/dalton-bullbear-tracker/scripts/collect.py:2733` | `<DALTON_RESEARCH_HOME>/state/funda-ai/index.sqlite`（`scripts/dalton/research_runtime/scripts/funda_ai.py:126-127`） | `funda_ai` |
| `research` | `all` | `scripts/dalton/researchdata_bridge.py:2460-2516`、`:14042-14422` | 跨本地四库（`company-wiki`、`sales-notes`、`tmtb`、`funda-ai`）与远端两库（`alphaengine`、`guidepoint`）并行扇出初筛（20 秒宽限窗口） | 沙箱 `ExecuteCode` 交互式初筛（见 `plugins/dalton_data/plugin.json:16`） | 组合上述六个来源的全部资源 | `researchdata_bridge`（按已启用子源动态裁剪） |
| `research` | `tmeet` | `scripts/dalton/researchdata_bridge.py:3094-3138`、`:11686-11708` | 调用本机 `tmeet` CLI 列出与读取腾讯会议纪要和转写 | 技能脚本：`plugins/dalton/skills/dalton-tmeet-weekly-minutes/scripts/tmeet_minutes.py:87,358-501` | 宿主机 `tmeet` 二进制（`BridgeConfig.tmeet_bin` 于 `:1175-1183`）及本机登录态 | `fund_automations` + `feishu_docs`（`src/dalton/content_assembly.py:100-104`） |
| `research` | `gcal` | `scripts/dalton/researchdata_bridge.py:3139-3184`、`:11970-11993` | 调用本机 `gws` CLI 查询 Google Calendar 财报与会议日程 | 技能脚本：`plugins/dalton/skills/dalton-richard-earnings/scripts/richard_earnings.py:197,565-840` | 宿主机 `gws` 二进制（`:1184-1192`）、`DALTON_GCAL_CALENDAR_ID`、Google 本地凭据 | `fund_automations`（`src/dalton/data_map/sources.yaml:121-128`） |
| `research` | `discord-history` | `scripts/dalton/researchdata_bridge.py:3185-3209`、`:12079-12145` | 拉取白名单 Discord 频道历史消息供 Wiki 自动入库扫描 | 技能脚本：`plugins/dalton/skills/dalton-wiki-auto-ingest/scripts/wiki_ingest.py:93,475,719` | `DALTON_DISCORD_BOT_TOKEN`、`DALTON_DISCORD_HISTORY_CHANNELS`、Discord REST API | `channel_discord`（`src/dalton/capabilities.py:784`） |
| `finance` | `sec` / `estimates` / `cn-hk` | `scripts/dalton/researchdata_bridge.py:2220-2374`、`scripts/dalton/research_runtime/scripts/data_gateway.py:142-226` | SEC 公告章节/附件提取、卖方一致预期快照、港股与 A 股公告（披露易/巨潮）及财务初值查询 | 1. `sec`：`plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:932-943`、`dalton-daily-filing-alert/SKILL.md:63`<br>2. `estimates`：`dalton-bullbear/scripts/implied_model.py:21,200`<br>3. `cn-hk`：`dalton-stock-move/scripts/cn_price_fallback.py:253-277`、后端探针 `src/dalton/autonomy/probes/cnhk.py:184-310` | `<DALTON_RESEARCH_CODE>/skills/{findata-analyst,cn-hk-findata}`、`SEC_USER_AGENT`、港股/A 股公开源与平台缓存 | 美股核心能力已由 `dalton_sec` 与 `yf_analysis` 覆盖；`cn-hk` 当前挂 `researchdata_bridge` |
| `macro` | `fred` / `treasury` / `market-regime` | `scripts/dalton/researchdata_bridge.py:2375-2459`、`:1268-1307`、`:13537-13539` | FRED 宏观序列、美债收益率曲线、本地市场状态面板快照读取 | 沙箱 `ExecuteCode` 宏观查询（`plugins/dalton_data/plugin.json:16`） | `FRED_API_KEY`、`<DALTON_RESEARCH_HOME>` 下市场状态 JSON/CSV（核心版本已有内置 `macro` MCP 服务器） | `researchdata_bridge` |
| `community` | `xueqiu` / `employee` / `last30days` / `x-timeline` | `scripts/dalton/researchdata_bridge.py:2976-3093`、`:12578-12750` | 雪球讨论、Blind/Glassdoor 员工评价、跨社区近 30 天热度、X 账号时间线抓取 | 技能脚本：`plugins/dalton/skills/dalton-bullbear-tracker/scripts/collect.py:2198,2238`、`dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py:1435,1503`、`dalton-full-coverage/references/branch-d-offstreet.md:18-29` | `xueqiu_posts.py`、`employee_reviews_cli.py`、`last30days.py`、本机 `xreach` CLI | `researchdata_bridge`（核心版本已有 `x_api` 与 `scrape`） |

---

## 2. 目标形态

### 2.1 职责拆分总览映射表

拆分后，原本集中在 `researchdata_bridge.py` 的职责按边界归入五类去向：

| 原桥职责组 | 目标承载位置 | 进程与传输形态 | 仓库归属 | 核心安装（Standalone Core）启用方式 | Everflow 生产启用方式 |
|---|---|---|---|---|---|
| `research(route="alphaengine" \| "transcript")` | 独立连接器插件 `dalton-alphaengine`（MCP 服务名 `dalton_alphaengine`） | macOS 宿主机 `streamable-http` 服务（监听 `127.0.0.1:8951`，内聚 SQLite v4 缓存/熔断/配额账本，上游接 `:8950` `server.mjs`） | 私有仓库 | Linux 宿主固定关闭；macOS 核心安装可通过挂载私有插件并设 `DALTON_CAP_ALPHAENGINE=true` 开启 | 默认开启（`everflow` 形态默认 `True` + macOS 宿主检测通过） |
| `research(route="guidepoint")` | 独立连接器插件 `dalton-guidepoint`（MCP 服务名 `dalton_guidepoint`） | 沙箱内 `stdio` MCP 子进程（直接内置 Auth0 OAuth 刷新并请求 `https://clapi.guidepoint.io/mcp-server/v1/mcp`，取代宿主机 `:8943` 代理） | 私有仓库 | 安装私有插件、配置 `GUIDEPOINT_CLIENT_ID`/`SECRET` 与刷新令牌并开启 `DALTON_CAP_GUIDEPOINT=true` | 默认开启 |
| `research(route="company-wiki")` + `POST /wiki/ingest` | 检索归插件 `dalton-company-wiki`（MCP 服务名 `dalton_company_wiki`）；写入归后端内置模块 `src/dalton/wiki_store/`（供 `ingest_company_wiki` 直接调用） | 检索：沙箱 `stdio` MCP 子进程（挂载只读数据目录）或容器内 HTTP；写入：后端进程直写共享卷 `wiki/` + 后端嵌入模块 | 接口与本地 Markdown+SQLite 引擎可放公开仓库（或插件包），基金历史数据放私有数据卷 | 挂载 `wiki` 数据卷、设置 `DALTON_CAP_COMPANY_WIKI=true` 即可独立启用 | 默认开启 |
| `research(route="sales-notes")` + `POST /sales-notes/ingest` | 检索归插件 `dalton-sales-notes`（MCP 服务名 `dalton_sales_notes`）；写入归后端模块 `src/dalton/sales_notes_store/` | 检索：沙箱 `stdio` MCP 子进程（只读挂载销售笔记库）；写入：后端进程直写共享卷并调用后端嵌入模块 | 私有仓库（含交易台邮件清洗规范） | 安装私有插件、挂载数据卷并设 `DALTON_CAP_SALES_NOTES=true` | 默认开启 |
| `research(route="tmtb")` | 独立连接器插件 `dalton-tmtb`（MCP 服务名 `dalton_tmtb`） | 沙箱 `stdio` MCP 子进程（只读查询 TMTB SQLite 向量库；增量同步脚本归 `scripts/dalton/ops/prod/`） | 私有仓库 | 安装私有插件并设 `DALTON_CAP_TMTB=true` | 默认开启 |
| `research(route="funda-ai")` | 独立连接器插件 `dalton-funda-ai`（MCP 服务名 `dalton_funda_ai`） | 沙箱 `stdio` MCP 子进程（只读查询 `state/funda-ai/index.sqlite`） | 私有仓库 | 安装私有插件并设 `DALTON_CAP_FUNDA_AI=true` | 默认开启 |
| `research(route="all")` | 沙箱 Python 聚合辅助函数 `scripts/dalton/research_runtime/scripts/federated_search.py`（并发调用当前工作区已启用的连接器包装器） | 沙箱内 `ExecuteCode` 进程内并发调用 | 公开仓库（仅按当前已生成的 `tools.dalton_*` 动态扇出） | 自动对当前已启用的研究连接器做并发检索 | 自动聚合全部 6 个已启用连接器 |
| `POST /embed` | 后端嵌入服务模块 `src/dalton/embeddings/`（直接调用 Gemini / OpenAI 兼容 Embedding HTTP API，支持可选独立容器 `DALTON_EMBEDDING_SERVICE_URL`） | 后端 FastAPI 进程内异步客户端（`httpx.AsyncClient`） | 公开仓库 | 配置 `GEMINI_API_KEY` 或 OpenAI 兼容嵌入密钥后随 `conversation_archive` / `company_wiki` 即开即用 | 默认开启，直接复用后端 `GEMINI_API_KEY` |
| `/transcriptions*`（共 6 个端点） | 独立转写连接器服务 `dalton-transcription`（独立容器/服务进程，复用 `asr_jobs.py` + `seed_asr_client.py` HTTP 契约） | 独立 HTTP 服务容器 `transcription`（监听内部端口 `:8965`），后端 `src/dalton/services/transcriptions.py` 指向 `DALTON_TRANSCRIPTION_SERVICE_URL` | 火山引擎 Seed-ASR 适配器放私有仓库（或按插件挂载），公开仓库保留标准 HTTP 契约与通用 Whisper/OpenAI ASR 适配器 | 在 `docker-compose` 启用 `transcription` profile 并设 `DALTON_CAP_TRANSCRIPTION=true` | 按需开启（独立 `launchd` 或容器服务） |
| `POST /notify` 与 `POST /feishu/*`、`research(route="discord-history")` | 渠道网关 `integrations/dalton-channel-gateway/`（第 ③ 阶段承接通知、`bot-info`、`chats`、`discord-history`）；飞书文档直连迁入 `src/dalton/feishu_openapi.py` 或网关文档代理 | 渠道网关独立服务（`:8962`）+ 后端飞书文档客户端 | 公开/私有按第 ③ 阶段网关规范划分 | 开启 `channel_gateway` / `feishu_docs` 时生效 | 默认开启（`DALTON_NOTIFY_VIA_GATEWAY=true` 切流） |
| `GET /jobs/ops` | `scripts/dalton/ops/prod/export_ops_status.py`（定时落盘 JSON 至共享状态目录）+ 后端 `src/dalton/autonomy/system_jobs.py` 读取状态文件 | 宿主机 `launchd` 轻量定时写入状态快照文件 `var/dalton/ops_jobs_status.json` | 公开仓库 `scripts/dalton/ops/prod/`（打包发布时由 `package_release.sh` 剥离） | `core` 形态下 `ops` 列表为空（保持现有行为） | Everflow 宿主定时刷新快照供后端只读展示 |
| `/internal/{market-digest,ai-finance,data-tasks}/*` 及 `tmeet`/`gcal`/`cn-hk`/`community` | Everflow 宿主自动化执行器 `scripts/dalton/ops/prod/host_task_daemon.py`（承接 `fund_automations` 与 `data_jobs` 宿主任务）；`cn-hk` 拆为可选插件 `dalton-cnhk` | Everflow 宿主机轻量守护进程（仅服务 `fund_automations` 与 `data_jobs`） | 私有仓库 + `scripts/dalton/ops/prod/` | 核心安装关闭 `fund_automations` 与宿主任务；港股/A 股可通过 `dalton-cnhk` 插件按需开启 | 默认开启 |

### 2.2 研究检索连接器统一返回信封契约（最小化技能改动）

为使现有沙箱技能脚本中的结果提取函数（如 `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:649-745` 的 `_extract_route_items_with_page_info`）与溯源中间件 `src/dalton/provenance/declared_sources.py:81-120` 直接复用，所有拆分后的研究检索 MCP 连接器统一保持以下顶层 JSON 返回结构：

```json
{
  "ok": true,
  "status": "ok",
  "route": "alphaengine",
  "operation": "search_library",
  "normalized_params": {
    "query": "NVDA 英伟达",
    "start": "2026-10-01",
    "end": "2026-11-24",
    "limit": 20
  },
  "data": {
    "results": [
      {
        "document_id": "ae_doc_123",
        "title": "...",
        "published_at": "2026-11-20",
        "publisher": "...",
        "snippet": "...",
        "url": "https://..."
      }
    ],
    "page_info": {
      "has_more": false,
      "next_cursor": null
    }
  },
  "_sources": [
    {
      "identity": "broker_report",
      "source_type": "research_doc",
      "document_id": "ae_doc_123",
      "title": "...",
      "publisher": "...",
      "as_of": "2026-11-20",
      "url": "https://..."
    }
  ],
  "_declared_sources": []
}
```

- **空结果与错误语义保持一致**：查询成功但无匹配记录时返回 `{"ok": true, "status": "empty", "empty_reason": "no_matching_documents", "data": {"results": []}, "_sources": []}`；上游报错或熔断时返回 `{"ok": false, "status": "error", "error_code": "...", "error": "...", "hint": "..."}`。
- **溯源白名单同步注册**：在 `src/dalton/provenance/declared_sources.py:13` 的 `_ALLOWED_SERVERS` 集合中注册全部新连接器服务名（`dalton_alphaengine`、`dalton_guidepoint`、`dalton_company_wiki`、`dalton_sales_notes`、`dalton_tmtb`、`dalton_funda_ai`、`dalton_cnhk`），使 `src/ptc_agent/agent/middleware/provenance/middleware.py:1008` 自动将其 `_sources` / `_declared_sources` 展开为 `prov:` 引用锚点。

### 2.3 各连接器工具清单、入参出参与鉴权方式

#### （1）`dalton-alphaengine`（MCP 服务名：`dalton_alphaengine`，仅 macOS）
- **形态**：Agent Plugins 1.0.0 插件包（`streamable-http` 类型，连接宿主机 `dalton_alphaengine_server.py`，默认端口 `8951`），内部整合原桥中的 `scripts/dalton/alphaengine_search_cache.py`（SQLite WAL v4 缓存、单飞合并、熔断器、30 分钟同公司软提示、配额账本）并转发至本机 `127.0.0.1:8950`（`server.mjs`）。同时承载原 `alphaengine` 与 `transcript` 两条路由能力。
- **工具清单**：
  1. `search_library(query="", start=None, end=None, limit=20, cursor=None, sort=None, search_in=None, document_categories=None, type_ids=None, industry_names=None, meeting_scope=None, fresh=False)`：对应原 `route="alphaengine", operation="search_library"`，出参含 `data.results`、`data.page_info`、`_bridge_cache` 与 `_sources`。
  2. `get_document(document, content_field=None, offset=0, max_chars=30000)`：分页读取研报或会议纪要正文，支持独立字段翻页，命中当日已读文档免计额度。
  3. `get_document_info(document)` / `list_attachments(document)`：读取文档元数据与附件列表。
  4. `search_transcripts(ticker=None, query=None, start=None, end=None, limit=10)`：对应原 `route="transcript"`，定向检索 `IND_SECOND_5FA442AE` 业绩会转写，出参保留 `coverage`（含 `complete` 与 `continuation_params`）。
  5. `list_recording_tasks(status=None, limit=20)` / `get_recording_status(task_id)` / `get_recording_task(task_id)`：查询个人/机构会议录制状态。
  6. `list_filters(filter_type)`：读取 `categories`、`types`、`industries` 词典。
- **鉴权方式**：
  - 传输层：`Authorization: Bearer ${DALTON_ALPHAENGINE_MCP_TOKEN}`。
  - 调用方身份透传：直接复用沙箱运行时 `src/ptc_agent/core/sandbox/mcp_client_runtime.py:2165-2198` 的 `_apply_caller_headers`，自动随每次 HTTP 工具调用附带 `X-Dalton-Caller-Token`、`X-Dalton-User`、`X-Dalton-Workspace`、`X-Dalton-Thread`（由后端 `src/dalton/sandbox_caller_identity.py:94-118` 基于 HKDF 派生密钥签发），用于单线程软提示与配额审计。
  - 上游认证：`server.mjs`（`scripts/dalton/research_runtime/scripts/alphaengine-mcp/server.mjs:112-116`）读取 macOS 钥匙串条目 `dalton-alphaengine` 与 `/Applications/AlphaEngine.app` 本地会话。

#### （2）`dalton-guidepoint`（MCP 服务名：`dalton_guidepoint`）
- **形态**：`stdio` MCP 插件（结构同 `plugins/dalton_sec/mcp.json:1-17`），在沙箱 supervisor 内按需拉起 Python 子进程，内部集成原 `scripts/dalton/research_runtime/scripts/guidepoint_mcp_proxy.py:29-185` 的 Auth0 令牌刷新与远端 JSON-RPC 转发逻辑，直连 `https://clapi.guidepoint.io/mcp-server/v1/mcp`。
- **工具清单**：
  1. `search_transcripts(query=None, keywords=None, start=None, end=None, limit=10, recency_bias=None)`：返回问答片段列表 `data.results`（每条含访谈日期、专家身份描述、<=20 词合规引用提示及 `_sources` 中 `identity="expert"`）。
- **鉴权方式**：通过插件 `plugin.json` 声明 `vault_blueprints` 或环境变量注入 `GUIDEPOINT_CLIENT_ID`、`GUIDEPOINT_CLIENT_SECRET`、`GUIDEPOINT_REFRESH_TOKEN`，令牌刷新后缓存在进程内存与持久化凭据文件。

#### （3）`dalton-company-wiki`（MCP 服务名：`dalton_company_wiki`）与后端 Wiki 入库模块
- **形态**：
  - 只读检索端：`stdio`（沙箱绑定只读卷 `/mnt/dalton-wiki`）或容器内 `streamable-http` MCP 插件，复用 `scripts/dalton/research_runtime/scripts/research_library.py:162-220` 的混合检索逻辑。
  - 受控写入端：后端模块 `src/dalton/wiki_store/ingest.py`，由后端直连工具 `ingest_company_wiki`（`src/dalton/tools/wiki_tools.py:676`）与 `ingest_research`（`src/dalton/tools/ingest_tools.py:615`）直接调用，写入 `wiki/` Markdown 文件并通过后端 `src/dalton/embeddings/` 更新 `wiki/vectors.db`。
- **工具清单（MCP 只读端）**：
  1. `list_documents(company=None, category=None, limit=50)`：列举公司或主题 Wiki 页面目录。
  2. `search(query, company=None, start=None, end=None, limit=10)`：向量 + FTS 混合检索，返回 `wiki:<path>` 文档 ID 与摘要。
  3. `read_document(document, offset=0, max_chars=30000)`：读取指定 `wiki:` 文档全文及关联研究图表路径。
- **鉴权方式**：沙箱只读文件系统挂载隔离 + 查询嵌入使用 `GEMINI_API_KEY`（或统一嵌入服务密钥）；写入端仅在后端进程内对已认证用户开放，延续现有审计日志 `wiki_ingest_audit.jsonl`。

#### （4）`dalton-sales-notes`、`dalton-tmtb`、`dalton-funda-ai`（三个内部研究库连接器）
- **形态**：各自独立的 Agent Plugins 1.0.0 `stdio` MCP 插件包（`dalton_sales_notes`、`dalton_tmtb`、`dalton_funda_ai`），分别封装 `sales_notes_store.py`、`vectors.db`（`alphaengine:%` 分区）与 `funda_ai.py`（`state/funda-ai/index.sqlite`）；`POST /sales-notes/ingest` 迁入后端模块 `src/dalton/sales_notes_store/ingest.py`。
- **统一工具清单（每个连接器均提供相同签名）**：
  1. `search(query, ticker=None, start=None, end=None, limit=15, cursor=None)`：返回对应前缀文档 ID（`mail:<id>`、`tmtb:<id>`、`funda:<id>`）、发布时间、来源摘要与 `_sources`。
  2. `read_document(document, offset=0, max_chars=30000)`：分页读取单篇完整内容。
- **鉴权方式**：沙箱只读卷挂载对应 SQLite 库文件 + 向量查询密钥（`dalton_funda_ai` 纯 SQLite FTS 检索，零外部密钥依赖）。

#### （5）后端嵌入模块 `src/dalton/embeddings/`（承接原 `POST /embed`）
- **设计选型**：将原桥 `handle_embed`（`scripts/dalton/researchdata_bridge.py:15419-15489`）收归后端模块 `src/dalton/embeddings/client.py`，默认在后端进程内通过异步 HTTP 直接调用配置的提供商 API（Everflow 默认使用 `GEMINI_API_KEY` 请求 `gemini-embedding-2-preview` 3072 维向量；核心安装支持配置 Gemini 或 OpenAI 兼容 `/v1/embeddings` 端点，亦可通过 `DALTON_EMBEDDING_SERVICE_URL` 指向外部独立嵌入微服务）。
- **内部接口**：
  - `embed_texts(texts: list[str], *, task_type: str = "RETRIEVAL_DOCUMENT") -> tuple[list[list[float]], str]`
  - `embed_query(query: str) -> list[float] | None`
- **统一受益方**：`src/dalton/conversations/embedder.py:126-207`、`src/dalton/wiki_store/ingest.py`、`src/dalton/sales_notes_store/ingest.py` 共享同一套重试与限流客户端，解除 `src/dalton/conversations/embedder.py:150` 对 `CapabilityId.RESEARCHDATA_BRIDGE` 的耦合。

#### （6）独立转写连接器服务 `dalton-transcription`（承接原 `/transcriptions*`）
- **形态**：将 `scripts/dalton/asr_jobs.py` 与 `scripts/dalton/seed_asr_client.py` 封装为独立轻量化 FastAPI/aiohttp 服务 `dalton-transcription`（容器镜像内置 `ffmpeg`/`ffprobe`），完整保留原 6 个 HTTP 端点契约：
  - `POST /transcriptions`、`GET /transcriptions/{job_id}`、`GET /transcriptions/{job_id}/result`、`GET /transcriptions/{job_id}/result/raw`、`POST /transcriptions/{job_id}/retry`、`POST /transcriptions/{job_id}/cancel`。
- **鉴权与配置**：使用独立令牌 `DALTON_TRANSCRIPTION_TOKEN` 与服务地址 `DALTON_TRANSCRIPTION_SERVICE_URL`（如 `http://transcription:8965`）。后端 `src/dalton/services/transcriptions.py:461-584` 仅将基础 URL 与 Bearer Token 切换至转写专属配置；前端 REST 接口 `/api/v1/dalton/transcriptions*`、Agent 工具 `prepare_transcription` 与沙箱脚本 `dalton_asr.py` 保持零改动。

#### （7）渠道网关交接边界（`/notify`、`/feishu/*`、`discord-history` —— 第 ③ 阶段衔接）
- **通知推送（`POST /notify`）**：后端 `src/dalton/notify/dispatch.py:1985-2080` 已内置 `DALTON_NOTIFY_VIA_GATEWAY` 切换分支（`src/dalton/settings.py:1473-1486`）。第 ③ 阶段在 `integrations/dalton-channel-gateway/` 补齐卡片发送回执对齐后（网关侧具体路由命名待核实），将 `DALTON_NOTIFY_VIA_GATEWAY` 默认值切为 `true`，随即下线桥上 `/notify`。
- **飞书机器人与群目录（`POST /feishu/bot-info`、`POST /feishu/chats`）**：归属渠道连通性元数据，移交 `integrations/dalton-channel-gateway/` 的飞书适配器对外暴露，供 `src/dalton/app/router.py:2563` 与 `src/dalton/notify/groups.py:28-56` 调用。
- **飞书知识库与文档读写（`POST /feishu/doc/*`、`POST /feishu/wiki/*`）**：由 `src/dalton/feishu.py:106-167` 切换至渠道网关文档代理端点（或后端基于 `FEISHU_APP_ID`/`FEISHU_APP_SECRET` 直连飞书开放平台 OpenAPI），保持 `src/dalton/tools/feishu_tools.py` 的 6 个工具入参出参不变，能力继续受 `CapabilityId.FEISHU_DOCS` 独立控制。
- **Discord 频道历史拉取（`research(route="discord-history")`）**：并入渠道网关 Discord 通道只读历史接口，供 `dalton-wiki-auto-ingest` 通过网关或专用 MCP 工具读取。

#### （8）生产运维状态与宿主自动化脚本（`/jobs/ops` 与 `/internal/*`）
- **`/jobs/ops` 归入 `scripts/dalton/ops/prod/`**：新增宿主机脚本 `scripts/dalton/ops/prod/export_ops_status.py`，由 `launchd` 每 5 分钟执行一次，读取 `launchctl` 中 `com.dalton.pg-backup`、`com.dalton.backend-watchdog` 等宿主任务状态并原子写入 `<repo>/var/dalton/ops_jobs_status.json`。后端 `src/dalton/autonomy/system_jobs.py:261-315` 改为直接读取该状态文件（文件存在且 `DALTON_PROFILE=everflow` 时展示），彻底解除后端系统任务页对桥 HTTP 端口的调用。
- **`/internal/{market-digest,ai-finance,data-tasks}/*`**：抽取为宿主机专属执行守护脚本 `scripts/dalton/ops/prod/host_task_daemon.py`（仅保留进程组受控拉起与摘要文件准备，约 800 行），随 `fund_automations` 与 `data_jobs` 在 Everflow Mac 上运行。

### 2.4 公开仓库与私有仓库边界

| 资产类别 | 公开仓库（`langalpha` / 核心发行包）保留内容 | 私有仓库（`dalton-connectors`）承载内容 |
|---|---|---|
| 能力与配置开关 | `src/dalton/capabilities.py` 全部 `CapabilityId` 枚举、`MCP_SERVER_CAPABILITY_MAP`、`BUNDLED_PLUGIN_CAPABILITY_MAP`、`src/dalton/dalton.core.yaml` | Everflow 生产环境变量模板与商业上游端点配置 |
| 内容装配与文档门控 | `src/dalton/content_assembly.py`、`src/dalton/data_map/sources.yaml`、`plugins/dalton/skills/**` 中的 `<!-- DALTON_CAP:... -->` 标记 | 基金专属私有工作流脚本与内部花名册配置 |
| 通用公共连接器与基础设施 | `plugins/dalton_sec/`、`src/dalton/embeddings/`、`src/dalton/wiki_store/`（通用 Markdown+SQLite Wiki 引擎）、`scripts/dalton/research_runtime/scripts/federated_search.py`、转写服务标准 HTTP 客户端 `src/dalton/services/transcriptions.py` | `dalton-alphaengine`（含 `server.mjs` 与 SQLite 缓存服务）、`dalton-guidepoint`、`dalton-sales-notes`、`dalton-tmtb`、`dalton-funda-ai`、`dalton-cnhk`、火山引擎 Seed-ASR 2.0 后端实现 |
| 发布剥离校验 | `scripts/standalone/package_release.sh:66-195` 白名单导出机制，确保导出的核心包零私有代码与零内部标识残留 | 通过独立私有部署流水线（软链或插件挂载目录 `DALTON_EXTRA_PLUGINS_DIR`）注入 Everflow 生产或付费客户环境 |

---

## 3. 调用方迁移

### 3.1 沙箱技能脚本的调用机制演进

核查现有代码可见，沙箱技能脚本已完成从裸 HTTP 向 PTC MCP 包装器的主体过渡：
- `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:569-646` 中的 `resolve_researchdata_transport()` 默认优先导入沙箱自动生成的 `tools.researchdata` 模块并通过 `mcp_client.call_mcp_tool_with_error_flag("researchdata", ...)` 发起调用（`mode == "wrapper"`），仅在显式注入 `DALTON_RESEARCHDATA_URL` 时回退到直连 HTTP。
- `dalton-mark-weekly-newenergy/scripts/rd_transport.py:50-115` 与 `dalton-richard-coverage-weekly/scripts/richard_cov_transport.py:12-45` 已完全使用 `tools.researchdata` / `mcp_client` 包装器模式。

拆分后，沙箱技能采用 **PTC MCP 连接器包装器直调 + 共享路由适配层** 方案：
1. 沙箱启动时，`ToolFunctionGenerator`（`src/ptc_agent/core/tool_generator.py:734-760`）自动为当前工作区已启用的每个连接器生成 `tools/dalton_alphaengine.py`、`tools/dalton_guidepoint.py`、`tools/dalton_company_wiki.py`、`tools/dalton_sales_notes.py`、`tools/dalton_tmtb.py`、`tools/dalton_funda_ai.py`。
2. 在共享运行时新增轻量适配模块 `plugins/dalton/skills/_shared/connector_transport.py`，提供统一入口 `call_research_source(source, operation, **kwargs)`：
   - **优先路径（新连接器）**：检测 `mcp_client` 中对应的新服务名（如 `dalton_alphaengine`、`dalton_guidepoint` 等）已挂载时，直接调用 `mcp_client.call_mcp_tool_with_error_flag("dalton_<source>", mapped_tool, args)`。
   - **兼容路径（过渡期回退）**：当新连接器尚未挂载而旧 `researchdata` 包装器仍存在时，自动回退调用 `mcp_client.call_mcp_tool_with_error_flag("researchdata", "research", {"route": source, "operation": operation, ...})`。
   - **能力关闭优雅跳过**：当新旧服务均未挂载（说明该数据源能力在当前实例已关闭），立即返回 `{"ok": true, "status": "skipped_disabled_capability", "data": {"results": []}}`，采集脚本自动跳过该源并继续执行其余已启用数据源。

### 3.2 需迁移的沙箱技能与提示词逐项清单

| 技能 / 文件路径 | 当前调用位置与方式 | 迁移改动内容 |
|---|---|---|
| `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py` | `:569-646`（`resolve_researchdata_transport`）、`:855-925`（6 个研究路由采集）、`:932-943`（`finance(route="sec")`） | `resolve_researchdata_transport` 接入共享适配层 `connector_transport.py`，6 个研究源按各自连接器名调用；`:932-943` 的美股 SEC 检索切至核心内置 `tools.dalton_sec` |
| `plugins/dalton/skills/dalton-bullbear-tracker/scripts/collect.py` | `:157`、`:600-601`、`:2733`（复用 `thesis-delta` 传输层）、`:2198` 与 `:2238`（`community` 路由） | 随 `connector_transport.py` 自动切至各独立连接器；社区路由按能力检测调用或降级至内置 `x_api` / `scrape` |
| `plugins/dalton/skills/dalton-sector-delta/scripts/collect_sector.py` | `:89-90`（导入 `thesis-delta` 的 `call_bridge_jsonrpc` 与 `resolve_researchdata_transport`） | 改为导入 `_shared/connector_transport.py` 的 `call_research_source`，按已启用连接器动态构建采集任务列表 |
| `plugins/dalton/skills/dalton-mark-weekly-newenergy/scripts/rd_transport.py` | `:50-115`（调用 `tools.researchdata` 的 `alphaengine`、`sales-notes` 等） | 切换至 `connector_transport.py`，优先调用 `dalton_alphaengine` 与 `dalton_sales_notes` |
| `plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/richard_cov_transport.py` 及 `plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py` | `plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/richard_cov_transport.py:12-45`、`plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py:1435,1503` | 研究源切至 `connector_transport.py`；X 时间线优先走内置 `tools.x_api` |
| `plugins/dalton/skills/dalton-tmeet-weekly-minutes/scripts/tmeet_minutes.py` | `:87`、`:358-501`（`from tools.researchdata import research` 调 `route="tmeet"`） | 切换至独立小插件 `dalton_tmeet`（或宿主自动化工具），仅在 `fund_automations` 开启时运行 |
| `plugins/dalton/skills/dalton-richard-earnings/scripts/richard_earnings.py` | `:197`、`:565-840`（`from tools.researchdata import research` 调 `route="gcal"`） | 切换至 `dalton_gcal` 连接器工具，受 `fund_automations` 门控 |
| `plugins/dalton/skills/dalton-wiki-auto-ingest/scripts/wiki_ingest.py` | `:93`、`:475`、`:719`（调 `company-wiki` 与 `discord-history`） | Wiki 查重读取改调 `tools.dalton_company_wiki`；Discord 历史读取在第 ③ 阶段切至渠道网关接口 |
| `plugins/dalton/skills/dalton-stock-move/scripts/cn_price_fallback.py` | `:253-277`（调 `researchdata.finance(route="cn-hk")`） | 优先检测 `tools.dalton_cnhk`，缺省时走公开行情兜底 |
| `plugins/dalton/skills/dalton-transcript-polish/scripts/dalton_asr.py` | `:49`、`:382-588`（调用后端 `/api/v1/dalton/transcriptions*`） | 保持现有后端 API 调用方式，原脚本代码直接沿用 |
| 技能 Markdown 与参考文档（`plugins/dalton/agents/dalton-evidence.md:5,22-36`、`dalton-conventions/`、`dalton-bullbear/`、`dalton-earnings-{preview,flash,deep}/`、`dalton-full-coverage/`、`dalton-weekly-review/`、`dalton-financial-lenses/`、`dalton-filing-review/`、`dalton-company-background/`、`dalton-ai-app-weekly-monitor/`） | 原使用 `<!-- DALTON_CAP:researchdata_bridge -->` 包裹内部四库与调用示例（如 `dalton-bullbear/SKILL.md:29-55`、`dalton-weekly-review/references/core-five-sources.md:17-47`） | 将笼统的 `<!-- DALTON_CAP:researchdata_bridge -->` 细化为具体来源能力标签（如 `<!-- DALTON_CAP:company_wiki|sales_notes|tmtb|funda_ai -->`、`<!-- DALTON_CAP:alphaengine -->`、`<!-- DALTON_CAP:guidepoint -->`），并将代码示例更新为对应连接器工具名；美股 SEC 示例统一指向 `dalton_sec` 与 `get_sec_filing` |

### 3.3 需迁移的后端调用点逐项清单

| 后端模块文件与行号 | 当前调用的桥路由 | 迁移目标与改动方式 |
|---|---|---|
| `src/dalton/conversations/embedder.py:75-207`（被 `src/dalton/conversations/search.py:259` 调用） | `POST /embed`（且 `:150` 检查 `RESEARCHDATA_BRIDGE`） | 改为调用后端内部 `src/dalton/embeddings/client.py`；能力检查改为校验嵌入密钥或 `DALTON_EMBEDDING_SERVICE_URL` 已配置 |
| `src/dalton/autonomy/system_jobs.py:232-315` | `GET /jobs/ops` | 改为读取 `scripts/dalton/ops/prod/export_ops_status.py` 生成的本地状态文件 `var/dalton/ops_jobs_status.json` |
| `src/dalton/services/transcriptions.py:461-584` | `/transcriptions*`（6 个端点） | `_require_transcription_bridge_url()` 改为读取 `DALTON_TRANSCRIPTION_SERVICE_URL` 与 `DALTON_TRANSCRIPTION_TOKEN`（过渡期若未设置则回退读取旧桥 URL） |
| `src/dalton/tools/wiki_tools.py:447-485` 与 `src/dalton/tools/ingest_tools.py:615,661-672` | `POST /wiki/ingest` | 改为调用后端进程内 `src/dalton/wiki_store/ingest.py`（过渡期保留环境变量开关支持双路切换） |
| `src/dalton/tools/ingest_tools.py:72-105,261,578-589` | `POST /sales-notes/ingest` | 改为调用后端进程内 `src/dalton/sales_notes_store/ingest.py` |
| `src/dalton/data_map/sources.yaml:1-192`、`src/dalton/data_map/health.py:173-235,790-1060`、`src/dalton/data_map/builder.py:273-820` | 通过桥 `/mcp` 探测各子路由健康度与渲染 `<data_map>` | `sources.yaml` 中各数据源的 `required_capabilities` 移除 `researchdata_bridge`，只保留各自独立 `CapabilityId`；`health.py` 直接探测已注册的对应连接器 MCP 服务或本地 SQLite 库文件可读性 |
| `src/dalton/provenance/declared_sources.py:13` | `_ALLOWED_SERVERS = frozenset({"researchdata", "dalton_sec"})` | 扩充白名单加入 `dalton_alphaengine`、`dalton_guidepoint`、`dalton_company_wiki`、`dalton_sales_notes`、`dalton_tmtb`、`dalton_funda_ai`、`dalton_cnhk` |
| `src/dalton/autonomy/probes/cnhk.py:184-310` | `POST /mcp`（`finance(route="cn-hk")`） | 改为调用 `dalton_cnhk` 连接器或模块函数 |
| `src/dalton/notify/dispatch.py:1985-2080`、`src/dalton/feishu.py:106-167`、`src/dalton/app/router.py:2526,2563`、`src/dalton/notify/groups.py:28-56` | `POST /notify`、`POST /feishu/*` | 第 ③ 阶段统一切流至 `integrations/dalton-channel-gateway/` 与飞书 OpenAPI 直连客户端 |
| `src/dalton/data_jobs/host_runner.py:23-29,108-181` 及 `src/dalton/tools/{market_digest_*,ai_finance_*}_tools.py` | `/internal/data-tasks/*`、`/internal/{market-digest,ai-finance}/*` | 指向 Everflow 宿主守护脚本 `DALTON_HOST_RUNNER_URL`（`scripts/dalton/ops/prod/host_task_daemon.py`） |

### 3.4 兼容期并存与旧路由下线判定标准

1. **双路并存机制**：迁移期间保留 `plugins/dalton_data/`（`researchdata`）与宿主 `researchdata_bridge.py` 在 Everflow 环境运行；每批新连接器上线时，通过环境变量（如 `DALTON_USE_STANDALONE_EMBED=true`、`DALTON_TRANSCRIPTION_SERVICE_URL`、以及沙箱 `connector_transport.py` 的新服务优先探测）实现灰度切流。一旦出现异常，只需将对应环境变量切回原值即可秒级回滚至桥路由。
2. **旧路由命中计量**：在 `scripts/dalton/researchdata_bridge.py` 的 `_handle_client`（`:25344`）与 `handle_tools_call`（`:13541`）按路由前缀与 `route` 子路由记录每日请求计数至 `~/Library/Logs/dalton-bridge-route-hits.jsonl`。
3. **下线准入标准**：某组路由切换至新连接器或后端模块后，在 Everflow 生产环境连续观察 **14 个自然日**（覆盖完整两轮周报、美股/港股盘前盘后定时任务及周末维护任务），满足 `dalton-bridge-route-hits.jsonl` 中该路由业务请求计数为 **0**，且相关定时任务与对话错误率平稳，即可在下一批次 PR 中删除桥内对应处理函数。

---

## 4. 能力与开关

### 4.1 连接器与能力 ID 映射表

全面沿用 `src/dalton/capabilities.py:42-78` 现有的 `CapabilityId` 枚举，核心变更为**解除各独立子能力对 `CapabilityId.RESEARCHDATA_BRIDGE` 的 `depends_on` 硬依赖**（`src/dalton/capabilities.py:95-159`），使每一项能力均可按需独立开启：

| 能力 ID（`CapabilityId`） | 承载组件 / MCP 服务名 | 拆分后 `depends_on` | 平台约束 | `core` 默认值 | `everflow` 默认值 | 开启所需配置 |
|---|---|---|---|---|---|---|
| `alphaengine` | 插件 `dalton-alphaengine`（`dalton_alphaengine`，含 `transcript`） | `()`（独立顶层能力） | 仅 `sys.platform == "darwin"`（Linux 宿主解析为关闭并记录启动提示日志） | `False` | `True` | macOS 宿主 + `:8950`/`8951` 服务运行 + `DALTON_ALPHAENGINE_MCP_URL` |
| `guidepoint` | 插件 `dalton-guidepoint`（`dalton_guidepoint`） | `()` | 跨平台（Linux / macOS） | `False` | `True` | `GUIDEPOINT_CLIENT_ID`、`GUIDEPOINT_CLIENT_SECRET`、`GUIDEPOINT_REFRESH_TOKEN` |
| `company_wiki` | 插件 `dalton-company-wiki`（`dalton_company_wiki`）+ 后端 `wiki_store` | `()` | 跨平台（Linux / macOS） | `False` | `True` | `DALTON_WIKI_ROOT` 数据卷 + 嵌入 API 密钥 |
| `sales_notes` | 插件 `dalton-sales-notes`（`dalton_sales_notes`）+ 后端 `sales_notes_store` | `()` | 跨平台（Linux / macOS） | `False` | `True` | `DALTON_SALES_NOTES_ROOT` 数据卷 + 嵌入 API 密钥 |
| `tmtb` | 插件 `dalton-tmtb`（`dalton_tmtb`） | `()` | 跨平台（Linux / macOS） | `False` | `True` | `DALTON_TMTB_DB_PATH` SQLite 文件 |
| `funda_ai` | 插件 `dalton-funda-ai`（`dalton_funda_ai`） | `()` | 跨平台（Linux / macOS） | `False` | `True` | `DALTON_FUNDA_AI_DB_PATH` SQLite 文件 |
| `transcription` | 独立服务 `dalton-transcription` | `()` | 跨平台（Linux / macOS） | `False` | `False`（按环境显式开启） | `DALTON_TRANSCRIPTION_SERVICE_URL` + ASR/对象存储密钥 |
| `conversation_archive` | 后端 `src/dalton/conversations/` + `src/dalton/embeddings/` | `()` | 跨平台（Linux / macOS） | `False` | `True` | 后端嵌入密钥（如 `GEMINI_API_KEY`） |
| `feishu_docs` | 后端 `src/dalton/feishu.py`（第 ③ 阶段接网关/OpenAPI） | `()` | 跨平台（Linux / macOS） | `False` | `True` | `FEISHU_APP_ID`、`FEISHU_APP_SECRET` |
| `researchdata_bridge` | 旧桥兼容包 `plugins/dalton_data/`（过渡期保留，全部迁完后退役） | `()` | 仅过渡期使用 | `False` | 过渡期 `True`，收官批改为 `False` | `DALTON_RESEARCHDATA_URL` |

同时在 `src/dalton/capabilities.py:603-637` 的 `MCP_SERVER_CAPABILITY_MAP` 与 `BUNDLED_PLUGIN_CAPABILITY_MAP` 中逐一登记新连接器名称与对应 `CapabilityId`，并通过 `src/ptc_agent/config/plugins.py:290-330` 的 `is_bundled_plugin_enabled_by_capability` 在发现阶段自动过滤。

### 4.2 能力关闭时各层的协同表现

沿用 `src/dalton/content_assembly.py` 与前端 `useCapabilities` 的现有多层门控机制，当某项连接器能力处于关闭态时，全栈各层自动保持一致：

1. **MCP 服务注册与重连层**：`filter_mcp_servers_by_capability`（`src/dalton/capabilities.py:678-690`）与 `filter_reconnectable_builtin_mcp`（`:709-728`）在启动时只保留已启用连接器，后台重连任务只针对已启用的远端连接器运行，沙箱只生成已启用连接器的 `tools/dalton_*.py` 包装模块。
2. **后端直连工具层**：`src/dalton/tools/__init__.py:189-275` 按 `has_capability` 装配工具——例如 `company_wiki` 关闭时，`ingest_company_wiki` 自动从 Agent 工具集省去；`sales_notes` 关闭时，`ingest_sales_note` 自动省去；两者都关闭时，统一入口 `ingest_research` 一并省去。
3. **技能目录与 Markdown 指令层**：`src/dalton/content_assembly.py:72-125`（`SKILL_CAPABILITY_REQUIREMENTS`）与 `:220-340`（`strip_disabled_capability_blocks`）在技能装载时裁剪 `<!-- DALTON_CAP:<cap> -->...<!-- /DALTON_CAP -->` 片段（支持 `<!-- DALTON_CAP_ELSE -->` 替代文案）。例如仅开启 `company_wiki` 与 `dalton_sec` 的核心安装实例，其 `dalton-bullbear` 技能提示词自动展示内部 Wiki 与 SEC 检索步骤，并省去 AlphaEngine、Guidepoint、TMTB、FUNDA AI 相关段落。
4. **系统提示词与 `<data_map>` 层**：`src/dalton/data_map/builder.py:273-820` 结合 `src/dalton/data_map/sources.yaml` 仅渲染当前已启用能力的数据源行与路由决策规则；关闭的能力条目在 `<data_map>` 与系统提示词中完全隐去，节约上下文 Token 并消除模型幻觉调用。
5. **前端设置页与能力发现层**：后端 `GET /api/v1/dalton/capabilities` 输出实时生效的布尔能力字典；前端 `web/src/hooks/useCapabilities.ts:1-177` 与 `web/src/pages/Settings/sections.ts:1-360` 仅展示已启用能力关联的设置卡片（如飞书绑定、自动入库开关等）。
6. **帮助中心文档层**：构建与运行时通过 `content_assembly` 相同的能力标记裁剪 `docs-site/` 与 `langalpha-doc` 技能参考文档（如 `plugins/langalpha_service/skills/langalpha-doc/references/settings-map.md:27`），确保用户看到的帮助文档与当前实例实际开启的连接器一致。

---

## 5. 运行与部署

### 5.1 连接器进程模型划分

| 连接器 / 服务 | 运行进程位置 | 生命周期与资源隔离特征 |
|---|---|---|
| `dalton_guidepoint`、`dalton_company_wiki`、`dalton_sales_notes`、`dalton_tmtb`、`dalton_funda_ai`、`dalton_cnhk` | **沙箱容器内 `stdio` MCP 子进程**（由沙箱 supervisor 按需拉起，与 `plugins/dalton_sec/mcp.json:4-15` 完全一致） | 随工作区沙箱生命周期启停；本地库连接器通过 Docker bind-mount `:ro` 只读挂载对应 SQLite 目录，零常驻宿主端口开销 |
| `dalton_alphaengine` | **macOS 宿主机常驻 HTTP 服务**（`127.0.0.1:8951` `dalton_alphaengine_server.py` + `127.0.0.1:8950` `server.mjs`） | 跨工作区与跨线程共享同一个 SQLite WAL 搜索缓存库、并发单飞锁、当日配额账本，并直接调用 macOS 钥匙串与桌面端 `/Applications/AlphaEngine.app` |
| `src/dalton/embeddings/`、`src/dalton/wiki_store/`、`src/dalton/sales_notes_store/` | **后端 `backend` 容器进程内异步模块** | 直接复用后端进程事件循环、连接池与配置体系，省去跨进程序列化开销 |
| `dalton-transcription` | **独立 Docker 容器服务**（`transcription:8965`，Everflow 亦可选择宿主 `launchd` 运行） | 隔离 `ffmpeg` 音视频转码的 CPU/磁盘峰值与长耗时火山引擎轮询任务，重启后端进程时正在执行的转写任务持续推进 |

### 5.2 Everflow 生产（macOS 宿主 + Colima/Docker）与核心安装（Linux Docker）部署对比

1. **Everflow 生产环境（macOS 宿主 + Colima 容器栈，`docker-compose.dalton.yml`）**：
   - **私有插件挂载**：将私有连接器仓库目录（如 `~/Projects/dalton-connectors/plugins/*`）挂载或同步至容器与沙箱可见的插件目录，由 `bundled_mcp_servers()`（`src/ptc_agent/config/plugins.py:278`）统一加载。
   - **数据卷挂载**：在 `docker-compose.dalton.yml` 与沙箱 Docker 配置中，将 `<DALTON_RESEARCH_HOME>/wiki`、`skills/market-digest`、`state/sales-notes`、`state/funda-ai` 以只读模式挂载至沙箱用于检索，以读写模式挂载至 `backend` 容器用于 `ingest_company_wiki` 与 `ingest_sales_note` 入库。
   - **宿主网络连通（容器访问宿主机 `dalton_alphaengine`）**：
     - 后端容器（用于启动探测与 `<data_map>` 健康检查）：通过 `http://host.docker.internal:8951/mcp` 访问宿主机上的 `dalton_alphaengine` 服务（与当前 `docker-compose.dalton.yml` 访问 `:8960` 的网络路径一致）。
     - 沙箱容器（位于隔离网络 `sandbox-net`）：沿用现行双模可达方案——当沙箱配置 `host.docker.internal` 路由（Everflow 单租户受信沙箱）时直连 `http://host.docker.internal:8951/mcp` 并由 `src/ptc_agent/core/sandbox/mcp_client_runtime.py:2177-2198` 注入每次调用的 `X-Dalton-Caller-Token`；在严格隔离的 `sandbox-net` 下，则通过后端出口中继 `EGRESS_RELAY_BASE_URL=http://backend:8000`（`src/server/services/egress/reachability.py:58-68` 与 `src/server/app/egress_relay.py:12`）由后端代签并转发至宿主机 `:8951`，在保持沙箱网桥隔离的同时完成安全转发。

2. **独立核心安装环境（Linux Docker，`docker-compose.yml` + `docker-compose.core.yml`）**：
   - **默认零外部依赖开箱即用**：`DALTON_PROFILE=core`，仅内置公共连接器（`dalton_sec`、`yfinance`、`macro`、`scrape` 等）启用；启动时 `alphaengine` 因非 macOS 平台自动保持关闭。
   - **按需渐进增强**：
     - 开启对话归档语义检索：在 `.env` 填入 `GEMINI_API_KEY`（或嵌入模型密钥）并设置 `DALTON_CAP_CONVERSATION_ARCHIVE=true`，后端进程内 `src/dalton/embeddings/` 直接生效。
     - 开启内部公司 Wiki：挂载本地目录至 `/var/lib/dalton/wiki`，设置 `DALTON_CAP_COMPANY_WIKI=true`，立即可用 `ingest_company_wiki` 沉淀文档并通过 `dalton_company_wiki` 混合检索。
     - 开启商业连接器（如 Guidepoint）或独立转写服务：放入对应插件包或启用 `--profile transcription` 容器组，设置对应 `DALTON_CAP_*=true` 与 API 凭证即可独立运行。

### 5.3 macOS `launchd` 服务增减清单（Everflow 生产）

| `launchd` 服务标识（Plist 模板位置） | 拆分后动作 | 说明 |
|---|---|---|
| `com.dalton.alphaengine-mcp`（`scripts/dalton/launchd/com.dalton.alphaengine-mcp.plist.template`） | **保留** | 继续在宿主机 `127.0.0.1:8950` 运行 `server.mjs`，负责与 `/Applications/AlphaEngine.app` 和钥匙串交互 |
| `com.dalton.alphaengine-desktop-watchdog`（`scripts/dalton/launchd/com.dalton.alphaengine-desktop-watchdog.plist.template`） | **保留** | 继续守护 macOS AlphaEngine 桌面客户端存活 |
| `com.dalton.alphaengine-connector`（新增，置于私有仓库或 `scripts/dalton/ops/prod/launchd/`） | **新增** | 在宿主机 `127.0.0.1:8951` 运行轻量 `dalton_alphaengine_server.py`（仅承载 AlphaEngine/Transcript MCP 协议、SQLite v4 缓存、熔断与配额账本） |
| `com.dalton.guidepoint-mcp-proxy`（`scripts/dalton/launchd/com.dalton.guidepoint-mcp-proxy.plist.template`） | **下线移除** | Auth0 令牌刷新与远端请求迁入 `dalton-guidepoint` `stdio` 插件后，省去宿主机 `:8943` 代理守护进程 |
| `com.dalton.ops-status-exporter`（新增，置于 `scripts/dalton/ops/prod/launchd/`） | **新增（轻量定时）** | `StartInterval=300` 秒运行一次 `export_ops_status.py`，刷新 `var/dalton/ops_jobs_status.json`，零常驻内存与端口占用 |
| `com.dalton.host-task-daemon`（新增，置于 `scripts/dalton/ops/prod/launchd/`） | **新增（按需）** | 承接 `T1`/`T29` 等 7 个 `runner: "host"` 任务与摘要准备接口，替代原桥中的 `/internal/*` |
| `com.dalton.researchdata-bridge`（`scripts/dalton/launchd/com.dalton.researchdata-bridge.plist.template`） | **收官下线移除** | 第 5 批及第 ③ 阶段渠道网关切流完成后正式卸载，回收 `:8960` 端口 |
| 其余生产运维服务（`com.dalton.colima`、`com.dalton.pg-backup`、`com.dalton.backend-watchdog`、`com.dalton.portwatch`、`com.dalton.channel-gateway` 等） | **保持现状** | 位于 `scripts/dalton/ops/prod/launchd/` 与 `scripts/dalton/launchd/`，职责清晰，维持现有运行方式 |

---

## 6. 分批计划

按影响面从收敛的后端内部接口到高频研究连接器，由低风险向高风险划分为 **5 个独立批次**。每个批次均可独立合并、独立部署并通过单一环境变量开关秒级回滚。

### 数据库迁移编号约定

核查 `src/dalton/migrations/versions/` 现有迁移链：`d045 -> d047 (local_auth_and_roles) -> d048 (invite_admin_tier) -> d046 (scoped_data_map_cache)`，当前实际迁移头为 **`d046`**（`src/dalton/migrations/versions/d046_scoped_data_map_cache.py:23-24`）。鉴于第 ③ 阶段前的微信多账号改造已预占 **`d049`**，本设计所有批次优先复用现有 `user_mcp_servers` / `workspace_mcp_servers` 与文件状态存储；若第 2 批转写服务或后续批次确需新增持久化表结构，迁移版本号统一从 **`d050`** 起编（若在微信多账号前合入则 `down_revision = "d046"`，合入后则接 `down_revision = "d049"`）。

### 第 1 批：能力解耦、`/embed` 收归后端与 `/jobs/ops` 归入生产运维（极低风险）

- **范围**：
  1. 新增后端嵌入模块 `src/dalton/embeddings/`，迁移 `src/dalton/conversations/embedder.py:75-207` 默认走后端直连嵌入 API（保留 `DALTON_EMBED_VIA_BRIDGE=true` 回退开关），解除 `:150` 对 `RESEARCHDATA_BRIDGE` 的检查。
  2. 新增 `scripts/dalton/ops/prod/export_ops_status.py` 与对应 plist，`src/dalton/autonomy/system_jobs.py:232-315` 改为优先读取 `var/dalton/ops_jobs_status.json`。
  3. 在 `src/dalton/capabilities.py:95-159` 中移除 8 个子能力对 `CapabilityId.RESEARCHDATA_BRIDGE` 的 `depends_on` 硬绑定，并为 `CapabilityId.ALPHAENGINE` 增加 `sys.platform == "darwin"` 平台守卫；更新 `src/dalton/provenance/declared_sources.py:13` 预注册新连接器服务名白名单。
- **数据库迁移**：无（复用现有表结构）。
- **验收条件**：核心安装在无桥环境下开启 `conversation_archive` + `GEMINI_API_KEY` 可完成对话向量归档与语义检索；Everflow 生产系统任务页正常展示 `pg-backup` 与 `backend-watchdog` 状态；桥上 `/embed` 与 `/jobs/ops` 连续 14 天命中数为 0。
- **回滚方式**：设置 `DALTON_EMBED_VIA_BRIDGE=true` 与 `DALTON_OPS_JOBS_VIA_BRIDGE=true` 即可切回原桥路由。

### 第 2 批：独立转写连接器服务 `dalton-transcription` 拆分（低风险）

- **范围**：
  1. 将 `scripts/dalton/asr_jobs.py` 与 `scripts/dalton/seed_asr_client.py` 打包为独立服务 `dalton-transcription`（在 `docker-compose.core.yml` 增加可选 `transcription` profile，在 `docker-compose.dalton.yml` 配置独立服务或宿主守护）。
  2. 更新 `src/dalton/services/transcriptions.py:461-584`，优先读取 `DALTON_TRANSCRIPTION_SERVICE_URL` 与 `DALTON_TRANSCRIPTION_TOKEN`；缺省时兼容回退至旧桥 URL。
- **数据库迁移**：无（转写任务元数据继续使用服务卷持久化目录，后端对账循环复用现有表；如需将作业元数据迁入 Postgres 则使用 `d050`）。
- **验收条件**：在独立核心环境启动 `transcription` 服务并开启 `DALTON_CAP_TRANSCRIPTION=true`，通过 `prepare_transcription` 与 `dalton_asr.py` 跑通音视频上传、状态轮询、重试/取消与逐字稿落盘全链路；Everflow 生产切流观察 14 天桥上 `/transcriptions*` 命中为 0。
- **回滚方式**：清空 `DALTON_TRANSCRIPTION_SERVICE_URL` 即自动回退至宿主桥 `:8960/transcriptions`。

### 第 3 批：跨平台内部研究连接器与入库模块拆分（`guidepoint`、`company-wiki`、`sales-notes`、`tmtb`、`funda-ai`）（中等风险）

- **范围**：
  1. 按 Agent Plugins 1.0.0 规范交付 `dalton-guidepoint`、`dalton-company-wiki`、`dalton-sales-notes`、`dalton-tmtb`、`dalton-funda-ai` 五个 `stdio` MCP 连接器包（通用 Wiki 连接器入公开仓库，其余商业/内部连接器入私有仓库），在 `MCP_SERVER_CAPABILITY_MAP` 与 `BUNDLED_PLUGIN_CAPABILITY_MAP` 完成绑定。
  2. 新增后端 `src/dalton/wiki_store/ingest.py` 与 `src/dalton/sales_notes_store/ingest.py`，切换 `wiki_tools.py` 与 `ingest_tools.py` 走后端直写入库。
  3. 上线共享传输适配层 `plugins/dalton/skills/_shared/connector_transport.py` 与 `federated_search.py`，完成 3.2 节中相关技能脚本与 `sources.yaml` / `health.py` 迁移。
- **数据库迁移**：无（插件自动扇出写入现有的 `user_mcp_servers` 与 `workspace_mcp_servers` 表，见 `src/server/services/plugins/server_fanout.py:1-60`）。
- **验收条件**：核心安装在 Linux 下单独开启 `company_wiki`（或挂载私有 `guidepoint` 插件）时，沙箱自动生成 `tools.dalton_company_wiki` 且 `ingest_company_wiki` 读写闭环通过；Everflow 生产下线 `com.dalton.guidepoint-mcp-proxy` 守护进程，五条旧路由在桥上连续 14 天零命中。
- **回滚方式**：关闭新插件挂载开关并将 `DALTON_INGEST_VIA_BRIDGE=true` 置真，`connector_transport.py` 自动回退至 `tools.researchdata`。

### 第 4 批：macOS 专属 `dalton-alphaengine` 连接器拆分与技能脚本全量切流（中高风险）

- **范围**：
  1. 在私有仓库交付 `dalton-alphaengine` 插件与宿主机 `dalton_alphaengine_server.py`（监听 `127.0.0.1:8951`），完整迁入 `alphaengine_search_cache.py`（SQLite v4 缓存、单飞、熔断器、30 分钟同公司提示、`X-Dalton-Caller-Token` 验签与配额日志），同时覆盖 `alphaengine` 与 `transcript` 检索。
  2. 更新全部技能 Markdown 的 `<!-- DALTON_CAP:... -->` 标签，将笼统的 `researchdata_bridge` 替换为细粒度来源能力标签；美股 SEC 调用在 `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:932-943` 等处统一收口至 `dalton_sec`。
- **数据库迁移**：无。
- **验收条件**：Everflow Mac 生产跑通 `dalton-thesis-delta`、`dalton-bullbear-tracker`、`dalton-sector-delta`、`dalton-earnings-flash` 实盘回归，缓存命中率、当日读文额度统计及 `prov:` 引用展开与切流前完全一致；Linux 核心安装启动日志确认 `alphaengine` 自动静默关闭且零报错。
- **回滚方式**：在 `agent_config.dalton.yaml` / `.env` 关闭 `dalton_alphaengine` URL，技能适配层即时回退走 `:8960` 旧桥的 `alphaengine` 路由。

### 第 5 批：宿主任务守护剥离、第 ③ 阶段网关交接与 `researchdata_bridge.py` 退役（收官批）

- **范围**：
  1. 将 `/internal/{market-digest,ai-finance,data-tasks}/*` 迁入 `scripts/dalton/ops/prod/host_task_daemon.py`（承接 `fund_automations` 与 `data_jobs` 的 7 个宿主任务），`cn-hk` 迁入可选插件 `dalton-cnhk`。
  2. 配合第 ③ 阶段完成 `/notify`、`/feishu/*`、`discord-history` 向 `integrations/dalton-channel-gateway/` 的正式切流（开启 `DALTON_NOTIFY_VIA_GATEWAY=true`）。
  3. 确认 `dalton-bridge-route-hits.jsonl` 全路由连续 14 天零命中后，卸载 `com.dalton.researchdata-bridge` launchd 服务，移除 `plugins/dalton_data/` 与 `scripts/dalton/researchdata_bridge.py`，将 `CapabilityId.RESEARCHDATA_BRIDGE` 标记为历史兼容别名并清理遗留配置。
- **数据库迁移**：可选清理迁移 `d050`（或 `d051`，视前序占用情况），幂等清理 `user_mcp_servers` / `workspace_mcp_servers` 中残留的 `researchdata` 墓碑行。
- **验收条件**：Everflow 生产在完全停止 `:8960` 进程的情况下稳定运行全部日常对话、研究技能、数据采集任务与飞书通知；`test_core_capabilities_gate.py` 与 `test_everflow_profile_parity.py` 全部绿灯。
- **回滚方式**：归档删除前保留整包启动脚本 1 个迭代周期，必要时重新加载 `com.dalton.researchdata-bridge.plist` 恢复旧进程。

---

## 7. 测试

### 7.1 单元测试（每次提交必跑，`uv run pytest tests/unit/`）

| 批次 | 新增与更新的单元测试覆盖点 |
|---|---|
| 第 1 批 | 1. 更新 `tests/unit/dalton/test_core_capabilities_gate.py:244-540`（`TestCapabilityRegistry`）：验证 `company_wiki`、`guidepoint`、`transcription` 等在 `researchdata_bridge=False` 时可单独解析为开启态；验证 `monkeypatch.setattr(sys, "platform", "linux")` 下 `alphaengine` 恒为关闭态。<br>2. 扩展 `tests/unit/dalton/test_embedder_retry.py:1-105` 并新增后端嵌入客户端单测：用 `httpx.MockTransport` 测试批量 100 条分片、瞬态 429/503 重试、维度校验及回退逻辑。<br>3. 新增运维状态文件解析单测：验证 `src/dalton/autonomy/system_jobs.py:261-315` 对 `ops_jobs_status.json` 正常、过期、缺失三种状态的解析。<br>4. 更新 `tests/unit/dalton/test_declared_sources.py:1-120`：覆盖全部新连接器服务名的 `_sources` 展开。 |
| 第 2 批 | 扩展 `tests/unit/dalton/test_transcriptions.py:1-120` 与 `tests/unit/dalton/test_transcription_master_switch.py:1-100`：契约测试 `src/dalton/services/transcriptions.py:461-584` 对独立转写服务 6 个端点的请求头、流式下载 `/result/raw`、独立开关与错误码映射。 |
| 第 3 批 | 1. 更新 `tests/unit/dalton/test_core_capabilities_gate.py:2144-2378`（`TestBundledPluginsAndMcpRegistrationGate` 与 `TestBatch3BackendTextAndSkillGates`）：校验新连接器注册表映射与技能 Markdown 按单源能力裁剪结果。<br>2. 扩展 `tests/unit/dalton/test_wiki_ingest.py:1-120` 并新增后端直写入库测试：在 `tmp_path` SQLite 库上验证幂等写入、并发文件锁与混合检索回读。<br>3. 新增沙箱技能适配层单测：验证 `connector_transport.py` 在“新连接器就绪 / 仅旧桥就绪 / 能力关闭”三种状态下的路由选择。 |
| 第 4 批 | 复用并扩展 `tests/unit/dalton/test_alphaengine_search_cache.py:1-120`（验证 SQLite v4 缓存命中、`fresh=True` 绕过、熔断器打开/半开恢复、`X-Dalton-Caller-Token` 验签拒绝伪造令牌）；更新 `tests/unit/dalton/test_everflow_profile_parity.py:142-281` 确保 Everflow 形态下 `<data_map>` 与系统提示词快照对齐。 |
| 第 5 批 | 验证清理 `researchdata` 遗留配置后的迁移幂等性及全量门控回归。 |

### 7.2 GitHub Actions 从零安装集成测试（`.github/workflows/standalone-core-e2e.yml`）

在零密钥 Ubuntu Runner（`.github/workflows/standalone-core-e2e.yml:52-176`）导出纯净发行目录 `/tmp/dalton-core-dist` 后，扩展 `tests/standalone/checks/` 验收脚本：

1. **打包隔离扫描（`package_release.sh`）**：确认商业私有连接器代码零混入导出目录，同时公开连接器接口（`dalton_sec`、`dalton-company-wiki`、嵌入客户端、转写客户端）完整存在于导出树。
2. **后端嵌入与归档闭环（扩展 `tests/standalone/checks/b2_backend_capabilities.py`）**：将 `GEMINI_API_KEY` / 嵌入端点指向 `tests/fakes/fake_openai_server.py` 扩展的 `/v1/embeddings` 假端点，开启 `DALTON_CAP_CONVERSATION_ARCHIVE=true`，验证纯容器栈环境下完成对话嵌入写入与混合检索。
3. **独立 Wiki 读写闭环（扩展 `b2_backend_capabilities.py` 与 `b3_skills_prompts_ui.py`）**：单独开启 `DALTON_CAP_COMPANY_WIKI=true`，验证：
   - `<data_map>` 仅出现 `company-wiki` 与公共源，其余商业源保持隐藏；
   - 调用 `ingest_company_wiki` 落盘测试词条后，沙箱 `ExecuteCode` 通过 `from tools.dalton_company_wiki import search, read_document` 成功检索并回读全文，同时生成合法 `prov:` 溯源记录；
   - `alphaengine` 在 `ubuntu-latest` 上即使显式设置 `DALTON_CAP_ALPHAENGINE=true` 仍被平台守卫安全拦截关闭。
4. **独立转写服务假桩验证**：在 `tests/fakes/` 下提供 50 行轻量 `fake_transcription_server.py`，验证开启 `DALTON_CAP_TRANSCRIPTION=true` 时后端对账循环与 `/api/v1/dalton/transcriptions` 接口端到端贯通。

### 7.3 仅能在 Everflow 生产 Mac 上验证的项目

以下项目强依赖 macOS 图形桌面会话、系统钥匙串及真实机构账号，统一在 Everflow 生产 Mac 上通过灰度验收脚本执行：

1. **AlphaEngine 桌面态与钥匙串链路**：验证 `com.dalton.alphaengine-connector`（`:8951`）-> `com.dalton.alphaengine-mcp`（`:8950`）-> macOS `security` CLI 读取 `dalton-alphaengine` 钥匙串及 `/Applications/AlphaEngine.app` 登录态，完成真实 `search_library`、`get_document`、`search_transcripts` 与录制状态查询。
2. **Colima 容器跨网桥调用与 Caller Identity 验签**：从运行在 Colima Docker `sandbox-net` 内的真实沙箱容器发起 `tools.dalton_alphaengine` 调用，核验 `~/Library/Logs/alphaengine_usage.jsonl` 中正确记录真实的 `user_id`、`workspace_id`、`thread_id` 及缓存命中标记。
3. **真实商业上游联调**：验证 `dalton-guidepoint` `stdio` 子进程在沙箱内使用真实 Auth0 凭据完成令牌刷新并返回问答摘录；验证 `dalton-transcription` 连接火山引擎北京区 TOS 与豆包 Seed-ASR 2.0 完成真实长音频说话人分离转写。
4. **宿主专属任务回归**：验证 `T1`/`T29`（SSH 触发 Windows BBG 刷新）等 `runner: "host"` 任务与 `com.dalton.pg-backup` 运维状态快照在切流后按既定时区调度正常执行。

---

## 8. 风险与待用户决定的事项

### 8.1 主要技术风险与应对措施

| 风险点 | 影响说明 | 缓解与应对措施 |
|---|---|---|
| 沙箱内多个 `stdio` MCP 子进程的内存叠加 | 将 5 个研究源全拆为独立 `stdio` 子进程后，Everflow 全开工作区沙箱会多启动数个 Python 进程 | 沙箱 supervisor 采用惰性按需启动与空闲回收机制；内部 SQLite 连接器共享轻量标准库 `sqlite3`（单进程常驻内存 < 35 MB），重负载向量计算统一走外部 Embedding API |
| 技能提示词裁剪颗粒度变细后的组合膨胀 | 单篇 `SKILL.md` 同时引用多个研究源时，独立开关产生多种启用子集组合 | 在 `content_assembly.py` 已有 `DALTON_CAP:a|b|c` 或语法基础上，章节级使用或表达式（任一内部源开启即保留章节框架），具体源列表行按各自单能力标签逐行裁剪 |
| `company-wiki` 与 `sales-notes` 读写并发竞争 SQLite 锁 | 后端进程写入 `vectors.db` 的同时，沙箱 `stdio` 连接器挂载同一文件执行只读检索 | SQLite 统一开启 `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`；沙箱挂载卷使用 `:ro` 或连接串 `mode=ro`，写入仅由后端单点串行落盘 |
| 过渡期环境变量增多导致排障复杂 | 5 个批次演进期间存在新旧地址并存 | 在 `GET /api/v1/dalton/capabilities` 与启动日志统一输出每个连接器当前绑定的传输通道（`standalone_connector` vs `legacy_bridge`），一目了然 |

### 8.2 待用户拍板的事项（含推荐选项）

1. **私有商业连接器仓库向生产与客户环境的交付方式**
   - **背景**：公开仓库只保留能力开关与接口，商业连接器（`dalton-alphaengine`、`dalton-guidepoint`、`dalton-sales-notes`、`dalton-tmtb`、`dalton-funda-ai`）存放在私有仓库。后端与沙箱发现插件目前扫描 `plugins/` 目录（`src/ptc_agent/config/plugins.py:60-115`）。
   - **选项 A（推荐）**：在 `src/ptc_agent/config/plugins.py` 支持可选环境变量 `DALTON_EXTRA_PLUGINS_DIRS`（冒号分隔目录列表），Everflow 生产通过 `docker-compose.dalton.yml` 将宿主机私有仓库 `~/Projects/dalton-connectors/plugins` 直接挂载进容器与沙箱，主仓库与私有仓库各自独立维护版本，零文件拷贝。
   - **选项 B**：部署时通过构建脚本将私有仓库插件复制到 `plugins/` 目录下（由 `.gitignore` 忽略）。

2. **核心安装下 `/embed` 的向量维度与提供商标准化策略**
   - **背景**：Everflow 现有 `conversations` 与 `wiki/vectors.db` 均采用 Gemini Embedding 3072 维向量；独立核心安装用户可能只配置 OpenAI 兼容密钥（如 `text-embedding-3-small` 1536 维或 `text-embedding-3-large` 3072 维）。
   - **选项 A（推荐）**：后端 `src/dalton/embeddings/` 同时内置 Gemini 与 OpenAI 兼容两种协议适配器，并在 `dalton.yaml` / `.env` 中支持配置 `EMBEDDING_DIMENSIONS`（Everflow 固定 3072 维无缝衔接历史库；核心安装初始化时按所选模型维度建表/建库，并在 `vectors.db` 元数据表记录模型名与维度以防混用）。
   - **选项 B**：核心安装强制要求配置支持 3072 维输出的嵌入模型（Gemini 或 `text-embedding-3-large`）。

3. **跨库初筛路由 `research(route="all")` 的最终去留**
   - **背景**：原桥提供 `route="all"` 对 6 个研究源做服务端并行扇出（`scripts/dalton/researchdata_bridge.py:14042-14422`）。核查代码库可见，所有确定性技能脚本（`thesis-delta`、`bullbear-tracker`、`sector-delta` 等）均已自行按单源并发调用，仅交互式 `ExecuteCode` 偶有调用 `route="all"`。
   - **选项 A（推荐）**：采用沙箱进程内聚合方案，预置轻量 Python 辅助模块 `from research_helpers import search_all_sources`（自动并发调用当前工作区已启用的连接器包装函数），并在系统提示词中引导模型按需直调具体连接器或该辅助函数。
   - **选项 B**：直接下线 `all` 聚合入口，由模型在单次 `ExecuteCode` 脚本中显式使用 `ThreadPoolExecutor` 并发调用所需的 2–4 个连接器。

4. **原桥上非核心长尾子路由（`cn-hk`、`community`、`tmeet`、`gcal`、`market-regime`）的归宿**
   - **背景**：除 6 个核心研究源外，桥 `/mcp` 还挂有港股/A 股检索 `cn-hk`、社区抓取 `community`（`xueqiu`/`employee`/`last30days`/`x-timeline`）、腾讯会议 `tmeet`、日历 `gcal` 与本地文件 `market-regime`。
   - **选项 A（推荐）**：`cn-hk` 独立为可选连接器插件 `dalton-cnhk`（绑定新增或复用能力开关，方便研究港股/A 股的核心安装实例选用）；`tmeet` 与 `gcal` 合并为 Everflow 内部办公小插件 `dalton-office`（受 `fund_automations` 门控）；`x-timeline` 与 `macro` 直接收口至核心已有的内置 `x_api`、`scrape` 与 `macro` MCP 服务器；`market-regime` 由沙箱直接只读读取挂载的状态文件。
   - **选项 B**：仅将 `cn-hk` 拆为插件，其余 `tmeet`、`gcal`、`xueqiu`、`employee` 随第 5 批宿主守护脚本 `host_task_daemon.py` 保留在 Everflow 宿主机内部使用。
