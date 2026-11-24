# 研究数据桥按职责拆分为连接器（独立核心版本第 ② 阶段）

> 状态：第 2 轮修订定稿（2026-11-24）
> 前置文档：[standalone-core.md](./standalone-core.md)、[alphaengine-cache.md](./alphaengine-cache.md)、[data-jobs.md](./data-jobs.md)
> 关联代码：`scripts/dalton/researchdata_bridge.py`、`src/dalton/capabilities.py`、`src/dalton/content_assembly.py`、`plugins/dalton_sec/`、`plugins/dalton_data/`

## 0. 背景与既定架构决定

宿主机单进程服务 `scripts/dalton/researchdata_bridge.py`（共 26,636 行，默认监听 `127.0.0.1:8960`）目前集中承载内部研究检索、金融与宏观扩展路由、向量嵌入、语音转写、飞书通知与文档代理、宿主定时任务执行及生产运维状态巡检。在独立核心版本第 ① 阶段（`docs/dalton/features/standalone-core.md:54-92`）中，`alphaengine`、`guidepoint`、`company_wiki`、`sales_notes`、`tmtb`、`funda_ai`、`feishu_docs`、`transcription` 八项能力均通过 `depends_on=(CapabilityId.RESEARCHDATA_BRIDGE,)` 挂在总开关 `researchdata_bridge` 下（`src/dalton/capabilities.py:95-159`），部署侧只能整体启停整座桥。

本设计将研究数据桥按职责边界拆分为独立连接器、后端内建模块、独立转写服务与生产运维守护进程，遵循以下全部既定决定：

1. **Everflow 生产固定运行于 macOS 宿主，宿主平台由部署配置显式传入**：AlphaEngine 连接器仅面向 macOS 设计，继续复用 macOS 钥匙串（`security` CLI）与 `/Applications/AlphaEngine.app` 桌面登录态。新增部署配置 `DALTON_HOST_PLATFORM`（`macos` | `linux`），由宿主部署或初始化脚本写入 `.env`，后端 `src/dalton/capabilities.py` 读取该配置判定 `alphaengine` 可用性，与容器内 Python 的 `sys.platform` 完全解耦。
2. **商业数据与供应商凭据留在宿主机侧，沙箱仅持有连接器调用令牌**：内部研究检索（AlphaEngine、Guidepoint、销售纪要、TMTB、Funda AI、公司 Wiki、跨库初筛、办公与社区扩展等）统一由宿主机服务提供；商业 SQLite 库、原始文档目录与供应商密钥（如 Guidepoint Auth0 刷新令牌、Gemini 密钥、飞书密钥）全部保留在宿主机进程内，令牌刷新由宿主机单一持有方串行写盘。沙箱与后端沿用现有 `streamable-http` 直连通道（`host.docker.internal`，同 `plugins/dalton_data/mcp.json:4-10`、`src/ptc_agent/core/sandbox/providers/docker.py:774-783`、`scripts/dalton/bridge_subprocess_env.py:2383`）携带连接器令牌与每次请求签发的调用方身份头访问。
3. **连接器插件采用 Agent Plugins 1.0.0 规范，公开数据进公开插件，商业实现放私有仓库**：每个连接器独立为一个插件包（`plugin.json` + `mcp.json` + 技能文件），按各自 `CapabilityId` 独立开关。SEC 法定披露与公开宏观序列（FRED、美国财政部 TGA/债务）放入公开仓库的公开插件供核心安装直接使用；商业连接器插件声明与接口契约保留在公开仓库（或通过唯一私有插件目录 `DALTON_EXTRA_PLUGINS_DIR` 装配），宿主机商业服务端实现存放在私有仓库。
4. **跨库初筛 `research(route="all")` 保留在宿主机侧**：由宿主机研究服务完成跨库扇出检索、按文档 ID 前缀分派全文读取（`read`）以及 `sales-mail` 横跨销售纪要与 TMTB 的兼容语义。
5. **向量嵌入支持库级模型与维度锁定，转写独立为连接器服务**：`/embed` 收归后端嵌入模块，数据库记录嵌入模型名与向量维度元数据（Everflow 锁定 3072 维与既有任务类型，核心新装按所选模型维度建表建索引，强制保证单库单一维度与模型）；`/transcriptions*` 拆为独立转写服务，通过排他锁交接历史结果、进行中任务与用量账本。
6. **生产运维归 `scripts/dalton/ops/prod/`，渠道通知留待第 ③ 阶段平滑移交**：`/jobs/ops` 与宿主任务执行器归入 `scripts/dalton/ops/prod/`；`/notify`、`/feishu/*` 与 `discord-history` 在第 ③ 阶段渠道网关 `integrations/dalton-channel-gateway/` 就绪前继续保留在缩减后的桥进程上，使研究连接器拆分批次与第 ③ 阶段完全解耦。
7. **落地前冻结桥外耦合**：各批次合入前，现行桥上的维护仅在 `scripts/dalton/researchdata_bridge.py` 内部完成。

---

## 1. 现状清单

### 1.1 HTTP 顶层路由组清单

`scripts/dalton/researchdata_bridge.py` 在 `_handle_client`（`scripts/dalton/researchdata_bridge.py:25344-26167`）中分发全部 HTTP 请求。`/healthz` 免鉴权；`/mcp` 校验 `DALTON_BRIDGE_TOKEN`（或 `DALTON_BRIDGE_ADMIN_TOKEN`，见 `_is_valid_mcp_token` 于 `:25301-25313`）；其余全部管理与内部路由统一在 `:25622` 经 `_require_admin_token`（`:25316-25342`）校验 `DALTON_BRIDGE_ADMIN_TOKEN`。（注：下表引用的 `<DALTON_RESEARCH_CODE>`、`<DALTON_RESEARCH_HOME>`、`~/Library/`、`/Applications/AlphaEngine.app` 等宿主机外部路径取自桥配置常量，本机工作区外实际文件状态待核实。）

| 路由前缀与方法 | 代码入口 | 职责说明 | 调用方（后端模块 / 沙箱技能 / 工作流 / 运维脚本） | 依赖的本机资源 | 当前对应能力 |
|---|---|---|---|---|---|
| `GET /healthz` | `scripts/dalton/researchdata_bridge.py:25419-25453` | 返回服务版本、并发限流器状态、从 JSON 文件读取的 AlphaEngine 当日读文统计（`_read_usage_today` 于 `:10897-10922`）、搜索熔断器状态、`xreach` 与 `tmeet` CLI 可用性 | 运维与验收脚本：`scripts/dalton/check_bridge.sh:32-36`、`scripts/dalton/install_bridge.sh:51-90`、`scripts/dalton/export_review_window.sh:222`、`scripts/dalton/realrun/check_env.py:835`、`scripts/dalton/realrun/g7_bridge.py:44-110` | `cfg.usage_file`（JSON 当日读文计数）、内存限流与熔断状态、本机 `xreach`/`tmeet` 二进制 | `researchdata_bridge` |
| `POST /mcp`<br>`POST /mcp/<token>` | `scripts/dalton/researchdata_bridge.py:25456-25619`；分发入口 `handle_tools_call` 于 `:13541-15264` | Streamable HTTP JSON-RPC 端点，暴露 `catalog`、`research`、`finance`、`macro`、`community` 5 个工具。在 `:25555-25589` 校验带 `X-Dalton-Caller-Scope: per-request` 的 `X-Dalton-Caller-Token` 用于工作区/用户并发桶与自动化优先级归属；缺失或验签未通过时降级为 `_caller_verified: False` 的未签名调用处理 | 1. 沙箱 PTC 运行时：`plugins/dalton_data/mcp.json:4-10`、`src/ptc_agent/core/sandbox/mcp_client_runtime.py:2165-2220`<br>2. 地址注入与配置中心：`scripts/dalton/bridge_subprocess_env.py:2229-2427`、`src/dalton/settings.py:1090-1151,1419-1435`<br>3. 后端健康探针与数据地图：`src/dalton/data_map/health.py:173-235,790-1060`、`src/dalton/data_map/builder.py:287-355,488-503`<br>4. 后端自治探针：`src/dalton/autonomy/probes/cnhk.py:184-310`、`scripts/dalton/realrun/g4_autonomy.py:412-420`<br>5. 技能脚本与 JS 工作流（详列于 1.2 节与 3.2 节） | 按子路由分别访问本机 `:8950`、`:8943`、`<DALTON_RESEARCH_HOME>` SQLite 库与本机 CLI（详列于 1.2 节） | 总开关 `researchdata_bridge` + `BRIDGE_ROUTE_CAPABILITY_MAP`（`src/dalton/capabilities.py:775-801`） |
| `POST /embed` | `scripts/dalton/researchdata_bridge.py:25626-25642`；处理函数 `handle_embed` 于 `:15419-15489` | 调用 `company_wiki_cli.py` 子进程请求 Gemini Embedding API（默认 `gemini-embedding-2-preview`，3072 维），支持每次最多 100 条批量向量化与查询向量化 | 1. 后端对话归档嵌入器：`src/dalton/conversations/embedder.py:75-207`，供 `src/dalton/conversations/search.py:259` 检索调用<br>2. 归档重渲染校验：`scripts/dalton/bridge_subprocess_env.py:29,881,1948-1950` | `<DALTON_RESEARCH_CODE>/skills/company-wiki/scripts/company_wiki_cli.py`、宿主机 `GEMINI_API_KEY` | 代码内检查 `researchdata_bridge`（`src/dalton/conversations/embedder.py:150-151`），业务归属 `conversation_archive` |
| `GET /jobs/ops` | `scripts/dalton/researchdata_bridge.py:25645-25651`；处理函数 `handle_get_ops_jobs` 于 `:15766-15794` | 读取 macOS `launchctl` 与备份日志，汇总 `com.dalton.pg-backup` 与 `com.dalton.backend-watchdog` 状态 | 后端系统任务模块：`src/dalton/autonomy/system_jobs.py:232-315` | macOS `/bin/launchctl`、`~/Library/LaunchAgents/*.plist`、`~/Library/Logs/dalton-pg-backup.log`、`~/Backups/dalton/.watchdog_failures` | `researchdata_bridge`（`src/dalton/autonomy/system_jobs.py:241,265`） |
| `POST /notify` | `scripts/dalton/researchdata_bridge.py:25659-25675`；处理函数 `handle_notify` 于 `:15796-15983` | 经受控环境过滤（`scripts/dalton/bridge_subprocess_env.py:423-452`）调用 `feishu_send_card.py` 推送飞书卡片 | 后端通知分发器：`src/dalton/notify/dispatch.py:1985-2080,5148`（受 `DALTON_NOTIFY_VIA_GATEWAY` 控制，见 `src/dalton/settings.py:1473-1486`） | `<DALTON_RESEARCH_CODE>/scripts/feishu_send_card.py`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET` | `channel_feishu` + `researchdata_bridge` |
| `POST /wiki/ingest` | `scripts/dalton/researchdata_bridge.py:25679-25699`；处理函数 `handle_wiki_ingest` 于 `:17050-17850` | 校验 Markdown 载荷、幂等去重、加文件锁并调用 `company-wiki/run.sh` 写入 Wiki 文件、更新 `wiki/vectors.db` 及审计日志 | 1. 后端工具：`src/dalton/tools/wiki_tools.py:447-485,676`（`ingest_company_wiki`，注册于 `src/dalton/tools/__init__.py:370-380`）<br>2. 统一入库工具：`src/dalton/tools/ingest_tools.py:615,661-677`（`ingest_research`，注册于 `src/dalton/tools/__init__.py:393-404`）<br>3. 前端工作区自动入库开关：`web/src/pages/ChatAgent/components/dalton/AutoIngestToggle.tsx:26` | `<DALTON_RESEARCH_HOME>/wiki/`、`wiki/vectors.db`、`var/dalton/wiki_ingest_audit.jsonl`、`GEMINI_API_KEY` | `ingest_company_wiki` 挂 `company_wiki`；`ingest_research` 与 `AutoIngestToggle` 当前挂 `researchdata_bridge` |
| `POST /sales-notes/ingest` | `scripts/dalton/researchdata_bridge.py:25703-25723`；处理函数 `handle_sales_notes_ingest` 于 `:18052-18430` | 校验销售纪要载荷并调用 `market-digest/run.sh add-note` 写入清单文件与 `skills/market-digest/vectors.db` | 后端工具：`src/dalton/tools/ingest_tools.py:72-105,261,578-594`（`ingest_sales_note` 注册于 `src/dalton/tools/__init__.py:381-392`，及 `ingest_research`） | `<DALTON_RESEARCH_HOME>/state/sales-notes/manifest.jsonl`、`skills/market-digest/vectors.db`、`GEMINI_API_KEY` | `ingest_sales_note` 挂 `sales_notes`；`ingest_research` 挂 `researchdata_bridge` |
| `POST /feishu/*`（共 8 个子路径） | `scripts/dalton/researchdata_bridge.py:25728-25768`；处理函数 `:18469-19024`；`FeishuClient` 于 `:22528-23369` | 代理飞书开放平台接口：`doc/read`、`wiki/nodes`、`wiki/node`、`doc/create`、`doc/append`、`doc/grant`、`bot-info`、`chats` | 1. 飞书客户端：`src/dalton/feishu.py:106-167`（`:117-118` 强校验 `RESEARCHDATA_BRIDGE`），供 `src/dalton/tools/feishu_tools.py:69-81,236,346,373,401,521,684` 调用<br>2. 后端管理路由：`src/dalton/app/router.py:2526`（`/feishu/spaces`）、`:2563`（`/notify/feishu-bot`）<br>3. 群发现缓存：`src/dalton/notify/groups.py:28-56` | 宿主机 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、飞书公网 OpenAPI | 工具注册挂 `feishu_docs` / `channel_feishu`，底层 `post_bridge_feishu` 挂 `researchdata_bridge` |
| `POST /transcriptions`<br>`GET /transcriptions/{id}`<br>`GET /transcriptions/{id}/result`<br>`GET /transcriptions/{id}/result/raw`<br>`POST /transcriptions/{id}/retry`<br>`POST /transcriptions/{id}/cancel` | `scripts/dalton/researchdata_bridge.py:25773-25890`；处理函数 `:19030-19113`；启动恢复 `:26191-26194`；引擎 `scripts/dalton/asr_jobs.py:2305-2345` 与 `scripts/dalton/seed_asr_client.py:1-160` | 音视频 `ffprobe`/`ffmpeg` 转码切分、火山引擎 TOS 上传与豆包 Seed-ASR 2.0 异步转写、未完成任务启动恢复及 `asr_usage.jsonl` 计量 | 1. 后端转写服务：`src/dalton/services/transcriptions.py:461-584,2038-2075`（`:465-466` 校验 `RESEARCHDATA_BRIDGE`），支撑路由 `src/dalton/app/transcriptions.py:100`、工具 `src/dalton/tools/transcription_tools.py:410`（注册于 `src/dalton/tools/__init__.py:405-410`）及对账循环 `src/server/app/setup.py:909-919`<br>2. 沙箱 CLI 经后端 API 调用：`plugins/dalton/skills/dalton-transcript-polish/scripts/dalton_asr.py:49,382-588` | 本机 `ffmpeg`/`ffprobe`、`var/dalton/asr_jobs/`（含状态文件、原始结果与 `asr_usage.jsonl`）、`VOLC_TOS_*` 与 `SEED_ASR_*` 密钥 | `transcription`（同时受 `src/dalton/capabilities.py:425-433` 后端转写存储配置约束） |
| `POST /internal/market-digest/{prepare,read,output/get,output/save,output/finalize}` | `scripts/dalton/researchdata_bridge.py:25894-25954,26082-26112`；处理函数 `:19770-19974` + `scripts/dalton/market_digest_access.py` | 每日市场摘要批次材料准备、分块读取、输出草稿保存与定稿 | 后端工具：`src/dalton/tools/market_digest_tools.py:42-43,286-289`、`src/dalton/tools/market_digest_output_tools.py:471-480`（注册于 `src/dalton/tools/__init__.py:94-113`） | `<DALTON_RESEARCH_HOME>/skills/market-digest/` 目录、邮件/电报拉取环境 | `fund_automations` |
| `POST /internal/ai-finance/{prepare,read,signals,report}` | `scripts/dalton/researchdata_bridge.py:25958-26078`；处理函数 `:19531-19769` + `scripts/dalton/ai_finance_access.py` | AI Finance 日报材料准备、信号库读写与报告快照提交 | 后端工具：`src/dalton/tools/ai_finance_inputs_tools.py:309`、`src/dalton/tools/ai_finance_signals_tools.py:706`、`src/dalton/tools/ai_finance_report_tools.py:779`（注册于 `src/dalton/tools/__init__.py:114-143`） | `DALTON_AI_FINANCE_CONFIG` 指向的宿主机状态目录 | `fund_automations` |
| `POST /internal/data-tasks/{host-run,host-kill}` | `scripts/dalton/researchdata_bridge.py:26116-26141`；状态恢复与执行 `:19168-19530,19975-20342`；启动恢复 `:26196-26199` | 受控执行 `src/dalton/data_jobs/manifest.yaml` 中 7 个 `runner: "host"` 任务（`T1` `:5-26`、`T29` `:335-356`、`T58` `:549-577`、`T34` `:871-895`、`T60` `:897-936`、`T61` `:939-964`、`T87` `:1045-1070`），维护 `generation`、进程组 `pgid`+`lstart`、`fence_lost` 标记、命令白名单与按任务凭据注入 | 后端宿主任务调度器：`src/dalton/data_jobs/host_runner.py:23-29,108-181` | `var/dalton/openclaw-data`、`.dalton-host-runner-state.json`、SSH Windows BBG 跳板机、`~/.local/share/everflow-tools/venvs/` | `data_jobs`（`src/dalton/capabilities.py:199-205`） |

此外，宿主机部署与迁移脚本直接管理桥生命周期：`scripts/dalton/ops/prod/deploy_main.sh:633,658,662,915-924` 在生产发布与回退时重启和校验 `com.dalton.researchdata-bridge`；`scripts/dalton/ops/research_runtime_migrate.sh:806-812` 调用 `researchdata_bridge.py --check-runtime` 校验数据目录完整性。

### 1.2 `/mcp` 内部工具与全部子路由清单

`/mcp` 的合法子路由集合定义在 `TOOL_VALID_ROUTES`（`scripts/dalton/researchdata_bridge.py:3212-3230`），参数与返回契约定义在 `ROUTE_CONTRACT_SPECS`（`:2219-3210`）及 `scripts/dalton/research_runtime/scripts/data_gateway.py:115-226`。

| MCP 工具 | `route` 子路由 | 现有操作集合（`operations`） | 代码定义位置 | 主要调用方（精确位置） | 本机资源与外部依赖 | 当前能力登记状态 |
|---|---|---|---|---|---|---|
| `catalog` | 可选按 `route` 过滤 | 自省返回路由参数契约 | `scripts/dalton/researchdata_bridge.py:2160-2218,13580-13650` | `plugins/dalton/agents/dalton-evidence.md:22`、`plugins/dalton/skills/dalton-bullbear/SKILL.md:29` | 内存契约字典 | `researchdata_bridge` |
| `research` | `all` | `search`、`read`、`list`、`get_document` | `:2460-2516,14042-14422` | 定时采集全文读取与联搜：`plugins/dalton/skills/dalton-bullbear-tracker/scripts/collect.py:1410-1418`、`plugins/dalton/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py:600-603`、`plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/sales_notes_search.py:199-207`；交互初筛 `plugins/dalton_data/plugin.json:16` | 本地四库 SQLite + 并发扇出 `:8950` 与 `:8943` | `sources.yaml:13-18` 挂 `researchdata_bridge`，未入 `BRIDGE_ROUTE_CAPABILITY_MAP` |
| `research` | `alphaengine` | `search_library`、`get_document`、`get_document_info`、`list_attachments`、`list_recording_tasks`、`get_recording_status`、`get_recording_task`、`list_filters`、`auth_status` | `:2517-2659,13930-14041`；上游 `scripts/dalton/research_runtime/scripts/alphaengine-mcp/server.mjs:1-220` | `dalton-thesis-delta/scripts/collect.py:902-915`、`dalton-bullbear-tracker/scripts/collect.py:2733`、`dalton-sector-delta/scripts/collect_sector.py:89-90`、`dalton-mark-weekly-newenergy/scripts/mark_weekly.py:610-619`、`dalton-richard-coverage-weekly/scripts/richard_cov_transport.py:12-45`、`dalton-china-consumer-weekly/scripts/china_consumer_weekly.py:1110-1112`、`dalton-global-consumer-weekly/scripts/global_consumer_weekly.py:1143`、`dalton-tob-industry-weekly/SKILL.md:81,89`、`workflows/topic-research/workflow.js:165`、`workflows/bullbear-doc/workflow.js:249`、`src/dalton/data_map/health.py:860-920` | 本机 `:8950`、`/Applications/AlphaEngine.app`（`server.mjs:112`）、钥匙串 `dalton-alphaengine`（`server.mjs:113-116`）、SQLite v4 缓存库 `alphaengine_search_cache.sqlite3`、`cfg.usage_file` | `alphaengine`（`capabilities.py:777`） |
| `research` | `transcript` | `transcript` | `:2704-2738`、`data_gateway.py:43,212-214` | `dalton-earnings-flash/SKILL.md:42`、`dalton-full-coverage/references/branch-c-primary.md:21`、`dalton-ai-app-weekly-monitor/SKILL.md:201-204` | 与 `alphaengine` 共用 `:8950`、缓存库与熔断器 | `alphaengine`（`capabilities.py:778`） |
| `research` | `guidepoint` | `search_library` | `:2660-2703`、`scripts/dalton/research_runtime/scripts/guidepoint_mcp_proxy.py:1-217` | `dalton-thesis-delta/scripts/collect.py:890-901`、`dalton-humanoid-robot-weekly/scripts/humanoid_robot_weekly.py:148-159`、`dalton-china-consumer-weekly/scripts/china_consumer_weekly.py:1113-1115`、`dalton-global-consumer-weekly/scripts/global_consumer_weekly.py:1144`、`dalton-tob-industry-weekly/SKILL.md:94`、`workflows/topic-research/workflow.js:186-191`、`src/dalton/data_map/health.py:921-965` | 本机 `:8943` 代理、`guidepoint_mcp_tokens.json`（进程内锁刷新写回，`:57,99`）、`GUIDEPOINT_CLIENT_ID`/`SECRET`、远端 `clapi.guidepoint.io` | `guidepoint`（`capabilities.py:779`） |
| `research` | `company-wiki` | `search`、`read`、`list`、`topic` | `:2739-2791`、`scripts/dalton/research_runtime/scripts/research_library.py:162,170` | `dalton-thesis-delta/scripts/collect.py:880-889`（传 `params.indexed_since`）、`dalton-mark-weekly-newenergy/scripts/mark_weekly.py:590-595`、`dalton-wiki-auto-ingest/scripts/wiki_ingest.py:93`、`workflows/topic-research/workflow.js:181-185`、`src/dalton/data_map/health.py:795-859` | `<DALTON_RESEARCH_HOME>/wiki/`、`wiki/vectors.db`、`state/research-images/assets`、`GEMINI_API_KEY` | `company_wiki`（`capabilities.py:780`） |
| `research` | `sales-notes` | `search`、`read`、`list` | `:2792-2837`、`scripts/dalton/research_runtime/scripts/sales_notes_store.py:18-78` | `dalton-thesis-delta/scripts/collect.py:916-928`、`dalton-richard-coverage-weekly/scripts/sales_notes_search.py:186-210`、`dalton-mark-weekly-newenergy/scripts/rd_transport.py:50-115`、`workflows/topic-research/workflow.js:183` | `skills/market-digest/vectors.db`（与 `tmtb` 及 `market-digest` 共用同一库文件，见 `research_library.py:172`）、`state/sales-notes/manifest.jsonl`、`GEMINI_API_KEY` | `sales_notes`（`capabilities.py:781`） |
| `research` | `sales-mail` | `search`、`read`、`list` | `:2838-2883`、`data_gateway.py:129-131` | 历史技能与交互兼容调用（`search` 同时查 `sales-notes` 与 `tmtb`，`read` 接受 `mail:`/`digest:`/`tmtb:`） | 同 `sales-notes` + `tmtb` | 未登记在 `BRIDGE_ROUTE_CAPABILITY_MAP`（当前随总开关暴露） |
| `research` | `tmtb` | `search`、`read`、`list` | `:2884-2929`；同步脚本 `scripts/dalton/research_runtime/scripts/tmtb_alphaengine_sync.py:1-160` | `dalton-thesis-delta/scripts/collect.py:916-928`、`dalton-richard-coverage-weekly/scripts/sales_notes_search.py:186`、`workflows/topic-research/workflow.js:183` | `skills/market-digest/vectors.db`（`message_id LIKE 'alphaengine:%'`）、`GEMINI_API_KEY` | `tmtb`（`capabilities.py:782`） |
| `research` | `funda-ai` | `search`、`read`、`list` | `:2930-2975`、`scripts/dalton/research_runtime/scripts/funda_ai.py:1-160` | `dalton-thesis-delta/scripts/collect.py:916-928`、`dalton-richard-coverage-weekly/scripts/sales_notes_search.py:186`、`workflows/topic-research/workflow.js:183` | `<DALTON_RESEARCH_HOME>/state/funda-ai/index.sqlite`（`funda_ai.py:126-127`） | `funda_ai`（`capabilities.py:783`） |
| `research` | `tmeet` | `auth_status`、`record_search`、`record_content` | `:3094-3138,11686-11708` | `plugins/dalton/skills/dalton-tmeet-weekly-minutes/scripts/tmeet_minutes.py:85-89,358-501` | 本机 `tmeet` CLI（`BridgeConfig.tmeet_bin` 于 `:1175-1183`）与登录态 | 当前未在 `BRIDGE_ROUTE_CAPABILITY_MAP` 或 `sources.yaml` 登记 |
| `research` | `gcal` | `list_events`、`insert_event`、`delete_event` | `:3139-3184,11970-11993` | `plugins/dalton/skills/dalton-richard-earnings/scripts/richard_earnings.py:195-199,565-840` | 本机 `gws` CLI（`:1184-1192`）、`DALTON_GCAL_CALENDAR_ID` | `sources.yaml:121-128` 挂 `researchdata_bridge` + `fund_automations`，未入 `BRIDGE_ROUTE_CAPABILITY_MAP` |
| `research` | `discord-history` | `list_messages` | `:3185-3209,12079-12145` | `plugins/dalton/skills/dalton-wiki-auto-ingest/scripts/wiki_ingest.py:93,475,719` | `DALTON_DISCORD_BOT_TOKEN`、`DALTON_DISCORD_HISTORY_CHANNELS` | `channel_discord`（`capabilities.py:784`） |
| `finance` | `sec` | 11 个操作：`list-filings`、`financials`、`ratios`、`facts`、`smart-facts`、`search-concepts`、`search-text`、`read-item`、`attachments`、`eight-k`、`insider` | `:2220-2302`、`data_gateway.py:143-187` | `dalton-thesis-delta/scripts/collect.py:932-943`、`dalton-bullbear-tracker/references/{amzn,googl,meta,msft,ant,spcx}-rules.md`、`dalton-short-screen/SKILL.md:33-51`、`dalton-filing-review/SKILL.md:15-33`、`dalton-tob-industry-weekly/SKILL.md:70`、`dalton-global-consumer-weekly/scripts/global_consumer_weekly.py:1147`、`dalton-ai-app-weekly-monitor/SKILL.md:188-191`、`plugins/langalpha_market_data/fundamentals_mcp_server.py:845-858` | `<DALTON_RESEARCH_CODE>/skills/findata-analyst/run.sh`、`SEC_USER_AGENT`、SEC EDGAR API | `sources.yaml:61-66` 挂 `researchdata_bridge` |
| `finance` | `estimates` | `consensus`（支持 `params.shares_bn`、`params.tax`） | `:2303-2338`、`data_gateway.py:215-225` | `dalton-bullbear/scripts/implied_model.py:21,200`、`dalton-earnings-preview/SKILL.md:38`、`dalton-tob-industry-weekly/SKILL.md:32`、`dalton-init-company/templates/data_consensus_README.md:3,10`、`dalton-conventions/SKILL.md:91` | `<DALTON_RESEARCH_CODE>/scripts/consensus_estimates.sh` | `sources.yaml:67-72` 挂 `researchdata_bridge` |
| `finance` | `cn-hk` | `filings`、`ask`、`quote` | `:2339-2374`、`data_gateway.py:194-211` | `dalton-daily-filing-alert/SKILL.md:63-80`、`dalton-stock-move/scripts/cn_price_fallback.py:253-277`、`dalton-china-consumer-weekly/scripts/china_consumer_weekly.py:1116-1118`、`dalton-tob-industry-weekly/SKILL.md:71-74`、`dalton-financial-lenses/SKILL.md:41-54`、`src/dalton/autonomy/probes/cnhk.py:184-310` | `<DALTON_RESEARCH_CODE>/skills/cn-hk-findata/run.sh`、巨潮/AkShare/腾讯/新浪接口 | `sources.yaml:73-78` 挂 `researchdata_bridge` |
| `macro` | `fred` | `macro`（支持 `series`、`start`、`end`、`vintage`） | `:2375-2399`、`data_gateway.py:188-190` | 沙箱宏观折现率与通胀就业查询（`sources.yaml:79-84`、`plugins/dalton_data/plugin.json:16`） | `findata-analyst/run.sh macro`、`FRED_API_KEY` | `sources.yaml:79-84` 挂 `researchdata_bridge` |
| `macro` | `treasury` | `treasury`（`dataset="tga" \| "debt"`） | `:2400-2422`、`data_gateway.py:191-193` | 沙箱流动性 TGA 余额与联邦债务查询（`sources.yaml:85-90`） | `findata-analyst/run.sh treasury`、美国财政部 Fiscal Data 公开 API | `sources.yaml:85-90` 挂 `researchdata_bridge` |
| `macro` | `market-regime` | `snapshot`、`us_topbottom`、`crowding`、`cn_leverage` | `:2423-2459,13208-13539` | 大盘状态与仓位拥挤度查询（`sources.yaml:91-96`、`dalton-ct-semi-sentiment/SKILL.md:54-55`） | `<DALTON_RESEARCH_HOME>` 下 `data_jobs` 产出的状态 JSON/CSV | `sources.yaml:91-96` 挂 `researchdata_bridge` |
| `community` | `xueqiu`、`employee`、`last30days`、`x-timeline` | `xueqiu`(`posts`/`post`/`comments`/`search`/`user`)、`employee`(`blind`/`summary`/`indeed`/`glassdoor`)、`last30days`(`search`)、`x-timeline`(`tweets`/`tweet`/`thread`/`search`) | `:2976-3093,12578-12750` | `dalton-bullbear-tracker/scripts/collect.py:2198,2238`、`dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py:1435,1503`、`dalton-humanoid-robot-weekly/scripts/humanoid_robot_weekly.py:160-170`、`dalton-china-consumer-weekly/scripts/china_consumer_weekly.py:1122-1124`、`dalton-global-consumer-weekly/scripts/global_consumer_weekly.py:1145`、`dalton-full-coverage/references/branch-d-offstreet.md:18-29`、`dalton-x-accounts-daily/SKILL.md:34-35,94` | `xueqiu_posts.py`、`employee_reviews_cli.py`、`last30days.py`、本机 `xreach` CLI | `sources.yaml:97-120` 挂 `researchdata_bridge` |

---

## 2. 目标形态

### 2.1 全路由与全子路由唯一归宿总表

下表为第 2 轮统一的唯一权威归宿清单，覆盖现行桥上每一组 HTTP 路由及 `/mcp` 下每一个子路由与操作：

| 原桥入口与子路由 / 操作 | 唯一目标归宿（连接器插件 / 后端模块 / 宿主服务 / 网关） | Everflow 专属 | 对应能力开关（`CapabilityId`） | 交付批次 | 替代后数据等价性与差异处理 |
|---|---|---|---|---|---|
| `GET /healthz` | 各宿主机服务各自提供 `/healthz`：`dalton-research-host`（`:8951/healthz`，保留从 JSON `usage_file` 读取当日 AlphaEngine 读文统计与熔断器状态）、`dalton-transcription`（`:8965/healthz`）、`host_task_daemon`（`:8952/healthz`）；缩减桥保留 `:8960/healthz` 至第 ③ 阶段 | 否（按已运行服务各自暴露） | 随各服务对应能力开启 | 第 2–5 批分步就位 | 等价；运维检查脚本按职责分别探活对应端口 |
| `/mcp` → `catalog` | 各连接器标准 MCP `tools/list` schema + `plugin.json` `instruction`；全局可用源汇总由 `src/dalton/data_map/builder.py` 渲染至 `<data_map>` | 否 | 随各连接器能力开启 | 第 3–4 批 | 等价；Agent 直接在 `<mcp-servers>` 与 `.agents/tools/docs/<server>/` 查看完整参数说明 |
| `research` → `alphaengine`（全部 9 个操作，含 `auth_status`、`list_filters`）+ `transcript` | 连接器插件 `dalton-alphaengine`（MCP 服务名 `dalton_alphaengine`，`streamable-http` 直连宿主机 `dalton-research-host` `:8951/mcp/alphaengine`） | 是（仅 `DALTON_HOST_PLATFORM=macos`） | `alphaengine` | 第 4 批 | 完全等价；继续走本机 `:8950` `server.mjs`、钥匙串与桌面客户端，宿主机单实例维护 SQLite v4 缓存、单飞、熔断与当日读文 JSON 账本 |
| `research` → `guidepoint`（`search_library`） | 连接器插件 `dalton-guidepoint`（MCP 服务名 `dalton_guidepoint`，`streamable-http` 直连宿主机 `:8951/mcp/guidepoint`） | 否（商业私有，Linux/macOS 宿主均可部署宿主服务） | `guidepoint` | 第 3 批 | 完全等价；Auth0 刷新令牌仅保存在宿主机 `dalton-research-host` 进程内由唯一异步锁刷新写盘 |
| `research` → `company-wiki`（`search`/`read`/`list`/`topic`）+ `POST /wiki/ingest` | 检索归插件 `dalton-company-wiki`（MCP 服务名 `dalton_company_wiki`，`streamable-http` 连研究服务 `:8951/mcp/company-wiki`）；入库归 `wiki_tools.py` 调用研究服务受控写口 `POST :8951/ingest/wiki`（核心安装可选运行开源 `dalton-wiki-service` 容器） | 否（公开引擎接口，核心安装可独立开启） | `company_wiki` | 第 3 批 | 完全等价；保留 `params.indexed_since`、`topic`、配图资产引用及幂等文件锁审计，Wiki 库留在研究服务侧受控访问 |
| `research` → `sales-notes`（`search`/`read`/`list`）+ `POST /sales-notes/ingest` | 检索归私有插件 `dalton-sales-notes`（MCP 服务名 `dalton_sales_notes`，`streamable-http` 连宿主机 `:8951/mcp/sales-notes`）；入库归 `ingest_tools.py` 调宿主机 `POST :8951/ingest/sales-notes` | 是（基金内部交易台邮件库） | `sales_notes` | 第 3 批 | 完全等价；宿主机服务强制限定查询 `sales-notes` 分区，隔离同库其他表 |
| `research` → `tmtb`（`search`/`read`/`list`） | 私有插件 `dalton-tmtb`（MCP 服务名 `dalton_tmtb`，`streamable-http` 连宿主机 `:8951/mcp/tmtb`；增量同步脚本归 `scripts/dalton/ops/prod/`） | 是 | `tmtb` | 第 3 批 | 完全等价；宿主机服务在 SQL 层强制限定 `message_id LIKE 'alphaengine:%'`，仅启用 `tmtb` 时无法越权读取同库的 `sales-notes` |
| `research` → `funda-ai`（`search`/`read`/`list`） | 私有插件 `dalton-funda-ai`（MCP 服务名 `dalton_funda_ai`，`streamable-http` 连宿主机 `:8951/mcp/funda-ai`） | 是 | `funda_ai` | 第 3 批 | 完全等价 |
| `research` → `all`（`search`/`read`/`list`/`get_document`）及兼容路由 `sales-mail` | 宿主机聚合插件 `dalton-research-all`（MCP 服务名 `dalton_research_all`，`streamable-http` 连宿主机 `:8951/mcp/federated`） | 否（随任一已启用的内部研究源自动生效） | 复合门控：`alphaengine \| guidepoint \| company_wiki \| sales_notes \| tmtb \| funda_ai`（其中 `sales_mail` 子工具要求 `sales_notes \| tmtb`） | 第 3 批 | 完全等价；宿主机进程内对当前已启用研究源并行扇出，保留 `alphaengine`、`guidepoint`、`source_status` 顶层字段、按文档 ID 前缀（`wiki:`/`mail:`/`digest:`/`tmtb:`/`funda:`）分派 `read` 全文及 `sales-mail` 跨 `sales-notes`+`tmtb` 检索语义 |
| `research` → `tmeet`（3 个操作）与 `gcal`（3 个操作） | 合并为私有办公连接器插件 **`dalton-office`**（MCP 服务名 `dalton_office`，`streamable-http` 连宿主机 `:8951/mcp/office`，在宿主机调用 `tmeet` 与 `gws` CLI） | 是 | `fund_automations`（`dalton-tmeet-weekly-minutes` 技能同时校验 `fund_automations,feishu_docs`） | 第 4 批 | 完全等价；保留 `tmeet` 东八区时间转换与 `gcal` 宿主机固定日历 ID 及 tracker 标记核验 |
| `finance` → `sec`（全部 11 个操作：`list-filings`、`financials`、`ratios`、`facts`、`smart-facts`、`search-concepts`、`search-text`、`read-item`、`attachments`、`eight-k`、`insider`） | **扩展公开插件 `plugins/dalton_sec/`**（MCP 服务名 `dalton_sec`，沙箱 `stdio` 运行，仅需公开合规头 `SEC_USER_AGENT`） | 否（核心安装与 Everflow 默认共用） | `CORE_CAPABILITY`（常驻开启） | 第 3 批 | **数据等价**：基于官方 SEC EDGAR Submissions、CompanyFacts 与归档 HTML/EX-99.1 全文解析补齐原 `findata-analyst` 全部 11 项能力（含 `as_of` 点时过滤、`form` 默认与显式覆盖、`accession` 精确附件读取、章节 `read-item` 与全文关键词窗口 `search-text`） |
| `finance` → `estimates`（`consensus`，含 `shares_bn`、`tax`） | 私有连接器插件 `dalton-estimates`（MCP 服务名 `dalton_estimates`，`streamable-http` 连宿主机 `:8951/mcp/estimates` 调 `consensus_estimates.sh`） | 是 | 新增 `CapabilityId.ESTIMATES`（`everflow` 默认 `True`，`core` 默认 `False`） | 第 4 批 | **Everflow 完全等价**（保留 `field_status`、分期预期币种与 `implied_model` 复合快照供 `plugins/dalton/skills/dalton-bullbear/scripts/implied_model.py:200` 消费）。**核心安装差异处理**：核心安装关闭 `estimates` 时，技能经 `DALTON_CAP_ELSE` 改调内置 `yf_analysis` 获取分析师预期表，并在 `implied_model.py` 中使用显式命令行参数计算隐含倍数 |
| `finance` → `cn-hk`（`filings`、`ask`、`quote`） | 私有连接器插件 `dalton-cnhk`（MCP 服务名 `dalton_cnhk`，`streamable-http` 连宿主机 `:8951/mcp/cnhk`） | 是（依赖宿主机 `cn-hk-findata` 运行环境） | 新增 `CapabilityId.CN_HK`（`everflow` 默认 `True`，`core` 默认 `False`） | 第 4 批 | **Everflow 完全等价**。**核心安装差异处理**：`cn_hk` 关闭时，港 A 股行情走内置 `yfinance`（`yf_price`/`yf_fundamentals`），法定公告原文走 `WebSearch`（`site:hkexnews.hk` / `cninfo.com.cn`） |
| `macro` → `fred`（`macro`，含 `vintage`）与 `treasury`（`dataset="tga" \| "debt"`） | **新增公开插件 `plugins/dalton_macro/`**（MCP 服务名 `dalton_macro`，沙箱 `stdio` 运行；`treasury` 直连美国财政部 Fiscal Data 免密钥公开 API，`fred` 读取可选 `FRED_API_KEY`） | 否（公开插件，随核心包分发） | `CORE_CAPABILITY`（`fred` 无密钥时返回明确配置提示，`treasury` 开箱即用） | 第 3 批 | **完全等价，且与内置 `macro` 明确区分**：内置 `plugins/langalpha_market_data/macro_mcp_server.py:304` 的 `get_treasury_rates` 返回 1M–30Y 国债收益率曲线，而 `dalton_macro.get_fiscal_treasury(dataset="tga"\|"debt")` 返回财政部 TGA 现金余额与联邦债务总额，两者互补并存 |
| `macro` → `market-regime`（`snapshot`、`us_topbottom`、`crowding`、`cn_leverage`） | 私有连接器插件 `dalton-market-regime`（MCP 服务名 `dalton_market_regime`，`streamable-http` 连宿主机 `:8951/mcp/market-regime` 读取 `data_jobs` 快照） | 是 | `data_jobs` | 第 4 批 | 完全等价；与生产 `data_jobs` 看板产出同源 |
| `community` → `xueqiu`、`employee`、`last30days`、`x-timeline` | 私有连接器插件 **`dalton-community`**（MCP 服务名 `dalton_community`，`streamable-http` 连宿主机 `:8951/mcp/community`） | 是 | 新增 `CapabilityId.COMMUNITY_SOURCES`（`everflow` 默认 `True`，`core` 默认 `False`） | 第 4 批 | **Everflow 完全等价**（保留雪球、Blind/Glassdoor、`last30days` 及 `xreach` 固定账号时间线的 `oldest_fetched_at` 水位字段）。**核心安装差异处理**：关闭时经 `DALTON_CAP_ELSE` 改用内置 `x_api` 与 `WebSearch`/`scrape`（内置 `x_api` 按搜索 API 返回推文，不含 `xreach` 主页水位字段） |
| `POST /embed` | 后端内置嵌入模块 `src/dalton/embeddings/`（支持 Gemini 与 OpenAI 兼容 `/v1/embeddings`，及可选外部服务 URL），配库级元数据表 `dalton_embedding_metadata` | 否（公开仓库核心能力底座） | 随 `conversation_archive` 开启 | 第 1 批 | 完全等价；Everflow 锁定 `gemini-embedding-2-preview` + 3072 维 + 原任务类型，核心新装按所选模型维度初始化 |
| `GET /jobs/ops` | `scripts/dalton/ops/prod/export_ops_status.py`（宿主机定时原子写入 `var/dalton/ops_jobs_status.json`）+ 后端 `src/dalton/autonomy/system_jobs.py` 只读解析 | 是 | ` DALTON_PROFILE=everflow` 且状态文件存在时展示（`core` 返回空列表） | 第 1 批 | 完全等价 |
| `/transcriptions*`（6 个端点） | 独立转写连接器服务 `dalton-transcription`（监听 `:8965`，独占接管 `var/dalton/asr_jobs/` 与 `asr_usage.jsonl`） | 否（服务框架与客户端公开，Seed-ASR 2.0 凭据按环境注入） | `transcription`（含转写对象存储门控） | 第 2 批 | 完全等价；保留全部 6 个 HTTP 端点、说话人分离逐字稿与历史任务下载路径 |
| `/internal/market-digest/*` 与 `/internal/ai-finance/*` | 宿主机研究服务受控管理口 `:8951/internal/{market-digest,ai-finance}/*`（与共享 `market-digest/vectors.db` 同进程协调写入） | 是 | `fund_automations` | 第 5 批 | 完全等价 |
| `/internal/data-tasks/{host-run,host-kill}` | 宿主机独立守护进程 `scripts/dalton/ops/prod/host_task_daemon.py`（监听 `127.0.0.1:8952`，独占持有 `.dalton-host-runner-state.json`） | 是 | `data_jobs` | 第 5 批 | 完全等价；完整保留 `generation`、进程组 `pgid`+`lstart`、`fence_lost`、命令白名单与按任务凭据注入 |
| `POST /notify`、`POST /feishu/*`（8 个子路径）、`research` → `discord-history` | **第 ② 阶段保留在缩减后的桥进程 `:8960` 上**；第 ③ 阶段整体并入 `integrations/dalton-channel-gateway/` | 否（按渠道能力开关控制） | `channel_feishu`、`feishu_docs`、`channel_discord` | 留待第 ③ 阶段（第 5 批仅缩减桥代码） | 第 ② 阶段零变动、完全等价；第 5 批完成与第 ③ 阶段的解耦 |

### 2.2 宿主机研究服务形态选型：单进程多连接器模块 vs 多独立进程

根据既定原则，商业研究检索数据与凭据统一留在宿主机侧。针对宿主机服务形态，**推荐采用“单宿主机研究服务进程 `dalton-research-host`（监听 `127.0.0.1:8951`）内按连接器划分独立子应用 `/mcp/<connector>`，仅保留上游 `:8950` `alphaengine-mcp` Node 桥独立运行”**，理由如下：

1. **共享 SQLite 库的单点访问控制与写协调**：`sales-notes`、`tmtb` 与每日 `market-digest` 共用 `<DALTON_RESEARCH_HOME>/skills/market-digest/vectors.db`（`scripts/dalton/research_runtime/scripts/research_library.py:163,172`）。由单一宿主机进程统一管理该库的读写连接并按路由路径 `/mcp/sales-notes` 与 `/mcp/tmtb` 强制附加来源过滤条件，既消除了多进程并发写锁争用，又保证单独开启 `tmtb` 时请求无法触达同库中的销售纪要记录。
2. **供应商刷新令牌唯一写入方**：Guidepoint 的 Auth0 令牌每次刷新会轮换或更新本地 `guidepoint_mcp_tokens.json`（`scripts/dalton/research_runtime/scripts/guidepoint_mcp_proxy.py:57,99`）。收编进单实例 `dalton-research-host` 后，进程内唯一异步锁即可保证全机只有一个刷新写入方，同时省去原先独立的 `:8943` 代理守护进程。
3. **AlphaEngine 全局配额、单飞与缓存一致性**：AlphaEngine 的公平并发限流器、SQLite v4 短时搜索缓存（`scripts/dalton/alphaengine_search_cache.py`）、30 分钟同公司软提示、熔断器及 JSON 当日读文计数（`scripts/dalton/researchdata_bridge.py:10897-10935`）需要跨所有工作区沙箱共享同一份内存与本地状态。
4. **跨库初筛 `all` 零本机二次网络跳转**：`/mcp/federated` 在同一进程内直接按已启用能力并发调用各连接器模块与按前缀读取全文，并保持对外每个连接器各自拥有独立 URL（`http://host.docker.internal:8951/mcp/<connector>`）与独立 `mcp.json` 插件声明——日后若需将任一连接器独立部署到其他机器或容器，只需修改对应连接器的 URL 环境变量，插件包与沙箱代码保持不变。

### 2.3 返回结构契约与完整工具签名

#### （1）对齐真实采集解析器与溯源提取器的返回信封契约

对照 `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py` 中的真实解析函数：
- 错误判别：`_extract_non_empty_error`（`:648-660`）与 `_normalize_dict_result`（`:675-744`）；
- 分页提取：`_extract_route_page_info`（`:946-1004`）；
- 条目与分页联合提取：`_extract_route_items_with_page_info`（`:1006-1010`）及 `_extract_route_items`（`:1013-1055`）；
- 溯源展开：`src/dalton/provenance/declared_sources.py:48-120`（`_extract_raw_sources_list` 与 `expand_declared_sources`）。

所有研究与金融连接器严格遵守以下四条字段契约：

1. **结果数组位置**：统一放在顶层 `"results": [...]` 或 `"data": {"structuredContent": {"results": [...]}}`（`guidepoint` 保持 `"data": {"structuredContent": {"data": [...]}}`，`transcript` 保持 `"data": {"transcripts": [...], "coverage": {...}}`，与 `_extract_route_items` `:1023-1053` 完全匹配）。
2. **扁平分页元数据字段**：在包含 `results` 的同一字典层级直接返回扁平键 `"cursor"`（字符串或 `null`）、`"has_more"`（布尔值）、`"total_upstream"`（整数或 `null`）、`"returned"`（整数），直接匹配 `_extract_route_page_info`（`:957-967`）。
3. **跨库初筛 `all` 专属字段**：`dalton_research_all.search` 顶层返回 `{"ok": true, "status": "ok", "results": [...], "result_count": int, "alphaengine": {...}, "guidepoint": {...}, "source_status": {...}, "_sources": [...]}`（对齐 `scripts/dalton/researchdata_bridge.py:2511`）。
4. **溯源字段契约**：`src/dalton/provenance/declared_sources.py:56-60` 优先读取 `"_declared_sources"`（只要该键的值为 `list` 即直接采用并跳过 `"_sources"`）。因此各连接器统一仅写入非空的 `"_sources": [...]`（省略空的 `_declared_sources` 键）；同时在 `src/dalton/provenance/declared_sources.py:13` 的 `_ALLOWED_SERVERS` 中注册全部新连接器服务名（`dalton_alphaengine`、`dalton_guidepoint`、`dalton_company_wiki`、`dalton_sales_notes`、`dalton_tmtb`、`dalton_funda_ai`、`dalton_research_all`、`dalton_office`、`dalton_sec`、`dalton_estimates`、`dalton_cnhk`、`dalton_macro`、`dalton_market_regime`、`dalton_community`）。

标准检索返回示例：
```json
{
  "ok": true,
  "status": "ok",
  "normalized_params": {
    "query": "宁德时代 300750",
    "start": "2026-10-01",
    "end": "2026-11-24",
    "limit": 20
  },
  "data": {
    "structuredContent": {
      "results": [
        {
          "doc_id": "290001790954279_inst_134249",
          "title": "...",
          "publish_time": "2026-11-20",
          "type_show_name": "机构会议"
        }
      ],
      "cursor": "eyJwYWdlIjoyfQ==",
      "has_more": true,
      "total_upstream": 48,
      "returned": 20
    }
  },
  "_sources": [
    {
      "identity": "broker_report",
      "source_type": "research_doc",
      "document_id": "290001790954279_inst_134249",
      "title": "...",
      "publisher": "...",
      "as_of": "2026-11-20",
      "url": "https://..."
    }
  ]
}
```

#### （2）核心连接器完整工具签名清单

1. **`dalton-alphaengine`（服务名 `dalton_alphaengine`，宿主机 `:8951/mcp/alphaengine`）**——完整保留原 `alphaengine` 9 个操作与 `transcript` 路由（对照 `scripts/dalton/researchdata_bridge.py:2520-2658,2704-2738`）：
   - `search_library(query="", ticker=None, start=None, end=None, limit=20, params=None)`：`params` 支持 `cursor`、`sort`（`"relevance"` | `"newest"`）、`search_in`（`"full_text"` | `"title"`）、`meeting_scope`（`"institution"` | `"personal"`）、`document_categories`、`type_ids`、`industry_names`、`company_names`、`company_short_names`、`stock_group_ids`、`first_regions`、`second_regions`、`fresh`（布尔值）。按 `cursor` / `has_more` 翻页。
   - `get_document(document=None, params=None)`：支持顶层 `document` 或 `params.doc_id`（保留 `_inst_` 后缀）、`params.content_field`（`"auto"` | `"original_text"` | `"content"` | `"digest"` | `"digest_cn"` | `"main_point"` | `"question_answer"`）、`params.offset`、`params.max_chars`（1–30000）、`params.mode`（`"auto"` | `"public"` | `"private"`）；返回 `next_offset` 与 `complete`。
   - `get_document_info(document=None, params=None)` 与 `list_attachments(document=None, params=None)`：读取文档元数据与可下载附件列表。
   - `list_recording_tasks(start=None, end=None, limit=20, params=None)`、`get_recording_status(params=None)`、`get_recording_task(params)`：`list_recording_tasks` 支持未来日期对 `date_from`/`date_to`、`industry_ids`、`meeting_scope`、`page`（返回 `next_page` 与 `has_more`）；`get_recording_task` 必填 `params.roadshow_id`。
   - `list_filters(params=None)`：`params.filter_type` 完整支持 8 类词典——`"categories"`（默认）、`"upstream_categories"`、`"document_types"`（可选 `params.document_code`）、`"sources"`、`"industries"`（可选 `params.document_code`）、`"companies"`（**必填 `params.query`**）、`"regions"`、`"watchlist_groups"`、`"watchlist_stocks"`（**必填 `params.group_id`**）；支持 `params.limit`（1–200）与 `params.offset`（返回 `next_offset`）。
   - `auth_status()`：探测桌面客户端与钥匙串会话有效状态。
   - `transcript(ticker, query=None, start=None, end=None, limit=4, params=None)`：`ticker` 必填，可选 `query` 支持 `|` 分隔中英文公司名，`params.fresh` 控制缓存刷新；单次最多扫描 10 页×50 条，返回 `data.transcripts` 与 `data.coverage`（`complete=false` 时产出 `continuation_params` 供 `search_library` 续查）。

2. **`dalton-company-wiki`（服务名 `dalton_company_wiki`，`:8951/mcp/company-wiki`）**——完整覆盖 4 个操作（对照 `scripts/dalton/researchdata_bridge.py:2739-2791`）：
   - `list(limit=50, params=None)`：`params` 完整支持 **`indexed_since`**（`YYYY-MM-DD`，供 `dalton-thesis-delta/scripts/collect.py:880-889` 与 `dalton-mark-weekly-newenergy/scripts/mark_weekly.py:590-595` 增量扫描）、`company`、`sector`、`tag`、`media`（`"text"` | `"all"`）。
   - `search(query=None, ticker=None, start=None, end=None, limit=10, params=None)`：`params` 支持 `company`、`sector`、`tag`、`search_mode`、`no_decay`、`media`、`indexed_since`。
   - `read(document, params=None)`：按 `wiki:<id>` 读取正文与关联图表，`params` 支持 `offset`、`max_chars`。
   - `topic(query=None, limit=20, params=None)`：按主题词 `query` 或 `params.tag` 聚合专题目录。

3. **`dalton-guidepoint`、`dalton-sales-notes`、`dalton-tmtb`、`dalton-funda-ai`、`dalton-research-all`**：
   - `dalton_guidepoint.search_library(query, ticker=None, start=None, end=None, limit=10, params=None)`：保留 `params.keywords`（1–10 项）、`params.recency_bias`（1–100）、`params.start_date`/`end_date`。
   - `dalton_sales_notes` / `dalton_tmtb` / `dalton_funda_ai`：各自提供 `search(query, ticker=None, start=None, end=None, limit=15, params=None)`（`params` 含 `sender`、`layer`、`search_mode`、`media`）、`read(document, params=None)`（`offset`、`max_chars`）、`list(limit=50, params=None)`。
   - `dalton_research_all`：提供 `search`、`read`（按 `wiki:`/`mail:`/`digest:`/`tmtb:`/`funda:` 前缀在宿主机侧校验对应子能力后分派读取全文）、`list` 及兼容入口 `sales_mail(operation, ...)`。

4. **公开插件 `plugins/dalton_sec/`（服务名 `dalton_sec`）扩展清单**：
   - 保留现有 `list_filings`、`get_company_facts`（`plugins/dalton_sec/dalton_sec_mcp_server.py:967`），并在同一公开插件内新增 `get_financials`（支持 `statement="all"|"income"|"balance"|"cash"`）、`get_ratios`、`search_concepts`、`search_smart_facts`、`search_filing_text`（支持 `form`、`window`、`max_matches`）、`read_filing_item`（支持 `form`、`item`、`max_chars` 1–500000）、`get_filing_attachments`（支持 `document` 或 `accession`+`document_type`、`max_chars`）、`list_eight_k_events`、`list_insider_transactions`，完整承接原 `finance(route="sec")` 全部 11 个操作。

5. **公开插件 `plugins/dalton_macro/`、私有插件 `dalton-estimates` / `dalton-cnhk` / `dalton-market-regime` / `dalton-office` / `dalton-community`**：
   - `dalton_macro`：`get_fred_series(series, start, end, vintage=None, limit=None)`、`get_fiscal_treasury(dataset, start, end, limit=None)`（`dataset` 为 `"tga"` 或 `"debt"`）。
   - `dalton_estimates`：`get_consensus(ticker, params=None)`（支持 `shares_bn`、`tax`，原样返回含 `implied_model` 的复合结构）。
   - `dalton_cnhk`：`filings(ticker, start, end, limit=20)`、`ask(query, limit=20)`、`quote(ticker)`。
   - `dalton_market_regime`：`get_regime(operation="snapshot", top=10, order="high", category=None, query=None, limit=None)`（支持 `snapshot`、`us_topbottom`、`crowding`、`cn_leverage`）。
   - `dalton_office`：`tmeet_auth_status()`、`tmeet_record_search(...)`、`tmeet_record_content(...)`、`gcal_list_events(...)`、`gcal_insert_event(...)`、`gcal_delete_event(...)`。
   - `dalton_community`：`xueqiu(...)`、`employee(...)`、`last30days(...)`、`x_timeline(...)`。

### 2.4 公开仓库与私有仓库边界及唯一插件目录 `DALTON_EXTRA_PLUGINS_DIR` 装配机制

1. **仓库边界**：
   - **公开仓库（主仓，进入核心发布包）**：`src/dalton/capabilities.py` 全部能力定义与映射表、`src/dalton/content_assembly.py`、`src/dalton/embeddings/`、转写 HTTP 客户端 `src/dalton/services/transcriptions.py`、公开数据插件 `plugins/dalton_sec/` 与 `plugins/dalton_macro/`、`dalton-company-wiki` 插件声明与开源版轻量 Wiki 服务实现。
   - **私有仓库（`dalton-connectors`）**：宿主机 `dalton-research-host` 商业服务端代码（AlphaEngine 缓存与上游对接、Guidepoint Auth0 令牌管理、销售纪要/TMTB/Funda AI/市场摘要/AI Finance/一致预期/港 A 股/社区爬虫/办公 CLI 封装）、火山引擎 Seed-ASR 2.0 转写后端，以及商业连接器的插件声明目录 `plugins/dalton_*`（仅含 `plugin.json`、`mcp.json` 与技能 Markdown，零服务端密钥）。

2. **私有插件目录唯一命名与四步装配链路**：
   全文统一使用单一环境变量 **`DALTON_EXTRA_PLUGINS_DIR`**（指向单个目录路径，Everflow 生产在 `docker-compose.dalton.yml` 中将私有仓库的 `plugins/` 目录只读挂载至 `backend` 容器的 `/opt/dalton-extra-plugins` 并设置 `DALTON_EXTRA_PLUGINS_DIR=/opt/dalton-extra-plugins`）：
   - **① 插件发现入口**：扩展 `src/ptc_agent/config/plugins.py:100-125` 的 `_bundle_dirs()`，先扫描内置 `BUNDLES_DIR`（`plugins/`），当 `DALTON_EXTRA_PLUGINS_DIR` 设置且目录可读时继续按目录名排序追加扫描；若包名与内置包重名则记录错误并拒绝覆盖。所有私有连接器名称提前静态写入 `src/dalton/capabilities.py:603-637` 的 `MCP_SERVER_CAPABILITY_MAP` 与 `BUNDLED_PLUGIN_CAPABILITY_MAP`，避免未知服务名在 `is_mcp_server_enabled_by_capability`（`:640-653`）中被默认放行。
   - **② 沙箱代码与包装器同步**：私有连接器在 `mcp.json` 中均为 `type: "streamable-http"`（`auth` 为 `None`，`relay_bound=False`，见 `src/ptc_agent/config/core.py:362-373`），跳过 `src/ptc_agent/core/sandbox/assets.py:669-720` 的单文件 `stdio` 上传路径；后端连接宿主机 `:8951/mcp/<connector>` 拉取工具 schema 后，由 `ToolFunctionGenerator` 在沙箱生成 `tools/dalton_*.py` 包装模块。插件包内附带的技能目录均包含合规 `SKILL.md`，经 `src/ptc_agent/core/sandbox/assets.py:314-335` 自动同步并完成 `DALTON_CAP` 条件裁剪。
   - **③ 共享采集传输辅助模块分发**：沙箱技能共用的连接器路由分派函数直接放在 `plugins/dalton/skills/dalton-conventions/scripts/connector_transport.py`（位于带 `SKILL.md` 的核心常驻技能 `dalton-conventions` 内，随 `src/ptc_agent/core/sandbox/assets.py:314-335` 同步进每一个沙箱，并保留在 `scripts/standalone/package_release.sh:282-288` 核心导出树内）。
   - **④ 核心发布包剥离规则**：`scripts/standalone/package_release.sh:152-165,282-310` 维持显式白名单归档：移除归档后的 `plugins/dalton_data`、`scripts/dalton/researchdata_bridge.py`、`scripts/dalton/research_runtime`、`scripts/dalton/ops`；将新增公开插件 `plugins/dalton_macro` 加入白名单；`DALTON_EXTRA_PLUGINS_DIR` 位于仓库树外部，导出的核心包天然不含任何商业连接器文件。

### 2.5 嵌入模块与库级维度锁定设计（承接 `/embed`）

核查现有代码：`src/dalton/migrations/versions/d009_conversation_archive.py:60,71-74` 将 `dalton_conversation_cards.embedding` 固定定义为 `vector(3072)` 与 `halfvec(3072)` HNSW 索引，`src/dalton/database/conversations.py:534,546,550` 在 SQL 查询中写死 `halfvec(3072)`，且 `src/dalton/conversations/search.py:262-275` 在向量查询报错时仅记录 warning 并静默退回关键词检索。

为此，`src/dalton/embeddings/` 采用**库级元数据锁定 + 动态维度建库与查询**机制：
1. **元数据表结构（迁移 `d050`）**：在 Postgres 新建 `dalton_embedding_metadata` 表，主键 `scope`（如 `'conversations'`），列包含 `provider`、`model_name`、`dimensions`（正整数约束）、`task_type_doc`、`task_type_query`、`created_at`、`updated_at`（宿主机 Wiki SQLite `vectors.db` 同步维护同结构 `embedding_metadata` 表）。
2. **Everflow 生产保持 3072 维零变动**：升级执行 `d050` 时，若 `dalton_conversation_cards` 已存在记录或当前运行于 `everflow` 形态，迁移自动写入锁定行 `(scope='conversations', provider='gemini', model_name='gemini-embedding-2-preview', dimensions=3072, task_type_doc='RETRIEVAL_DOCUMENT', task_type_query='RETRIEVAL_QUERY')`，原有 `vector(3072)` 列与 `ix_dalton_conv_cards_embedding_hnsw` 索引原样保留。
3. **核心安装按所选模型初始化维度**：核心新装首次启用 `conversation_archive` 时，初始化逻辑读取配置的模型与维度 `dim`（如 OpenAI `text-embedding-3-small` 的 `1536` 维或 `3072` 维），在空表状态下执行 `ALTER TABLE dalton_conversation_cards ALTER COLUMN embedding TYPE vector(<dim>)` 并重建 `USING hnsw ((embedding::halfvec(<dim>)) halfvec_cosine_ops)` 索引，随后写入 `dalton_embedding_metadata` 锁定该库维度。
4. **维度一致性强校验与显式报错**：后端启动及每次调用 `embed_texts` / `search_cards_dense` 前，强制比对当前配置的 `(model_name, dimensions)` 与 `dalton_embedding_metadata` 锁定值；发现差异时立即抛出致命配置错误（在 `src/dalton/conversations/search.py:262-275` 中对维度/元数据异常直接上抛告警，保持与瞬态超时区分）。`src/dalton/database/conversations.py:534-558` 读取经白名单整数校验后的已锁定 `dim` 构造 `halfvec(<dim>)` 余弦距离查询。

---

## 3. 调用方迁移

### 3.1 沙箱技能传输层演进与防越权回退规则

1. **修正现状认知**：当前 `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:569-578` 的 `resolve_researchdata_transport()` 逻辑为**显式设置环境变量 `DALTON_RESEARCHDATA_URL` 时优先走直连 HTTP 桥（覆盖模式）**，缺省时导入沙箱包装模块 `tools.researchdata` 经 `mcp_client.call_mcp_tool_with_error_flag` 调用。
2. **新共享适配层 `plugins/dalton/skills/dalton-conventions/scripts/connector_transport.py`**：
   - 读取沙箱工作区 `.agents/tools/mcp_client_config.json`（同 `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:595-627`）获取当前工作区已启用的 MCP 服务集合 `enabled_servers`。
   - **单源调用 `call_source_connector(source_id, operation, **kwargs)`**：
     - 若目标新连接器（如 `dalton_alphaengine`、`dalton_guidepoint`、`dalton_company_wiki`、`dalton_sales_notes`、`dalton_tmtb`、`dalton_funda_ai`、`dalton_sec`、`dalton_cnhk`、`dalton_estimates`、`dalton_office`、`dalton_community`）在 `enabled_servers` 中，则直接经 `call_mcp_tool_with_error_flag` 调用该连接器的对应工具（显式调试覆盖变量按连接器独立命名为 `DALTON_CONNECTOR_URL_<NAME>`）。
     - **受控过渡回退规则**：兼容过渡期内，仅当旧 `researchdata` 仍在 `enabled_servers` 中、**且**旧桥侧 `is_bridge_route_enabled(route_id)`（`src/dalton/capabilities.py:789-801`）与宿主机服务端路由门控同时校验该子路由对应的 `CapabilityId` 处于开启态时，才允许回退调用 `researchdata`；若某子能力已被管理员关闭，新连接器从 `enabled_servers` 隐去且旧桥路由门直接拒绝该 `route`，采集脚本收到明确的能力关闭状态并跳过该源。

### 3.2 沙箱技能、工作流与内置插件调用点迁移全表

| 类别 | 文件路径与精确行号 | 当前调用内容 | 迁移目标与改动说明 |
|---|---|---|---|
| 采集脚本 | `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:569-646,870-944` | `resolve_researchdata_transport`（`:569`）、6 个内部研究路由（`:870-929`，含 Wiki `indexed_since`）、`finance(route="sec", operation="list-filings")`（`:932-943`） | 切换至 `connector_transport.py` 分派 6 个研究连接器；`:932-943` 改调 `tools.dalton_sec.list_filings` |
| 采集脚本 | `plugins/dalton/skills/dalton-bullbear-tracker/scripts/collect.py:157-158,600-601,1410-1418,1523,1915,2011,2198,2238,2733` | 复用 `thesis-delta` 传输层；`:1410-1418` 调 `research(route="all", operation="read")`；`:2198,2238` 调 `community`（`xueqiu`/`employee`/`last30days`/`x-timeline`） | 研究源走各连接器；`:1410-1418` 全文读取改调 `tools.dalton_research_all.read`；社区调用改调 `tools.dalton_community`（关闭时降级 `x_api`） |
| 采集脚本 | `plugins/dalton/skills/dalton-sector-delta/scripts/collect_sector.py:89-90,182,376,386` | 导入 `thesis-delta` 传输函数采集内部研究源 | 改用 `connector_transport.py` 按已启用研究连接器并发采集 |
| 周报脚本 | `plugins/dalton/skills/dalton-mark-weekly-newenergy/scripts/rd_transport.py:50-115` 及 `plugins/dalton/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py:590-620,1918,2323` | `plugins/dalton/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py:590-595` 调 `company-wiki` `list`(`indexed_since`)，`:600-603` 调 `all` `read`，`:610-619` 调 `alphaengine` `get_document` | `rd_transport.py` 委托 `connector_transport.py`，分别路由至 `dalton_company_wiki`、`dalton_research_all.read`、`dalton_alphaengine` |
| 周报脚本 | `plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/richard_cov_transport.py:12-45`、`plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/sales_notes_search.py:44,124,192-210,405-421`、`plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py:1216-1224,1435,1503` | `plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/sales_notes_search.py:199-207` 调 `route="all", operation="read"` 读四库全文；`plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py:1435,1503` 调 `community` | `sales_notes_search.py` 改调各库连接器与 `dalton_research_all.read`；周报 CLI 社区调用切至 `dalton_community` |
| 周报脚本 | `plugins/dalton/skills/dalton-humanoid-robot-weekly/scripts/humanoid_robot_weekly.py:148-170` | 构造 `guidepoint`（`:148-159`）与 `x-timeline`（`:160-170`）调用计划 | 调用计划改为指向 `dalton_guidepoint.search_library` 与 `dalton_community.x_timeline`（带 `DALTON_CAP` 门控） |
| 周报脚本 | `plugins/dalton/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py:1110-1125` | 输出 `alphaengine`、`guidepoint`、`cn-hk`、`xueqiu`/`last30days` 调用指令 | 按 `effective_capabilities` 或新连接器工具名输出 `dalton_alphaengine`、`dalton_guidepoint`、`dalton_cnhk`、`dalton_community` 调用指引 |
| 周报脚本 | `plugins/dalton/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py:1143-1147` | 输出 `alphaengine`、`guidepoint`、`last30days`、`sec` 调用指令 | 分别改为 `dalton_alphaengine`、`dalton_guidepoint`、`dalton_community.last30days`、`dalton_sec.list_filings` |
| 周报/估值脚本 | `plugins/dalton/skills/dalton-tob-industry-weekly/scripts/tob_industry_weekly.py:241`、`plugins/dalton/skills/dalton-bullbear/scripts/implied_model.py:21,200` | `tob` 引用 `finance()` 取公告；`implied_model.py` 读取 `estimates` 快照 JSON | `tob` 指向 `dalton_sec` / `dalton_cnhk`；`implied_model.py` 兼容 `dalton_estimates.get_consensus` 快照与核心安装显式参数模式 |
| 办公与入库脚本 | `plugins/dalton/skills/dalton-tmeet-weekly-minutes/scripts/tmeet_minutes.py:85-89,358-501`、`plugins/dalton/skills/dalton-richard-earnings/scripts/richard_earnings.py:195-199,565-840`、`plugins/dalton/skills/dalton-wiki-auto-ingest/scripts/wiki_ingest.py:91-95,475,719`、`plugins/dalton/skills/dalton-stock-move/scripts/cn_price_fallback.py:253-277` | 分别调 `tmeet`、`gcal`、`company-wiki`+`discord-history`、`cn-hk` | `tmeet_minutes.py` 与 `richard_earnings.py` 改调 `tools.dalton_office`；`wiki_ingest.py` Wiki 读取改调 `tools.dalton_company_wiki`（`discord-history` 第 ③ 阶段随网关切换）；`cn_price_fallback.py` 改调 `tools.dalton_cnhk` |
| 转写 CLI 脚本 | `plugins/dalton/skills/dalton-transcript-polish/scripts/dalton_asr.py:49,382-588` | 仅调用后端 `/api/v1/dalton/transcriptions*` | 后端 API 路径与载荷完全不变，原脚本直接沿用 |
| JS 工作流与契约 | `workflows/topic-research/workflow.js:165,181-186,194,201-208,220,243,260,276` + `contract.json`、`workflows/bullbear-doc/workflow.js:249`、`workflows/initial-coverage/workflow.js:321` | 使用 `/* DALTON_CAP:researchdata_bridge */` 门控四库检索支线并硬编码 `检索 researchdata 的 sales-notes...` | 条件表达式改为 `company_wiki\|sales_notes\|tmtb\|funda_ai`（`content_assembly.py:281-353` 同步改为同一条件），提示词改为按已启用连接器调用 |
| 内置市场数据插件 | `plugins/langalpha_market_data/fundamentals_mcp_server.py:845-858` | `DALTON-HOOK(market-data-fallback)` 注释与工具文档串指引美股优先调 `finance(route="sec", operation="financials")` | 文档串更新为指引调用公开连接器 `dalton_sec.get_financials`（并同步刷新 `agent_docstring_lock.json`） |
| 全部技能 Markdown 与规则模板 | 涵盖 `plugins/dalton/agents/dalton-evidence.md:5,22-69`、`dalton-daily-filing-alert/SKILL.md:63-80`（调 `cn-hk` `filings`）、`dalton-short-screen/{SKILL.md,references/four-dimensions.md}`、`dalton-tob-industry-weekly/{SKILL.md,references/source-universe.yaml}`、`dalton-global-consumer-weekly/SKILL.md`、`dalton-china-consumer-weekly/{SKILL.md,references/source-universe.yaml}`、`dalton-humanoid-robot-weekly/SKILL.md`、`dalton-ai-app-weekly-monitor/{SKILL.md,references/backfill-protocol.md}`、`dalton-stock-move/{SKILL.md,references/peer-groups.md}`、`dalton-bullbear-tracker/references/{common,amzn,googl,meta,msft,ant,spcx}-rules.md`、`dalton-init-company/templates/{data_consensus_README,sources_README}.md`、`dalton-conventions/`、`dalton-bullbear/`、`dalton-earnings-{calendar,preview,flash,deep}/`、`dalton-filing-review/`、`dalton-financial-lenses/`、`dalton-company-background/`、`dalton-initial-coverage/`、`dalton-full-coverage/`、`dalton-topic-research/`、`dalton-weekly-review/`、`dalton-ct-semi-sentiment/`、`dalton-x-accounts-daily/`、`dalton-vip-news-alert/`、`dalton-tmeet-weekly-minutes/SKILL.md`（新增 `DALTON_CAP:fund_automations,feishu_docs`） | 使用笼统的 `<!-- DALTON_CAP:researchdata_bridge -->` 及 `researchdata.<tool>(route=...)` 调用示例 | 按具体能力拆分为细粒度 `<!-- DALTON_CAP:alphaengine -->`、`<!-- DALTON_CAP:company_wiki\|sales_notes\|tmtb\|funda_ai -->`、`<!-- DALTON_CAP:cn_hk -->`、`<!-- DALTON_CAP:estimates -->`、`<!-- DALTON_CAP:community_sources -->` 等标签；SEC 调用统一改为 `dalton_sec.*`（常驻无需门控），宏观 TGA/FRED 改为 `dalton_macro.*` |

### 3.3 后端、前端与宿主机运维脚本迁移全表

| 模块 / 脚本路径与行号 | 当前逻辑 | 迁移改动说明 |
|---|---|---|
| `src/dalton/settings.py:1090-1151,1227,1263,1324,1419-1435` | 集中解析 `DALTON_RESEARCHDATA_URL`、`DALTON_BRIDGE_URL`、`DALTON_BRIDGE_TOKEN`、`DALTON_BRIDGE_ADMIN_TOKEN` 且强校验 `RESEARCHDATA_BRIDGE` | 新增 `get_dalton_research_host_url()`、`get_dalton_transcription_service_url()`、`get_dalton_host_runner_url()` 及对应独立 Token 读取函数；保留 `get_dalton_bridge_base_url()` 专供缩减后的飞书/通知桥使用直至第 ③ 阶段 |
| `src/dalton/conversations/embedder.py:75-207`、`src/dalton/database/conversations.py:525-558`、`src/dalton/conversations/search.py:250-285` | 调桥 `/embed`（`:150-151` 强校验 `RESEARCHDATA_BRIDGE`），写死 `halfvec(3072)`，向量报错静默退回关键词 | 改为调用后端 `src/dalton/embeddings/client.py`；读取 `dalton_embedding_metadata` 校验模型与维度并动态构造 `halfvec(<dim>)` 查询；维度不匹配显式报错 |
| `src/dalton/autonomy/system_jobs.py:232-315` | `_fetch_ops_jobs` 调桥 `GET /jobs/ops`（校验 `RESEARCHDATA_BRIDGE`） | 改为读取宿主机定时导出的 `var/dalton/ops_jobs_status.json`（校验 `resolve_profile() == DeploymentProfile.EVERFLOW`） |
| `src/dalton/services/transcriptions.py:461-584,2038-2075` | `_require_transcription_bridge_url` 校验 `RESEARCHDATA_BRIDGE` 并调桥 `/transcriptions*` | 改为校验 `CapabilityId.TRANSCRIPTION` 并请求 `DALTON_TRANSCRIPTION_SERVICE_URL` + `DALTON_TRANSCRIPTION_TOKEN` |
| `src/dalton/tools/__init__.py:94-143,370-410`、`src/dalton/tools/wiki_tools.py:447-485`、`src/dalton/tools/ingest_tools.py:72-105,578-677` | `ingest_research` 门控为 `RESEARCHDATA_BRIDGE`（`:400`）；入库 POST 到旧桥 `:8960` | `ingest_research` 门控改为 `has_capability(COMPANY_WIKI) or has_capability(SALES_NOTES)`；入库请求指向 `dalton-research-host` `:8951/ingest/{wiki,sales-notes}`；`market_digest_*` 与 `ai_finance_*`（`:94-143`）指向 `:8951/internal/*` |
| `web/src/pages/ChatAgent/components/dalton/AutoIngestToggle.tsx:26` 与 `plugins/langalpha_service/skills/langalpha-doc/references/settings-map.md:27` | `hasCapability('researchdata_bridge')` 控制工作区自动入库按钮与帮助表 | 改为 `hasCapability('company_wiki') \|\| hasCapability('sales_notes')`，帮助文档标签改为 `<!-- DALTON_CAP:company_wiki\|sales_notes -->` |
| `src/dalton/data_map/builder.py:287-355,488-503`、`src/dalton/data_map/health.py:173-235,790-1060`、`src/dalton/data_map/sources.yaml:1-140` | `builder.py:288` 在 `RESEARCHDATA_BRIDGE` 关闭时直接返回空列表 `[]`；`:325` 调 `is_bridge_route_enabled` | 解除 `:288` 对 `RESEARCHDATA_BRIDGE` 的一刀切返回，改为按各连接器各自 `CapabilityId` 合并渲染与健康探测；更新 `sources.yaml` 各条目的 `capability` 与调用示例 |
| `src/dalton/content_assembly.py:231-254,267-365` | `assemble_markdown_text`（`:240-253`）与 `assemble_json_text`（`:284,306`）在 `RESEARCHDATA_BRIDGE` 关闭时强制替换“内部研究库”文案并裁剪 JSON 契约 | 触发条件改为“内部四库连接器能力（`company_wiki`、`sales_notes`、`tmtb`、`funda_ai`）全部关闭时”才执行公开模式文案替换与 JSON 契约裁剪 |
| `src/dalton/feishu.py:106-130` | `post_bridge_feishu`（`:117-118`）统一检查 `RESEARCHDATA_BRIDGE` | 文档子路由检查 `FEISHU_DOCS`，机器人/群列表子路由检查 `CHANNEL_FEISHU`；第 ② 阶段继续请求缩减桥 `:8960/feishu/*`，第 ③ 阶段切至渠道网关 |
| `src/dalton/autonomy/probes/cnhk.py:184-310` | 拼接 `:8960/mcp/<token>` 探测 `cn-hk` | 校验 `CapabilityId.CN_HK` 并探测 `:8951/mcp/cnhk` |
| `src/dalton/data_jobs/host_runner.py:23-29,108-181` | 调 `:8960/internal/data-tasks/{host-run,host-kill}` | 改为请求独立守护进程 `DALTON_HOST_RUNNER_URL`（`http://host.docker.internal:8952`）+ `DALTON_HOST_RUNNER_TOKEN` |
| `scripts/dalton/bridge_subprocess_env.py:2229-2427`、`scripts/dalton/ops/prod/deploy_main.sh:633,658,662,915-924`、`scripts/dalton/ops/research_runtime_migrate.sh:806-812`、`scripts/standalone/init.sh:1-95` | 合并旧桥 `.env`、写入 `DALTON_RESEARCHDATA_URL`、重启桥与 `--check-runtime` | 写入 `DALTON_HOST_PLATFORM`、注入 `dalton-research-host` / `dalton-transcription` / `host_task_daemon` 地址与令牌；`deploy_main.sh` 按服务清单管理新 `launchd` 服务与缩减桥；`research_runtime_migrate.sh` 改为调用 `dalton-research-host --check-runtime` |

### 3.4 退役判定标准（双指标核对 + 低频任务主动演练）

旧路由下线判定同时满足以下三项硬指标，避免因调用方报错中断、低频任务未到排程或日志停更造成“假零命中”误判：
1. **旧入口请求量归零**：在连续 **14 个自然日**内，旧桥按路由记录的审计日志显示目标路由请求数为 **0**，且计数心跳日志每日持续写入（证明计量器本身存活）。
2. **新入口成功调用量与质量达标**：同一 14 天窗口内，对应新连接器或新后端模块的成功请求量 > 0、错误率不高于切流前基线，AlphaEngine 搜索缓存命中率与 `prov:` 引用生成率与切流前基线一致。
3. **低频与周频任务主动实跑通过**：在观察期内确认所有周频/双周频/月频消费者（含 `dalton-mark-weekly-newenergy`、`dalton-richard-coverage-weekly`、`dalton-humanoid-robot-weekly`、`dalton-china-consumer-weekly`、`dalton-global-consumer-weekly`、`dalton-tob-industry-weekly`、`dalton-tmeet-weekly-minutes`、`T58`、`T60`、`T61`）至少完整成功跑通 1 次（未到自然排程的在维护窗口主动触发一次实跑核验）。

---

## 4. 能力与开关

### 4.1 宿主平台配置与能力注册表映射

1. **宿主平台配置 `DALTON_HOST_PLATFORM`**：
   - 在 `src/dalton/capabilities.py` 新增 `resolve_host_platform()`：优先读取环境变量 `DALTON_HOST_PLATFORM`（及 `dalton.yaml` 的 `host_platform`，合法值为 `macos` 或 `linux`，非法值在启动时抛出 `DaltonStartupConfigError`）；未显式设置时，`everflow` 形态缺省为 `macos`，`core` 形态缺省为 `linux`。
   - Everflow 生产由 `scripts/dalton/bridge_subprocess_env.py` 与 `scripts/dalton/ops/prod/deploy_main.sh` 固定写入 `DALTON_HOST_PLATFORM=macos`；核心安装由 `scripts/standalone/init.sh` 按宿主机 `uname -s` 实测结果自动写入 `DALTON_HOST_PLATFORM=macos` 或 `DALTON_HOST_PLATFORM=linux`。
   - `effective_capabilities()` 在解析 `CapabilityId.ALPHAENGINE` 时校验 `resolve_host_platform() == "macos"`；在 Linux 宿主上 `alphaengine` 恒解析为关闭态，且完全不受容器内 Linux Python 的 `sys.platform` 影响。

2. **连接器能力开关总表**（各能力的 `depends_on=(RESEARCHDATA_BRIDGE,)` 仅在对应连接器全入口改完的那一批同步移除）：

| 能力 ID（`CapabilityId`） | 绑定的插件包与 MCP 服务名 | 拆分完成后 `depends_on` | 额外前置约束 | `core` 默认值 | `everflow` 默认值 |
|---|---|---|---|---|---|
| `alphaengine` | `dalton-alphaengine`（`dalton_alphaengine`，含 `transcript`） | `()` | `resolve_host_platform() == "macos"` | `False` | `True` |
| `guidepoint` | `dalton-guidepoint`（`dalton_guidepoint`） | `()` | 宿主研究服务已配置 Guidepoint 凭据 | `False` | `True` |
| `company_wiki` | `dalton-company-wiki`（`dalton_company_wiki`）+ `/ingest/wiki` | `()` | 研究服务已配置 Wiki 存储目录 | `False` | `True` |
| `sales_notes` | `dalton-sales-notes`（`dalton_sales_notes`）+ `/ingest/sales-notes` | `()` | 宿主研究服务已挂载销售笔记库 | `False` | `True` |
| `tmtb` | `dalton-tmtb`（`dalton_tmtb`） | `()` | 宿主研究服务已挂载 TMTB 库 | `False` | `True` |
| `funda_ai` | `dalton-funda-ai`（`dalton_funda_ai`） | `()` | 宿主研究服务已挂载 Funda AI 库 | `False` | `True` |
| `estimates`（新增） | `dalton-estimates`（`dalton_estimates`） | `()` | 宿主研究服务已配置一致预期脚本 | `False` | `True` |
| `cn_hk`（新增） | `dalton-cnhk`（`dalton_cnhk`） | `()` | 宿主研究服务已配置 `cn-hk-findata` | `False` | `True` |
| `community_sources`（新增） | `dalton-community`（`dalton_community`） | `()` | 宿主研究服务已配置社区抓取环境 | `False` | `True` |
| `fund_automations` | `dalton-office`（`dalton_office`：含 `tmeet` 与 `gcal`）+ 摘要/AI Finance 工具 | `()` | Everflow 宿主办公 CLI 与摘要目录 | `False` | `True` |
| `data_jobs` | `dalton-market-regime`（`dalton_market_regime`）+ `host_task_daemon.py` | `()` | 定时任务与快照目录 | `False` | `True` |
| `transcription` | 独立服务 `dalton-transcription` | `()` | `_resolve_transcription_storage_configured()` 为真（`capabilities.py:425-433`） | `False` | `False`（显式开启） |
| `conversation_archive` | 后端 `src/dalton/conversations/` + `src/dalton/embeddings/` | `()` | 嵌入模型密钥与 `dalton_embedding_metadata` 就绪 | `False` | `True` |
| `feishu_docs` | 缩减桥 `/feishu/doc/*`、`/feishu/wiki/*`（第 ③ 阶段迁渠道网关） | 第 ② 阶段保持 `(RESEARCHDATA_BRIDGE,)`（第 ③ 阶段改挂 `CHANNEL_GATEWAY` 或独立 OpenAPI） | `FEISHU_APP_ID`、`FEISHU_APP_SECRET` | `False` | `True` |
| `researchdata_bridge` | 第 5 批后仅代表缩减后的飞书/通知过渡桥（`:8960`），不再控制研究连接器 | `()` | 第 ③ 阶段渠道网关接管后彻底退役 | `False` | 第 ② 阶段保持 `True`（仅服务飞书/通知过渡） |

同时补齐 `BRIDGE_ROUTE_CAPABILITY_MAP`（`src/dalton/capabilities.py:775-786`）中此前缺失的子路由映射：`"sales-mail"` 映射为 `sales_notes | tmtb` 复合校验，`"tmeet"` 与 `"gcal"` 映射为 `CapabilityId.FUND_AUTOMATIONS`，`"estimates"` 映射为 `CapabilityId.ESTIMATES`，`"cn-hk"` 映射为 `CapabilityId.CN_HK`，`"market-regime"` 映射为 `CapabilityId.DATA_JOBS`，`"xueqiu"`/`"employee"`/`"last30days"`/`"x-timeline"` 映射为 `CapabilityId.COMMUNITY_SOURCES`。

### 4.2 能力关闭时全栈各层的表现

基于 `src/dalton/capabilities.py` 与 `src/dalton/content_assembly.py` 的真实实现，当某项连接器能力处于关闭态时：

1. **插件与 MCP 服务器装配层**：`is_bundled_plugin_enabled_by_capability`（`src/dalton/capabilities.py:656-676`）与 `filter_mcp_servers_by_capability`（`:678-690`）在 `bundled_mcp_servers()`（`src/ptc_agent/config/plugins.py:278-355`）加载阶段直接剔除未启用连接器；`filter_reconnectable_builtin_mcp`（`capabilities.py:709-728`）跳过未启用服务的后台重连；沙箱仅生成已启用连接器的 `tools/dalton_*.py` 包装器。宿主机 `dalton-research-host` 同步读取能力开关，对未启用子路由返回 `403 capability_disabled`。
2. **后端直连工具注册层**：`src/dalton/tools/__init__.py:94-143,370-410` 按 `DaltonToolRegistration.capability` 过滤工具；`company_wiki` 与 `sales_notes` 均关闭时，`ingest_company_wiki`、`ingest_sales_note` 与 `ingest_research` 全部从工具表省去。
3. **技能、工作流与 JSON 契约条件装配层**：`src/dalton/content_assembly.py` 通过 `evaluate_capability_expr`（`:70-130`）与栈式解析器 `_assemble_stacked`（`:156-229`）对 `.md`/`.mdx`/`.txt`/`.yaml`（`assemble_markdown_text` 于 `:231-254`）和 `.js` 工作流（`assemble_js_text` 于 `:256-265`）执行嵌套条件裁剪，并通过 `assemble_json_text`（`:267-365`）裁剪 `contract.json` 与 `common.schema.json`。当某连接器关闭时，包含其调用指令的片段自动切换至 `DALTON_CAP_ELSE` 公开源替代方案（如 `yf_analysis`、`x_api`、`WebSearch`）。
4. **系统提示词与 `<data_map>` 层**：`src/dalton/data_map/builder.py:287-355,488-518` 仅将已启用连接器渲染进可用数据源表，并将未开启的内部源按规则列入“明确没有的数据”提示栏，防止模型猜测或盲调。
5. **前端设置页、对话栏与帮助文档层**：前端 `web/src/hooks/useCapabilities.ts:1-177`、`web/src/pages/Settings/sections.ts:1-360` 与 `web/src/pages/ChatAgent/components/dalton/AutoIngestToggle.tsx:26` 仅渲染已启用能力对应的设置分组与工作区自动入库开关；帮助站点与 `langalpha-doc` 经同一套 `assemble_markdown_text` 保持展示一致。

---

## 5. 运行与部署

### 5.1 进程分布与网络访问路径

| 组件名称 | 运行位置与端口 | 进程与网络机制 | 凭据与数据隔离边界 |
|---|---|---|---|
| `plugins/dalton_sec/`、`plugins/dalton_macro/` | 沙箱容器内 `stdio` MCP 子进程 | 由沙箱 supervisor 按 `uv run python mcp_servers/<entry>.py` 按需启动（同 `plugins/dalton_sec/mcp.json:4-15`） | 仅访问公开 SEC EDGAR / 财政部 Fiscal Data / 可选 `FRED_API_KEY`，零商业私有数据 |
| `dalton-research-host`（承载 `alphaengine`、`guidepoint`、`company-wiki`、`sales-notes`、`tmtb`、`funda-ai`、`all`、`office`、`estimates`、`cnhk`、`market-regime`、`community` 及受控入库/摘要管理口） | Everflow：macOS 宿主机 `127.0.0.1:8951`（核心安装若开启 `company_wiki` 则运行独立 `wiki-service:8951` 容器） | 后端容器与 Everflow 沙箱容器（配置 `ExtraHosts = ["host.docker.internal:host-gateway"]`，见 `src/ptc_agent/core/sandbox/providers/docker.py:774-783`）通过 `http://host.docker.internal:8951/mcp/<connector>` 直连 `streamable-http`；`src/ptc_agent/core/sandbox/mcp_client_runtime.py:2177-2198` 自动附带每次请求签名的 `X-Dalton-Caller-Token` | 宿主机进程独占持有 SQLite 库文件、Wiki/邮件目录、Guidepoint Auth0 刷新令牌与 Gemini 密钥；沙箱只持有 `DALTON_RESEARCH_HOST_TOKEN` 调用令牌 |
| `com.dalton.alphaengine-mcp`（`server.mjs`） | Everflow macOS 宿主机 `127.0.0.1:8950` | 仅对本机回环 `dalton-research-host`（`:8951`）开放调用 | 读取 macOS 钥匙串 `dalton-alphaengine` 与 `/Applications/AlphaEngine.app` |
| `src/dalton/embeddings/` | 后端 `backend` 容器进程内 | FastAPI 异步进程内直接请求配置的 Embedding API（或可选独立嵌入服务） | 嵌入 API 密钥仅存于 `backend` 容器环境，不进入沙箱 |
| `dalton-transcription` | 独立容器或宿主机服务（`:8965`） | 后端 `src/dalton/services/transcriptions.py` 通过 `DALTON_TRANSCRIPTION_SERVICE_URL` 访问；沙箱脚本 `dalton_asr.py` 仅调后端 `/api/v1/dalton/transcriptions*` | `SEED_ASR_*`、`VOLC_TOS_*` 与 `var/dalton/asr_jobs/`（排他锁 `.owner.lock`）仅归该服务持有 |
| `host_task_daemon.py` | Everflow macOS 宿主机 `127.0.0.1:8952` | 后端 `host_runner.py` 通过 `http://host.docker.internal:8952` + `DALTON_HOST_RUNNER_TOKEN` 调用 | 独占持有 `.dalton-host-runner-state.json`，按任务白名单最小化注入凭据 |
| 缩减后的 `researchdata_bridge.py`（过渡期） | Everflow macOS 宿主机 `127.0.0.1:8960` | 第 5 批后仅保留 `/notify`、`/feishu/*`、`discord-history` 供后端调用，直至第 ③ 阶段并入 `:8962` `dalton-channel-gateway` | 仅持有 `FEISHU_*` 与 `DALTON_DISCORD_BOT_TOKEN` |

### 5.2 Everflow 生产与核心安装部署步骤对比

1. **Everflow 生产（macOS 宿主 + Colima 容器栈，`docker-compose.dalton.yml`）**：
   - `scripts/dalton/bridge_subprocess_env.py` 与 `scripts/dalton/ops/prod/deploy_main.sh` 向 `<repo>/.env` 写入 `DALTON_HOST_PLATFORM=macos`、`DALTON_EXTRA_PLUGINS_DIR=/opt/dalton-extra-plugins`、`DALTON_RESEARCH_HOST_URL=http://host.docker.internal:8951`、`DALTON_TRANSCRIPTION_SERVICE_URL`、`DALTON_HOST_RUNNER_URL=http://host.docker.internal:8952` 及各自独立的随机令牌。
   - `docker-compose.dalton.yml` 将私有连接器插件声明目录只读挂载至 `backend` 容器的 `/opt/dalton-extra-plugins:ro`（商业服务端代码与研究数据库留在宿主机文件系统中，不挂载进任何沙箱容器）。
   - Everflow 沙箱因 `is_core_profile()` 为 `False`（`src/ptc_agent/core/sandbox/providers/docker.py:778-782`），继续获得 `host.docker.internal:host-gateway` 映射，直连宿主机 `:8951` 的各个 `/mcp/<connector>` 端点并附带 `X-Dalton-Caller-Token`。

2. **独立核心安装（Linux 或 macOS Docker，`docker-compose.yml` + `docker-compose.core.yml`）**：
   - `scripts/standalone/init.sh` 检测宿主机 `uname -s` 自动向 `.env` 写入 `DALTON_HOST_PLATFORM=linux`（或 `macos`），生成核心密钥并保持全部商业能力默认关闭。
   - 开箱默认启用公开插件 `dalton_sec`（11 项 SEC 能力）、`dalton_macro`（财政部 TGA/债务与可选 FRED）、`yfinance`、`macro`、`scrape` 等。
   - 按需开启扩展能力：
     - **对话归档检索**：配置嵌入模型密钥与 `EMBEDDING_DIMENSIONS`（如 `1536` 或 `3072`），设 `DALTON_CAP_CONVERSATION_ARCHIVE=true`，重启 `backend` 完成 `dalton_embedding_metadata` 维度锁定。
     - **内部公司 Wiki**：启用 `docker-compose.core.yml` 中的可选 `wiki-service` profile（容器内运行开源 Wiki 检索与入库服务），设置 `DALTON_CAP_COMPANY_WIKI=true`。
     - **语音转写**：配置后端转写专用对象存储（满足 `src/dalton/capabilities.py:425-433` 校验）并启用 `transcription` profile 服务，设置 `DALTON_CAP_TRANSCRIPTION=true`。

### 5.3 macOS `launchd` 服务演进清单（Everflow 生产）

| `launchd` 服务标识 | 模板位置 | 演进动作与批次 | 职责说明 |
|---|---|---|---|
| `com.dalton.alphaengine-mcp` | `scripts/dalton/launchd/com.dalton.alphaengine-mcp.plist.template` | **长期保留** | 在 `127.0.0.1:8950` 运行 `server.mjs`，对接 `/Applications/AlphaEngine.app` 与钥匙串 |
| `com.dalton.alphaengine-desktop-watchdog` | `scripts/dalton/launchd/com.dalton.alphaengine-desktop-watchdog.plist.template` | **长期保留** | 守护 macOS AlphaEngine 桌面客户端 |
| `com.dalton.ops-status-exporter` | 新增于 `scripts/dalton/ops/prod/launchd/` | **第 1 批新增** | 每 300 秒执行 `export_ops_status.py`，原子更新 `var/dalton/ops_jobs_status.json` |
| `com.dalton.transcription` | 新增于私有仓库 / `scripts/dalton/ops/prod/launchd/` | **第 2 批新增**（按需启用） | 在 `127.0.0.1:8965` 独立运行转写连接器服务，持有 `asr_jobs/.owner.lock` |
| `com.dalton.research-host` | 新增于私有仓库部署目录 | **第 3 批新增，第 4–5 批扩充模块** | 在 `127.0.0.1:8951` 提供全部内部研究检索连接器端点、受控入库及摘要管理口 |
| `com.dalton.guidepoint-mcp-proxy` | `scripts/dalton/launchd/com.dalton.guidepoint-mcp-proxy.plist.template` | **第 3 批步骤 C 卸载**（切流并完成观察期后） | 令牌刷新迁入 `com.dalton.research-host` 单实例模块；观察期内暂停旧代理以保证单一刷新方 |
| `com.dalton.host-task-daemon` | 新增于 `scripts/dalton/ops/prod/launchd/` | **第 5 批新增** | 在 `127.0.0.1:8952` 独立运行宿主任务执行守护，持有 `.dalton-host-runner.owner.lock` |
| `com.dalton.researchdata-bridge` | `scripts/dalton/launchd/com.dalton.researchdata-bridge.plist.template` | **第 5 批缩减为纯通知/飞书代理，第 ③ 阶段卸载** | 第 5 批清空研究、嵌入、转写、宿主任务代码，仅服务 `/notify`、`/feishu/*`、`discord-history` 直至第 ③ 阶段网关接管 |
| `com.dalton.colima`、`pg-backup`、`backend-watchdog`、`portwatch`、`channel-gateway`、`monitor-feeds` | `scripts/dalton/ops/prod/launchd/` 等 | **保持现状** | 既有基础设施与运维守护服务 |

---

## 6. 分批计划

### 6.1 数据库迁移编号与合并顺序约定

当前仓库迁移头为 **`d046`**（`src/dalton/migrations/versions/d046_scoped_data_map_cache.py:23-24`，其 `down_revision = "d048"`）。发布顺序明确约定如下：
1. **微信多账号迁移 `d049`（接 `d046`）先于本设计含迁移的批次合入**，确保主干始终保持单一线性迁移头。
2. 本设计首个需要数据库迁移的批次从 **`d050`** 起编号，后续需要迁移的批次依次顺延，每批使用唯一迁移号且合入时严格接当时唯一的迁移头：
   - **第 1 批**：**`d050_embedding_metadata_and_dimension`**（预计 `down_revision = "d049"`）
   - **第 2 批**：无数据库迁移
   - **第 3 批**：无数据库迁移
   - **第 4 批**：无数据库迁移
   - **第 5 批**：**`d051_cleanup_legacy_researchdata_mcp_rows`**（预计 `down_revision = "d050"`）

### 6.2 各批次实施范围、三步切换与写实回滚规程

每个批次均严格按 **步骤 A（停止旧入口接新单并切流）→ 步骤 B（保留可回退版本与双指标观察）→ 步骤 C（删除旧组件）** 执行。

#### 第 1 批：宿主平台配置 `DALTON_HOST_PLATFORM`、后端嵌入模块（含维度锁定 `d050`）与 `/jobs/ops` 状态文件化（低风险）

- **数据库迁移**：**`d050_embedding_metadata_and_dimension`**（创建 `dalton_embedding_metadata` 表；Everflow 自动锁定 `gemini-embedding-2-preview` + `3072` 维；支持核心新装按配置维度初始化 `vector(<dim>)` 与 `halfvec(<dim>)` HNSW 索引）。
- **范围**：
  1. 在 `src/dalton/capabilities.py` 实现 `resolve_host_platform()`（读取 `DALTON_HOST_PLATFORM`），为 `CapabilityId.ALPHAENGINE` 接入宿主平台判定；同步更新 `scripts/standalone/init.sh`、`scripts/dalton/bridge_subprocess_env.py` 与 `scripts/dalton/ops/prod/deploy_main.sh` 写入 `DALTON_HOST_PLATFORM`。（注：本批保持 8 个子能力原有的 `depends_on` 关系不动，待各连接器就绪批次逐个解绑。）
  2. 新增 `src/dalton/embeddings/`，改造 `src/dalton/conversations/embedder.py:75-207`、`src/dalton/database/conversations.py:525-558`、`src/dalton/conversations/search.py:250-285` 默认走后端直连嵌入并强制核验 `dalton_embedding_metadata`，移除 `embedder.py:150` 对 `RESEARCHDATA_BRIDGE` 的依赖。
  3. 新增 `scripts/dalton/ops/prod/export_ops_status.py` 与 `com.dalton.ops-status-exporter` plist，改造 `src/dalton/autonomy/system_jobs.py:232-315` 读取 `var/dalton/ops_jobs_status.json`。
- **三步切换与回滚**：
  - **步骤 A（切流）**：加载 `com.dalton.ops-status-exporter` 确认生成首份状态文件；执行数据库备份与 `alembic upgrade d050`；重建并重启 `backend` 容器使新嵌入模块与 `DALTON_HOST_PLATFORM=macos` 生效（本批不涉及沙箱工具 schema 变更，无需重刷运行中沙箱）。
  - **步骤 B（可回退观察）**：旧桥上的 `/embed` 与 `/jobs/ops` 代码保留开启但不再收到后端请求。若后端直连嵌入出现异常，在 `.env` 设置过渡回退开关 `DALTON_EMBED_VIA_BRIDGE=true` / `DALTON_OPS_JOBS_VIA_BRIDGE=true` 并**重启 `backend` 容器**即可恢复走旧桥；若需连同数据库回退发布版本，按 `deploy_main.sh:630-666` 停后端、执行 `alembic downgrade d049` 并重建上一版 `backend` 镜像。
  - **步骤 C（清理）**：连续 14 天满足旧桥 `/embed`、`/jobs/ops` 零命中且后端嵌入归档写入与检索计数正常后，移除回退开关与桥上这两个处理函数。
- **验收条件**：Everflow 生产 `alphaengine` 在容器内 `sys.platform == "linux"` 下保持正常开启，历史 3072 维归档检索结果一致；核心安装以 1536 维模型初始化并跑通纯语义向量检索命中验证。

#### 第 2 批：独立转写连接器服务 `dalton-transcription` 与存量任务排他交接（中低风险）

- **数据库迁移**：无。
- **范围**：
  1. 交付独立服务 `dalton-transcription`（监听 `:8965`，内置 `asr_jobs.py`、`seed_asr_client.py` 与目录排他锁 `var/dalton/asr_jobs/.owner.lock`），在 `docker-compose.core.yml` 增加可选 `transcription` profile，在 Everflow 增加 `com.dalton.transcription` 服务。
  2. 同步修改 `src/dalton/capabilities.py:151-159`（移除 `TRANSCRIPTION` 的 `depends_on=(RESEARCHDATA_BRIDGE,)`，保留 `:425-433` 转写对象存储校验）与 `src/dalton/services/transcriptions.py:461-584`（`_require_transcription_bridge_url` 改为校验 `CapabilityId.TRANSCRIPTION` 并请求 `DALTON_TRANSCRIPTION_SERVICE_URL` + `DALTON_TRANSCRIPTION_TOKEN`）。
- **转写任务交接与三步切换规程**：
  - **步骤 A（单处理方排他交接）**：
    1. 选定无活跃长音频提交的维护窗口，在旧桥配置写入 `DALTON_BRIDGE_ASR_DISABLED=true` 并重启 `com.dalton.researchdata-bridge`——旧桥跳过启动时的 `recover_incomplete_jobs()`（`scripts/dalton/researchdata_bridge.py:26191-26194`），释放对 `var/dalton/asr_jobs/` 的内存锁并对 `/transcriptions*` 返回 `503`；
    2. 启动 `com.dalton.transcription`（`:8965`），挂载同一宿主机目录 `var/dalton/asr_jobs/`（完整保留全部历史任务状态 JSON、`/result/raw` 原始逐字稿文件及 `asr_usage.jsonl` 用量账本），获取 `.owner.lock` 文件排他锁后执行唯一的 `recover_incomplete_jobs()` 接续未完成任务；
    3. 更新 `.env` 中 `DALTON_TRANSCRIPTION_SERVICE_URL` 并**重启 `backend` 容器**（`dalton_asr.py` 走后端 API，无需刷新沙箱）。
  - **步骤 B（可回退观察）**：旧桥转写代码保留但处于 `DALTON_BRIDGE_ASR_DISABLED=true` 静默态。如需回退：先停止 `com.dalton.transcription`（释放 `.owner.lock`），再将旧桥 `DALTON_BRIDGE_ASR_DISABLED=false` 重启接管目录，最后将后端切回旧桥 URL 并重启 `backend` 容器，确保任一时刻全机仅有一个进程恢复和提交火山引擎付费任务。
  - **步骤 C（清理）**：核验历史任务查询、`/result/raw` 流式下载、新建/取消/重试任务及 `asr_usage.jsonl` 记账正常且旧桥转写端点 14 天零请求后，删除桥内转写路由代码。
- **验收条件**：切流前创建的历史已完成任务与进行中任务在切流后通过 `/api/v1/dalton/transcriptions/{id}` 及 `/result/raw` 均返回 200；核心安装在配置转写专用存储后可独立启用 `transcription`。

#### 第 3 批：公开插件扩展（`dalton_sec` 全操作、`dalton_macro`）与五路内部研究连接器 + 跨库初筛上线（中等风险）

- **数据库迁移**：无。
- **范围**：
  1. 公开仓库扩展 `plugins/dalton_sec/` 覆盖原 `sec` 全部 11 个操作，新增 `plugins/dalton_macro/` 覆盖 `fred` 与 `treasury`（TGA/债务），更新 `plugins/langalpha_market_data/fundamentals_mcp_server.py:845-858` 文档串。
  2. 部署宿主机 `com.dalton.research-host`（`:8951`），上线 `dalton-guidepoint`、`dalton-company-wiki`、`dalton-sales-notes`、`dalton-tmtb`、`dalton-funda-ai`、`dalton-research-all`（含 `all` 检索、`read` 前缀分派与 `sales-mail`）六个连接器及 `:8951/ingest/{wiki,sales-notes}` 受控写口；扩展 `src/ptc_agent/config/plugins.py:100-125` 支持 `DALTON_EXTRA_PLUGINS_DIR`。
  3. **同批改完全部关联能力入口后解绑依赖**：更新 `MCP_SERVER_CAPABILITY_MAP`、`BUNDLED_PLUGIN_CAPABILITY_MAP`、`BRIDGE_ROUTE_CAPABILITY_MAP`（含 `sales-mail`）、`src/dalton/provenance/declared_sources.py:13`、`src/dalton/tools/__init__.py:393-404`（`ingest_research` 改挂 `company_wiki | sales_notes`）、`web/src/pages/ChatAgent/components/dalton/AutoIngestToggle.tsx:26`、`src/dalton/data_map/builder.py:287-355`、`src/dalton/content_assembly.py:240-253,281-353`，随后移除 `guidepoint`、`company_wiki`、`sales_notes`、`tmtb`、`funda_ai` 五项能力对 `RESEARCHDATA_BRIDGE` 的 `depends_on`。
  4. 上线 `connector_transport.py` 并迁移相关技能脚本与 `workflows/topic-research/workflow.js`（此时 `alphaengine` 仍走旧桥或兼容路由，下一批迁出）。
- **三步切换与回滚**：
  - **步骤 A（切流）**：先停 `com.dalton.guidepoint-mcp-proxy`（`:8943`），启动 `com.dalton.research-host`（`:8951`）独占接管 `guidepoint_mcp_tokens.json` 唯一刷新写权与本地库访问（并将旧桥的 Guidepoint 上游临时指向 `:8951` 以保证过渡期旧桥回退同样可用且共享单一刷新方）；重新构建前端静态资源（因 `AutoIngestToggle.tsx` 改动）、**重建并重启 `backend` 容器**，并对所有运行中的活跃工作区沙箱触发资产重同步（刷新 `mcp_client_config.json` 与 `tools/dalton_*.py` 包装器）。
  - **步骤 B（可回退观察）**：旧桥与 `:8943` plist 模板保留在盘。如需回退：恢复上一版发布代码与配置，停 `:8951` Guidepoint 模块并重新 `launchctl bootstrap` 拉起 `com.dalton.guidepoint-mcp-proxy`，重建 `backend`/前端容器并重刷活跃沙箱包装器。
  - **步骤 C（清理）**：双指标观察 14 天并实跑全部周频消费者通过后，移除 `com.dalton.guidepoint-mcp-proxy.plist.template` 及旧桥上对应子路由。
- **验收条件**：核心安装单独开启 `company_wiki` 时，`<data_map>`、`AutoIngestToggle`、`ingest_company_wiki`、`tools.dalton_company_wiki` 读写与 `dalton_sec` 11 个操作、`dalton_macro` TGA 查询全部贯通；沙箱容器内零商业数据库挂载、零供应商密钥。

#### 第 4 批：`dalton-alphaengine`、`dalton-office`、`dalton-estimates`、`dalton-cnhk`、`dalton-market-regime`、`dalton-community` 全量切流（中高风险）

- **数据库迁移**：无。
- **范围**：
  1. 在宿主机 `com.dalton.research-host`（`:8951`）启用 `/mcp/alphaengine`（含 `transcript`、SQLite v4 搜索缓存、并发限流、熔断与 JSON 当日读文计数）、`/mcp/office`（`tmeet` + `gcal`）、`/mcp/estimates`、`/mcp/cnhk`、`/mcp/market-regime`、`/mcp/community`。
  2. 注册 `CapabilityId.ESTIMATES`、`CapabilityId.CN_HK`、`CapabilityId.COMMUNITY_SOURCES` 及 `tmeet`/`gcal` 能力映射，解除 `alphaengine` 对 `RESEARCHDATA_BRIDGE` 的 `depends_on`。
  3. 完成 3.2 节剩余全部技能脚本、工作流与全部 `SKILL.md` 细粒度 `DALTON_CAP` 标签的迁移，下线 `plugins/dalton_data/` 默认挂载。
- **单一缓存与配额持有方切换及回滚规程**：
  - **步骤 A（切流）**：为确保 AlphaEngine 并发上限、SQLite v4 搜索缓存与 `cfg.usage_file` 当日读文计数只有唯一写入方，在旧桥设置 `DALTON_BRIDGE_MCP_DISABLED=true` 重启旧桥（停用旧桥 `/mcp` 入口，释放 `alphaengine_search_cache.sqlite3` 与 `usage_file` 写锁），同窗口启用 `:8951/mcp/alphaengine` 接管同一份缓存库与 `usage_file`；**重建并重启 `backend` 容器**，重刷全部已有工作区沙箱使 `tools.dalton_alphaengine` 等包装器生效并清理旧 `tools.researchdata` 包装器。
  - **步骤 B（可回退观察）**：如需回退，先停 `:8951` AlphaEngine 模块释放缓存与计数文件写锁，将旧桥 `DALTON_BRIDGE_MCP_DISABLED=false` 重启，回退后端配置/代码版本重建 `backend` 并重刷沙箱恢复 `tools.researchdata`。
  - **步骤 C（清理）**：连续 14 天旧桥 `/mcp` 零命中且新连接器成功调用量、缓存命中率、全部周报与财报技能实跑通过后，删除桥内全部 `/mcp` 处理代码与 `plugins/dalton_data/` 目录。
- **验收条件**：全仓静态门控测试确认业务技能与工作流零残留 `tools.researchdata` 调用；Everflow Mac 生产跑通 AlphaEngine 全 9 个操作（含 `auth_status`、`list_filters` 公司/自选股词典）与 `transcript` 续查。

#### 第 5 批：宿主任务守护进程剥离、桥缩减为纯通知/飞书过渡代理与遗留行清理（收官批，与第 ③ 阶段解耦）

- **数据库迁移**：**`d051_cleanup_legacy_researchdata_mcp_rows`**（幂等删除 `user_mcp_servers`、`workspace_mcp_servers` 与工具 schema 缓存表中历史遗留的 `researchdata` 记录；`downgrade()` 为无副作用空操作或按备份恢复）。
- **范围**：
  1. `/internal/{market-digest,ai-finance}/*` 切入 `com.dalton.research-host`（`:8951`）管理口。
  2. 上线 `scripts/dalton/ops/prod/host_task_daemon.py`（`com.dalton.host-task-daemon`，监听 `127.0.0.1:8952`），完整承接 `/internal/data-tasks/{host-run,host-kill}`。
  3. **将 `scripts/dalton/researchdata_bridge.py` 缩减为纯通知与飞书/Discord 过渡代理**（仅保留 `/healthz`、`/notify`、`/feishu/*` 8 个子路径及 `discord-history`，待第 ③ 阶段渠道网关就绪后再整体下线 `:8960`）。
  4. 更新 `scripts/dalton/ops/prod/deploy_main.sh:633,658,662,915-924` 与 `scripts/dalton/ops/research_runtime_migrate.sh:806-812` 管理 `com.dalton.research-host`、`com.dalton.host-task-daemon` 与缩减桥。
- **宿主任务守护进程交接与三步切换规程**：
  - **步骤 A（单执行方受控交接）**：
    1. 检查 `.dalton-host-runner-state.json` 确认当前 `active_runs` 为空（避开 `T1`/`T29`/`T58`/`T34`/`T60`/`T61`/`T87` 排程时刻）；
    2. 在旧桥设置 `DALTON_BRIDGE_HOST_TASKS_DISABLED=true` 重启旧桥，使其跳过启动时的 `recover_host_runner_state_on_startup()`（`scripts/dalton/researchdata_bridge.py:26196-26199`）并拒绝新 `host-run`；
    3. 启动 `com.dalton.host-task-daemon`（`:8952`），获取 `.dalton-host-runner.owner.lock` 排他锁后加载原 `.dalton-host-runner-state.json`（保留单调递增的 `max_seen_generation`，拦截旧代次重放，见 `scripts/dalton/researchdata_bridge.py:19168-19380,20049-20056`），启用独立令牌 `DALTON_HOST_RUNNER_TOKEN`、命令白名单 `resolve_host_run_command_spec`、`BBG_HOST_JOB_KEYS` 专属 `mirror` 校验及 `scripts/dalton/bridge_subprocess_env.py:385-420` 按任务最小化凭据注入；
    4. 执行 `alembic upgrade d051`，切换后端 `DALTON_HOST_RUNNER_URL` 并**重启 `backend` 容器**。
  - **步骤 B（可回退观察）**：回退时先确认 `:8952` 无运行中 `pgid`，停止 `com.dalton.host-task-daemon` 释放 `.owner.lock`，再开启旧桥宿主任务开关并切回后端配置重启 `backend`。
  - **步骤 C（清理）**：实跑核验 BBG 拉取、市场摘要与 AI Finance 日报连续 14 天稳定且旧桥内部任务端点零请求后，完成桥代码瘦身定稿。
- **验收条件**：第 5 批在不依赖第 ③ 阶段渠道网关改动的条件下独立合并上线；飞书文档与卡片通知经缩减桥照常工作，研究检索、嵌入、转写、宿主定时任务全部在各自独立组件稳定运行。

---

## 7. 测试

### 7.1 单元测试与静态门控测试（每次提交必跑，`uv run pytest tests/unit/`）

1. **宿主平台判定与容器环境解耦测试（`tests/unit/dalton/test_core_capabilities_gate.py:244-540`）**：
   - 在 `monkeypatch.setattr(sys, "platform", "linux")` 条件下（模拟 Linux Docker 后端容器），断言 `DALTON_HOST_PLATFORM=macos` 时 `alphaengine` 正常开启、`DALTON_HOST_PLATFORM=linux` 时即使设置 `DALTON_CAP_ALPHAENGINE=true` 仍解析为关闭；再次在 `sys.platform == "darwin"` 下运行同一组断言，证明容器内 `sys.platform` 对结果零影响。
2. **返回结构、分页与溯源契约测试（新增 `tests/unit/dalton/test_connector_envelope_contract.py` + 扩展 `tests/unit/dalton/test_declared_sources.py:1-120`）**：
   - 直接调用 `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:946-1055` 的 `_extract_route_page_info` 与 `_extract_route_items_with_page_info` 校验每个连接器响应样例，断言 `cursor`、`has_more`、`total_upstream`、`returned` 准确提取，断言 `dalton_research_all` 保留 `alphaengine`、`guidepoint`、`source_status` 及按前缀 `read` 分派。
   - 校验 `dalton_company_wiki.list` 接收 `params.indexed_since`、`dalton_alphaengine` 包含 `auth_status` 及 `list_filters` 的 `companies`(`params.query`)/`watchlist_stocks`(`params.group_id`) 参数校验。
   - 用 `expand_declared_sources`（`src/dalton/provenance/declared_sources.py:81-120`）校验全部 14 个连接器服务名的 `_sources` 展开，并断言任何连接器响应均不同时携带空 `_declared_sources: []` 与非空 `_sources`。
3. **全仓 `researchdata` 调用点静态归宿门控测试（新增 `tests/unit/dalton/test_researchdata_callers_accounted.py`）**：
   - 静态扫描 `plugins/`、`workflows/`、`src/`、`web/src/`、`scripts/` 中所有 `tools.researchdata`、`researchdata.`、`DALTON_CAP:researchdata_bridge`、`:8960` 引用：在第 1–3 批对照显式待迁白名单防新增耦合；第 4–5 批收官后断言除缩减桥保留的飞书/通知过渡模块外，业务代码与技能提示词零残留旧桥研究调用。
4. **凭据与原始商业库不进沙箱门控测试（扩展 `tests/unit/dalton/test_core_capabilities_gate.py:2144-2378`）**：
   - 构造启用全部商业连接器（`DALTON_EXTRA_PLUGINS_DIR` 挂载测试包）的沙箱配置，断言 Docker `host_config["Binds"]`（`src/ptc_agent/core/sandbox/providers/docker.py:794-804`）不包含任何宿主机 SQLite 库或 `<DALTON_RESEARCH_HOME>` 目录，且沙箱环境变量与 `.agents/tools/` 配置文件不包含 `GUIDEPOINT_CLIENT_SECRET`、`GUIDEPOINT_REFRESH_TOKEN`、`GEMINI_API_KEY`、`SEED_ASR_*`、`FEISHU_APP_SECRET`。
5. **嵌入维度锁定与防混写单测（扩展 `tests/unit/dalton/test_embedder_retry.py:1-105` + 新增元数据单测）**：
   - 验证 `dalton_embedding_metadata` 在模型名或维度与锁定值存在差异时立即抛出配置异常，且 `src/dalton/conversations/search.py:262-275` 将该异常直接向上传递；验证 `d050` 在已有 3072 维表上保持原模型与 `RETRIEVAL_DOCUMENT` / `RETRIEVAL_QUERY` 任务类型不变。
6. **转写排他恢复与宿主任务状态机单测（扩展 `tests/unit/dalton/test_transcriptions.py:1-120`、`tests/unit/dalton/test_transcription_master_switch.py:1-100` 及宿主执行器单测）**：
   - 验证 `.owner.lock` 阻止双进程并发 `recover_incomplete_jobs()`，覆盖旧任务查询、取消、重试与 `/result/raw` 下载；验证 `host_task_daemon.py` 对 `stale_generation`（409）、`fence_lost`（409）、遗留 `pgid` 清理、命令白名单拒绝与任务执行中途重启/取消的处理。

### 7.2 GitHub Actions 从零安装与升级集成测试（`.github/workflows/standalone-core-e2e.yml`）

在零密钥 Ubuntu Runner（`.github/workflows/standalone-core-e2e.yml:52-176`）上由 `scripts/standalone/` 驱动两类端到端验证：

1. **每批纯净发布包从零启动检查（扩展 `tests/standalone/checks/b2_backend_capabilities.py` 与 `b3_skills_prompts_ui.py`）**：
   - `package_release.sh` 导出后验证 `DALTON_EXTRA_PLUGINS_DIR` 缺失或未设置时后端与沙箱干净启动；验证 `init.sh` 自动写入 `DALTON_HOST_PLATFORM=linux` 且 `alphaengine` 保持关闭。
   - **1536 维向量检索真实命中检查**：在 `tests/fakes/fake_openai_server.py` 提供返回确定性 1536 维正交向量的 `/v1/embeddings` 端点，初始化核心新装库并写入一条正文字面与查询词零重叠的对话卡片，调用混合检索断言 `dense_hits` 命中该卡片且 `dense_score > 0`。
   - **公开连接器与独立能力检查**：验证 `dalton_sec` 扩展操作与 `dalton_macro` 在沙箱 `ExecuteCode` 内可调用并产出 `prov:` 引用；开启 `company_wiki`（连接测试容器内开源 `wiki-service`）验证 `AutoIngestToggle`、`ingest_company_wiki` 与 `tools.dalton_company_wiki.list(params={"indexed_since": ...})` 端到端闭环；开启假桩转写服务与对象存储配置验证 `transcription` 全流程。
2. **每批从上一版升级与沙箱重装配检查（扩展 `tests/standalone/checks/b6_tls_backup_upgrade.py`）**：
   - 基于上一版数据库快照与已存在的运行中沙箱工作区执行升级（运行 `alembic upgrade head`），验证：① 既有沙箱重新连接时自动更新 `mcp_client_config.json` 与 `tools/` 包装模块；② 当某项能力在升级后被关闭时，即便沙箱脚本尝试请求旧桥路径也被路由门拒绝（返回 `capability_disabled`）。

### 7.3 Everflow 生产 Mac 实机验证与回滚演练

由 `scripts/dalton/ops/prod/` 下的验收脚本在 Everflow Mac 宿主上逐批执行：

1. **宿主机直连与调用方签名验签**：从 Colima 沙箱容器发起 `tools.dalton_alphaengine`、`tools.dalton_guidepoint` 等全部连接器调用，核验 `http://host.docker.internal:8951` 可达、`X-Dalton-Caller-Token` 验签通过且工作区/线程归属准确落入配额日志，`/healthz` JSON 当日读文计数准确累加。
2. **真实商业源与原采集脚本全量实跑**：在生产工作区实跑 `dalton-thesis-delta`、`dalton-bullbear-tracker`（含 `dalton_research_all.read` 全文分页）、`dalton-mark-weekly-newenergy`、`dalton-richard-coverage-weekly`、`dalton-humanoid-robot-weekly`、`dalton-tmeet-weekly-minutes`、`dalton-richard-earnings` 与 `T1` BBG 宿主任务，对比切流前后产物完整性。
3. **每批上线前预演回滚**：在每批正式观察期开始前，按 6.2 节的步骤 B 实操演练一次回退与再切回（含第 2 批转写 `.owner.lock` 交接回退、第 3 批 Guidepoint 代理恢复、第 4 批 AlphaEngine 缓存写锁交接、第 5 批宿主任务 `.dalton-host-runner-state.json` 代次衔接），确认回退耗时与步骤全部受控。

---

## 8. 风险与待用户决定的事项

### 8.1 主要技术风险与应对措施

| 风险点 | 影响说明 | 应对措施 |
|---|---|---|
| 已有长生命周期工作区沙箱缓存旧包装器模块 | 升级后若活跃沙箱未重同步 `.agents/tools/`，可能继续导入已下线的 `tools.researchdata` | 在每批步骤 A 切流后主动触发既有沙箱 `reconnect` 资产同步（`src/ptc_agent/core/sandbox/assets.py` 根据 `mcp_servers` 指纹自动覆盖包装器并清理失效文件），并在升级检查中专项断言 |
| `sales-notes`、`tmtb` 与 `market-digest` 共用 `market-digest/vectors.db` 的并发读写 | 入库、TMTB 同步与检索并发访问同一宿主机 SQLite 库 | 统一收口于单实例 `dalton-research-host`（`:8951`）宿主机进程内，宿主机文件系统本地读写正常支持 WAL + `busy_timeout=5000` 及单写锁队列，并在查询层强制绑定来源路由过滤条件 |
| 转写或宿主任务切换瞬间双进程并发恢复 | 新旧守护进程若同时扫描同一状态目录，会重复提交付费 ASR 或重复拉起 BBG 任务 | 强制引入宿主机文件排他锁（`asr_jobs/.owner.lock` 与 `.dalton-host-runner.owner.lock`），严格执行“旧桥置禁用开关重启释放 → 新服务拿锁启动 → 切换后端”的顺序 |
| SEC 11 个操作迁入公开插件 `dalton_sec` 的解析边界差异 | 原 `findata-analyst` 对特殊表单（如 6-K/20-F 附件与 Item 切分）有定制规则 | 第 3 批上线前用 Everflow 覆盖池全部美股及中概 ADR 近 4 个季度的真实 10-K/10-Q/8-K/6-K/20-F/Form 4 样本对新旧实现做离线回归比对，对齐输出字段后再切流 |

### 8.2 待用户拍板的事项

无（第 1 轮提出的私有插件目录交付、嵌入维度策略、跨库初筛归属、长尾子路由拆分及分批解耦等事项均已拍板并完整落入本文 §0–§7）。
