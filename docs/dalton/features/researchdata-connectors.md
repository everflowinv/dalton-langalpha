# 研究数据桥按职责拆分为连接器（独立核心版本第 ② 阶段）

> 状态：第 3 轮修订定稿（2026-11-24）
> 前置文档：[standalone-core.md](./standalone-core.md)、[alphaengine-cache.md](./alphaengine-cache.md)、[data-jobs.md](./data-jobs.md)
> 关联代码：`scripts/dalton/researchdata_bridge.py`、`src/dalton/capabilities.py`、`src/dalton/content_assembly.py`、`plugins/dalton_sec/`、`plugins/dalton_data/`

## 0. 文档定位与既定架构决定

### 0.1 文档定位说明

本文是研究数据桥按职责拆分与渐进演进的**总体方向与分批计划**。每一批正式开发前会另写独立的批次开发任务书，由开发任务书给出该批逐操作的字段级接口定义、精确切换与回退命令行，并单独经过审查。

本文对每一批明确规定四项核心契约：**实施范围**、**依赖的前置条件**、**切换与回退的步骤顺序**（何时停止接单、何时交接排他锁、何时执行 Dalton 数据库迁移、何时重建容器、何时重刷运行中沙箱），以及**验收条件**；逐字段接口细节与具体运维脚本命令在本文标注到“该批开发任务书须定义 X”的粒度。

### 0.2 既定架构决定（E1–E17 汇总）

宿主机单进程服务 `scripts/dalton/researchdata_bridge.py`（共 26,636 行，默认监听 `127.0.0.1:8960`）目前集中承载内部研究检索、金融与宏观扩展路由、向量嵌入、语音转写、飞书通知与文档代理、宿主定时任务执行及生产运维状态巡检。在独立核心版本第 ① 阶段（`docs/dalton/features/standalone-core.md:54-92`）中，`alphaengine`、`guidepoint`、`company_wiki`、`sales_notes`、`tmtb`、`funda_ai`、`feishu_docs`、`transcription` 八项能力均通过 `depends_on=(CapabilityId.RESEARCHDATA_BRIDGE,)` 绑定在总开关 `researchdata_bridge` 下（`src/dalton/capabilities.py:95-159`）。本阶段按以下既定决定完成拆分：

1. **宿主平台由部署配置显式传入，Everflow 固定运行于 macOS 宿主**：AlphaEngine 连接器仅面向 macOS 设计，继续复用 macOS 钥匙串（`security` CLI）与 `/Applications/AlphaEngine.app` 桌面登录态。新增部署配置 `DALTON_HOST_PLATFORM`（`macos` | `linux`），Everflow 由 `scripts/dalton/ops/prod/` 部署流程固定写入 `macos`，核心安装由 `scripts/standalone/init.sh` 按实际宿主 `uname -s` 写入；`src/dalton/capabilities.py` 读取该配置判定 `alphaengine` 可用性，容器内 `sys.platform` 对结果零影响。
2. **商业数据与供应商凭据留在宿主机侧，三类凭据严格分离**：内部研究检索统一由宿主机服务提供，商业数据库文件、原始文档目录与供应商密钥全部保留在宿主机进程内，令牌刷新由宿主机唯一持有方串行写盘。全链路严格区分三类凭据：① **沙箱连接器调用令牌**（仅访问已启用 `/mcp/<connector>`）；② **每次请求调用方身份签名**（`X-Dalton-Caller-Token`，仅用于并发公平排队与配额归属）；③ **后端独立管理凭据**（仅存于 `backend` 容器，专用于 `/ingest/*`、`/internal/*`、`/transcriptions*`、`/notify`、`/feishu/*`）。
3. **连接器采用 Agent Plugins 1.0.0 规范，同一公开声明按部署形态装配地址**：每个连接器独立为一个插件包（`plugin.json` + `mcp.json` + 技能文件）。公开数据（SEC 11 项操作、财政部 TGA/债务与 FRED）放入公开仓库 `stdio` 插件；核心安装可用的服务型连接器（公司 Wiki、跨库初筛 `all`、转写）在公开仓库提供插件声明与开源服务端，在核心安装中接入 `sandbox-net` 通过 Compose 服务名访问，在 Everflow 中通过 `host.docker.internal` 访问，连接地址在插件配置装配入口按部署形态注入。商业实现存放在私有仓库，缺少私有实现的核心安装对应能力默认关闭。
4. **跨库初筛保留在宿主机侧，全入口逐来源检查有效能力**：`research(route="all")` 与 `sales-mail` 由宿主机研究服务聚合，保留按文档 ID 前缀（含 `wiki:`、`mail:`、`digest:`、`tmtb:`、`funda:`、`image:`）读取全文与部分失败状态返回；旧桥、新研究服务、`all` 与 `sales-mail` 均读取同一份有效能力配置逐来源做权限检查，单次联搜或全文读取只访问已开启的来源库。
5. **嵌入模块沿用现有客户端行为并分离存储标签与请求模型名**：`dalton_embedding_metadata` 分开记录库内标签（Everflow 保持 `gemini-embedding-2-preview`）与实际 API 请求模型名（`gemini-embedding-2`，对齐 `scripts/dalton/research_runtime/skills/company-wiki/scripts/company_wiki_cli.py:54-61`），保留每段文本构造独立 `Content` 的批量请求行为（`scripts/dalton/research_runtime/skills/company-wiki/scripts/company_wiki_cli.py:219-225`）。核心新装按所选模型维度建库；已写入 1536 维数据的实例不支持原地回退至只认 3072 维的旧版本，跨版本回退前须使用升级前备份恢复数据库。
6. **每批任一时刻仅有一个权威处理方，排他锁前置到管理器构造之前**：第 3 批的 `all` 通过当时唯一的 AlphaEngine 处理方（旧桥）检索 AlphaEngine；转写与宿主任务的新旧进程在构造管理器、恢复未完成状态及开始接单前均须先持有同一把排他文件锁，切换与回退严格遵循“先停止接单 → 确认旧任务结束或已交接 → 新处理方拿锁启动 → 重建后端容器”。
7. **渠道与 `discord-history` 统一留待第 ③ 阶段接手**：`/notify`、`/feishu/*` 及受限 `/mcp` 下的 `discord-history`（支撑 `wiki_ingest.py` 消息分页与附件链接刷新，沙箱侧配专属受限插件 `plugins/dalton_discord_history/`）在第 4、5 批继续保留在缩减后的桥进程上，使第 ② 阶段收官与第 ③ 阶段 `integrations/dalton-channel-gateway/` 完全解耦。

---

## 1. 现状清单

### 1.1 HTTP 顶层路由组与外围配置清单

`scripts/dalton/researchdata_bridge.py` 在 `_handle_client`（`scripts/dalton/researchdata_bridge.py:25344-26167`）中分发全部 HTTP 请求。`/healthz` 免鉴权；`/mcp` 校验 `DALTON_BRIDGE_TOKEN`（`_is_valid_mcp_token` 于 `scripts/dalton/researchdata_bridge.py:25301-25313`）；其余全部管理与内部路由统一在 `:25622` 经 `_require_admin_token`（`scripts/dalton/researchdata_bridge.py:25316-25342`）校验 `DALTON_BRIDGE_ADMIN_TOKEN`。（注：下表引用的 `<DALTON_RESEARCH_CODE>`、`<DALTON_RESEARCH_HOME>`、`~/Library/`、`/Applications/AlphaEngine.app` 等仓库外宿主机路径取自桥配置常量，本机实际排布在批次开发前于宿主终端核对。）

| 路由前缀与方法 | 代码入口 | 职责说明 | 调用方（后端模块 / 沙箱技能 / 运维脚本） | 依赖的本机资源 | 当前对应能力 |
|---|---|---|---|---|---|
| `GET /healthz` | `scripts/dalton/researchdata_bridge.py:25419-25453` | 返回服务版本、并发限流器状态、从 JSON 文件读取的 AlphaEngine 当日读文计数（`_read_usage_today` 于 `:10897-10922`）、搜索熔断器状态、`xreach` 与 `tmeet` CLI 可用性 | `scripts/dalton/check_bridge.sh:41-45`、`scripts/dalton/install_bridge.sh:51-90`、`scripts/dalton/export_review_window.sh:222`、`scripts/dalton/realrun/check_env.py:835`、`scripts/dalton/realrun/g7_bridge.py:44-110` | `cfg.usage_file`（JSON 当日读文计数）、内存限流与熔断状态、本机 `xreach`/`tmeet` | `researchdata_bridge` |
| `POST /mcp`<br>`POST /mcp/<token>` | `scripts/dalton/researchdata_bridge.py:25456-25619`；分发 `handle_tools_call` 于 `:13541-15264` | Streamable HTTP JSON-RPC 端点，暴露 `catalog`、`research`、`finance`、`macro`、`community` 5 个工具。`:25555-25589` 校验带 `X-Dalton-Caller-Scope: per-request` 的 `X-Dalton-Caller-Token` 用于并发桶与自动化优先级归属；缺失或验签未过归入 `_caller_verified: False` 匿名桶处理 | 1. 沙箱 PTC 运行时与超时配置：`plugins/dalton_data/mcp.json:4-10`、`agent_config.dalton.yaml:51-52`、`src/ptc_agent/core/sandbox/mcp_client_runtime.py:2165-2220`<br>2. 地址注入、系统提示词与配置：`scripts/dalton/bridge_subprocess_env.py:2229-2427`、`src/dalton/settings.py:1090-1151,1419-1435`、`src/ptc_agent/agent/prompts/loader.py:104-107`、`src/ptc_agent/agent/prompts/templates/components/tool_guide.md.j2:23-27`<br>3. 健康探针与数据地图：`src/dalton/data_map/health.py:173-235,790-1060`、`src/dalton/data_map/builder.py:287-355,488-503`<br>4. 自治与运维探针：`src/dalton/autonomy/probes/cnhk.py:184-310`、`scripts/dalton/check_bridge.sh:39-65`、`scripts/dalton/realrun/g4_autonomy.py:412-420`<br>5. 技能脚本与 JS 工作流（见 1.2 与 3.1 节） | 本机 `:8950`、`:8943`、`<DALTON_RESEARCH_HOME>` SQLite 库与本机 CLI | 总开关 `researchdata_bridge` + `BRIDGE_ROUTE_CAPABILITY_MAP`（`src/dalton/capabilities.py:775-801`） |
| `POST /embed` | `scripts/dalton/researchdata_bridge.py:25626-25642`；`handle_embed` 于 `:15419-15489` | 调 `company_wiki_cli.py` 子进程（库内标签 `gemini-embedding-2-preview` 映射为 API 模型 `gemini-embedding-2`，每段文本独立 `Content` 批量请求，3072 维，见 `scripts/dalton/research_runtime/skills/company-wiki/scripts/company_wiki_cli.py:54-61,205-254`） | 1. 对话归档嵌入：`src/dalton/conversations/embedder.py:75-207`，供 `src/dalton/conversations/search.py:259` 调用<br>2. 归档重渲染环境强校验：`scripts/dalton/bridge_subprocess_env.py:29,881,1437-1447,1946-1950` | `company_wiki_cli.py`、宿主机 `GEMINI_API_KEY` | `src/dalton/conversations/embedder.py:150-151` 强校验 `researchdata_bridge`，业务归属 `conversation_archive` |
| `GET /jobs/ops` | `scripts/dalton/researchdata_bridge.py:25645-25651`；`handle_get_ops_jobs` 于 `:15766-15794` | 读取 macOS `launchctl` 与备份日志，汇总 `com.dalton.pg-backup` 与 `com.dalton.backend-watchdog` 状态 | 后端系统任务模块：`src/dalton/autonomy/system_jobs.py:232-315`；提示词说明门控：`src/ptc_agent/agent/prompts/templates/components/chief_of_staff.md.j2:58`、`src/ptc_agent/agent/prompts/templates/components/user_profile.md.j2:92` | macOS `/bin/launchctl`、`~/Library/LaunchAgents/*.plist`、`~/Library/Logs/dalton-pg-backup.log`、`~/Backups/dalton/.watchdog_failures` | `researchdata_bridge` |
| `POST /notify` | `scripts/dalton/researchdata_bridge.py:25659-25675`；`handle_notify` 于 `:15796-15983` | 经环境清洗（`scripts/dalton/bridge_subprocess_env.py:423-452`）调用 `feishu_send_card.py` 发送飞书卡片 | `src/dalton/notify/dispatch.py:1985-2080,5148`（受 `DALTON_NOTIFY_VIA_GATEWAY` 控制，见 `src/dalton/settings.py:1473-1486`） | `feishu_send_card.py`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET` | `channel_feishu` + `researchdata_bridge` |
| `POST /wiki/ingest` | `scripts/dalton/researchdata_bridge.py:25679-25699`；`handle_wiki_ingest` 于 `:17050-17850` | 校验 Markdown、幂等去重、加锁并调用 `company-wiki/run.sh` 写入 Wiki 文件、更新 `wiki/vectors.db` 与审计日志 | 1. `src/dalton/tools/wiki_tools.py:447-485,676`（注册于 `src/dalton/tools/__init__.py:370-380`）<br>2. `src/dalton/tools/ingest_tools.py:615,661-677`（`ingest_research` 注册于 `src/dalton/tools/__init__.py:393-404`）<br>3. 前端开关：`web/src/pages/ChatAgent/components/dalton/AutoIngestToggle.tsx:26` | `<DALTON_RESEARCH_HOME>/wiki/`、`wiki/vectors.db`、`DALTON_WIKI_INGEST_AUDIT_FILE`（`:1469-1471`）、`GEMINI_API_KEY` | `ingest_company_wiki` 挂 `company_wiki`；`ingest_research` 与前端按钮挂 `researchdata_bridge` |
| `POST /sales-notes/ingest` | `scripts/dalton/researchdata_bridge.py:25703-25723`；`handle_sales_notes_ingest` 于 `:18052-18430` | 校验销售纪要并调用 `market-digest/run.sh add-note` 写入清单与 `skills/market-digest/vectors.db` | `src/dalton/tools/ingest_tools.py:72-105,261,578-594`（注册于 `src/dalton/tools/__init__.py:381-404`） | `state/sales-notes/manifest.jsonl`、`skills/market-digest/vectors.db`、`GEMINI_API_KEY` | `ingest_sales_note` 挂 `sales_notes`；`ingest_research` 挂 `researchdata_bridge` |
| `POST /feishu/*`（8 个子路径） | `scripts/dalton/researchdata_bridge.py:25728-25768`；处理函数 `:18469-19024`；`FeishuClient` 于 `:22528-23369` | 代理飞书接口：`doc/read`、`wiki/nodes`、`wiki/node`、`doc/create`、`doc/append`、`doc/grant`、`bot-info`、`chats` | `src/dalton/feishu.py:106-167`（`:117-118` 校验 `RESEARCHDATA_BRIDGE`）、`src/dalton/tools/feishu_tools.py:69-81,236,346,373,401,521,684`、`src/dalton/app/router.py:2526,2563`、`src/dalton/notify/groups.py:28-56` | `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、飞书 OpenAPI | 工具注册挂 `feishu_docs` / `channel_feishu`，客户端挂 `researchdata_bridge` |
| `POST /transcriptions`<br>`GET /transcriptions/{id}`<br>`GET /transcriptions/{id}/result`<br>`GET /transcriptions/{id}/result/raw`<br>`POST /transcriptions/{id}/retry`<br>`POST /transcriptions/{id}/cancel` | `scripts/dalton/researchdata_bridge.py:25773-25890`；构造器 `_asr_job_manager` 于 `:19039-19065`；启动恢复 `:26191-26194`；引擎 `scripts/dalton/asr_jobs.py:2295-2345` | `ffprobe`/`ffmpeg` 切分转码、火山引擎 TOS 上传与 Seed-ASR 2.0 转写；`AsrJobManager.__init__` 在运行事件循环中构造时会自动调用 `recover_incomplete_jobs()`（`scripts/dalton/asr_jobs.py:2321-2325`） | `src/dalton/services/transcriptions.py:461-584,2038-2075`（支撑 `src/dalton/app/transcriptions.py:100`、`src/dalton/tools/transcription_tools.py:410`、`src/server/app/setup.py:909-919`）；沙箱脚本经后端 API 调用：`plugins/dalton/skills/dalton-transcript-polish/scripts/dalton_asr.py:49,382-588` | 本机 `ffmpeg`/`ffprobe`、`DALTON_ASR_JOBS_DIR`（`scripts/dalton/researchdata_bridge.py:1477-1479`）、`DALTON_ASR_USAGE_FILE`（`scripts/dalton/asr_jobs.py:2301-2306`）、`VOLC_TOS_*` 与 `SEED_ASR_*` | `transcription`（同时受 `src/dalton/capabilities.py:425-433` 转写对象存储配置约束） |
| `POST /internal/market-digest/{prepare,read,output/get,output/save,output/finalize}` | `scripts/dalton/researchdata_bridge.py:25894-25954,26082-26112`；处理函数 `:19770-19974` | 每日市场摘要批次材料准备、读取、草稿保存与定稿 | `src/dalton/tools/market_digest_tools.py:42-43,286-289`、`src/dalton/tools/market_digest_output_tools.py:471-480`（注册于 `src/dalton/tools/__init__.py:94-113`） | `<DALTON_RESEARCH_HOME>/skills/market-digest/`、邮件/电报拉取环境 | `fund_automations` |
| `POST /internal/ai-finance/{prepare,read,signals,report}` | `scripts/dalton/researchdata_bridge.py:25958-26078`；处理函数 `:19531-19769` | AI Finance 日报准备、信号读写与报告提交 | `src/dalton/tools/ai_finance_inputs_tools.py:309`、`src/dalton/tools/ai_finance_signals_tools.py:706`、`src/dalton/tools/ai_finance_report_tools.py:779`（注册于 `src/dalton/tools/__init__.py:114-143`） | `DALTON_AI_FINANCE_CONFIG` 指向的宿主机目录 | `fund_automations` |
| `POST /internal/data-tasks/{host-run,host-kill}` | `scripts/dalton/researchdata_bridge.py:26116-26141`；状态与执行 `:19168-19530,19975-20342`；启动恢复 `:26196-26199` | 受控执行 `src/dalton/data_jobs/manifest.yaml` 中 7 个 `runner: "host"` 任务（`T1` `:5-26`、`T29` `:335-356`、`T58` `:549-577`、`T34` `:871-895`、`T60` `:897-936`、`T61` `:939-964`、`T87` `:1045-1070`），维护 `generation`、`pgid`+`lstart`、`fence_lost`、命令白名单与按任务凭据注入 | `src/dalton/data_jobs/host_runner.py:23-29,108-181` | `var/dalton/openclaw-data`、`.dalton-host-runner-state.json`、SSH Windows BBG 跳板机、本地专属 venv | `data_jobs` |

宿主机生命周期与迁移脚本入口：`scripts/dalton/ops/prod/deploy_main.sh:500-510,633,658,662,915-924`（容器重建/重启、桥 kickstart 与失败回退）、`scripts/dalton/ops/research_runtime_migrate.sh:806-812`（`--check-runtime` 校验），以及宿主机 Guidepoint 直调脚本 `scripts/dalton/research_runtime/scripts/data_gateway.py:40,283` 与 `scripts/dalton/research_runtime/skills/guidepoint-transcript-search/scripts/gp_search.py:13`（当前硬编码 `http://127.0.0.1:8943/mcp`）。

### 1.2 `/mcp` 内部工具与全部子路由清单

合法子路由定义在 `TOOL_VALID_ROUTES`（`scripts/dalton/researchdata_bridge.py:3212-3230`），参数与返回结构定义在 `ROUTE_CONTRACT_SPECS`（`scripts/dalton/researchdata_bridge.py:2219-3210`）、`scripts/dalton/research_runtime/scripts/data_gateway.py:115-226` 与 `scripts/dalton/research_runtime/scripts/research_library.py:45-165,435-480`。

| MCP 工具 | `route` 子路由 | 现有操作集合（`operations`） | 代码定义位置 | 主要调用方（精确位置） | 本机资源与外部依赖 | 当前能力状态 |
|---|---|---|---|---|---|---|
| `catalog` | 可选按 `route` 过滤 | 自省返回路由参数契约 | `scripts/dalton/researchdata_bridge.py:2160-2218,13580-13650` | `plugins/dalton/agents/dalton-evidence.md:22`、`plugins/dalton/skills/dalton-bullbear/SKILL.md:29` | 内存契约字典 | `researchdata_bridge` |
| `research` | `all` | `search`、`read`、`list`、`get_document` | `scripts/dalton/researchdata_bridge.py:2460-2516,14042-14422`、`scripts/dalton/research_runtime/scripts/research_library.py:447-480` | 定时采集全文与图片读取：`plugins/dalton/skills/dalton-bullbear-tracker/scripts/collect.py:1410-1418`、`plugins/dalton/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py:600-603`、`plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/sales_notes_search.py:199-207`；交互初筛 `plugins/dalton_data/plugin.json:16` | 本地四库 SQLite + 配图索引（`image:`）+ 并发扇出 `:8950` 与 `:8943` | `src/dalton/data_map/sources.yaml:13-18` 挂 `researchdata_bridge` |
| `research` | `alphaengine` | `search_library`、`get_document`、`get_document_info`、`list_attachments`、`list_recording_tasks`、`get_recording_status`、`get_recording_task`、`list_filters`、`auth_status` | `scripts/dalton/researchdata_bridge.py:2517-2659,13930-14041`、`scripts/dalton/research_runtime/scripts/alphaengine-mcp/server.mjs:1-220` | `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:902-915`、`plugins/dalton/skills/dalton-bullbear-tracker/scripts/collect.py:2733`、`plugins/dalton/skills/dalton-sector-delta/scripts/collect_sector.py:89-90`、`plugins/dalton/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py:610-619`、`plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/richard_cov_transport.py:12-45`、`plugins/dalton/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py:1110-1112`、`plugins/dalton/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py:1143`、`plugins/dalton/skills/dalton-tob-industry-weekly/SKILL.md:81,89`、`workflows/topic-research/workflow.js:165`、`workflows/bullbear-doc/workflow.js:249`、`src/dalton/data_map/health.py:860-920` | 本机 `:8950`、`/Applications/AlphaEngine.app`（`scripts/dalton/research_runtime/scripts/alphaengine-mcp/server.mjs:112`）、钥匙串 `dalton-alphaengine`（`scripts/dalton/research_runtime/scripts/alphaengine-mcp/server.mjs:113-116`）、SQLite v4 缓存库、`cfg.usage_file` | `alphaengine`（`src/dalton/capabilities.py:777`） |
| `research` | `transcript` | `transcript` | `scripts/dalton/researchdata_bridge.py:2704-2738`、`scripts/dalton/research_runtime/scripts/data_gateway.py:43,212-214` | `plugins/dalton/skills/dalton-earnings-flash/SKILL.md:42`、`plugins/dalton/skills/dalton-full-coverage/references/branch-c-primary.md:21`、`plugins/dalton/skills/dalton-ai-app-weekly-monitor/SKILL.md:201-204` | 与 `alphaengine` 共用 `:8950`、缓存库与熔断器 | `alphaengine`（`src/dalton/capabilities.py:778`） |
| `research` | `guidepoint` | `search_library` | `scripts/dalton/researchdata_bridge.py:2660-2703`、`scripts/dalton/research_runtime/scripts/guidepoint_mcp_proxy.py:1-217` | `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:890-901`、`plugins/dalton/skills/dalton-humanoid-robot-weekly/scripts/humanoid_robot_weekly.py:148-159`、`plugins/dalton/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py:1113-1115`、`plugins/dalton/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py:1144`、`plugins/dalton/skills/dalton-tob-industry-weekly/SKILL.md:94`、`workflows/topic-research/workflow.js:186-191`、`src/dalton/data_map/health.py:921-965`、`scripts/dalton/research_runtime/skills/guidepoint-transcript-search/scripts/gp_search.py:13` | 本机 `:8943`、`guidepoint_mcp_tokens.json`（进程内锁刷新写盘，`:57,99`）、`GUIDEPOINT_CLIENT_ID`/`SECRET` | `guidepoint`（`src/dalton/capabilities.py:779`） |
| `research` | `company-wiki` | `search`、`read`、`list`、`topic` | `scripts/dalton/researchdata_bridge.py:2739-2791`、`scripts/dalton/research_runtime/scripts/research_library.py:56,62,162,170` | `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:880-889`（传 `indexed_since`）、`plugins/dalton/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py:590-595`、`workflows/topic-research/workflow.js:181-185`、`src/dalton/data_map/health.py:795-859` | `<DALTON_RESEARCH_HOME>/wiki/`、`wiki/vectors.db`、`state/research-images/assets`、`GEMINI_API_KEY` | `company_wiki`（`src/dalton/capabilities.py:780`） |
| `research` | `sales-notes` | `search`、`read`、`list` | `scripts/dalton/researchdata_bridge.py:2792-2837`、`scripts/dalton/research_runtime/scripts/sales_notes_store.py:18-78` | `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:916-928`、`plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/sales_notes_search.py:186-210`、`plugins/dalton/skills/dalton-mark-weekly-newenergy/scripts/rd_transport.py:50-115`、`workflows/topic-research/workflow.js:183` | `skills/market-digest/vectors.db`（与 `tmtb`、`market-digest` 共用，见 `scripts/dalton/research_runtime/scripts/research_library.py:172`）、`state/sales-notes/manifest.jsonl` | `sales_notes`（`src/dalton/capabilities.py:781`） |
| `research` | `sales-mail` | `search`、`read`、`list` | `scripts/dalton/researchdata_bridge.py:2838-2883`、`scripts/dalton/research_runtime/scripts/data_gateway.py:129-131` | 兼容调用（`search` 横跨 `sales-notes` 与 `tmtb`，`read` 接受 `mail:`/`digest:`/`tmtb:`） | 同 `sales-notes` + `tmtb` | 待补入 `BRIDGE_ROUTE_CAPABILITY_MAP` |
| `research` | `tmtb` | `search`、`read`、`list` | `scripts/dalton/researchdata_bridge.py:2884-2929`、`scripts/dalton/research_runtime/scripts/tmtb_alphaengine_sync.py:1-160` | `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:916-928`、`plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/sales_notes_search.py:186`、`workflows/topic-research/workflow.js:183` | `skills/market-digest/vectors.db`（`message_id LIKE 'alphaengine:%'`） | `tmtb`（`src/dalton/capabilities.py:782`） |
| `research` | `funda-ai` | `search`、`read`、`list` | `scripts/dalton/researchdata_bridge.py:2930-2975`、`scripts/dalton/research_runtime/scripts/funda_ai.py:1-160` | `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:916-928`、`plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/sales_notes_search.py:186`、`workflows/topic-research/workflow.js:183` | `<DALTON_RESEARCH_HOME>/state/funda-ai/index.sqlite`（`scripts/dalton/research_runtime/scripts/funda_ai.py:126-127`） | `funda_ai`（`src/dalton/capabilities.py:783`） |
| `research` | `tmeet` | `auth_status`、`record_search`、`record_content` | `scripts/dalton/researchdata_bridge.py:3094-3138,11686-11708` | `plugins/dalton/skills/dalton-tmeet-weekly-minutes/scripts/tmeet_minutes.py:85-89,358-501` | 本机 `tmeet` CLI（`:1175-1183`） | 待补入 `BRIDGE_ROUTE_CAPABILITY_MAP` 与 `sources.yaml` |
| `research` | `gcal` | `list_events`、`insert_event`、`delete_event` | `scripts/dalton/researchdata_bridge.py:3139-3184,11970-11993` | `plugins/dalton/skills/dalton-richard-earnings/scripts/richard_earnings.py:195-199,565-840` | 本机 `gws` CLI（`:1184-1192`）、`DALTON_GCAL_CALENDAR_ID` | `src/dalton/data_map/sources.yaml:121-128` 挂 `researchdata_bridge` + `fund_automations` |
| `research` | `discord-history` | `list_messages` | `scripts/dalton/researchdata_bridge.py:3185-3209,12079-12145,13718-13719` | `plugins/dalton/skills/dalton-wiki-auto-ingest/scripts/wiki_ingest.py:91-103,474-480`（消息分页）与 `:718-724`（附件链接刷新） | `DALTON_DISCORD_BOT_TOKEN`、`DALTON_DISCORD_HISTORY_CHANNELS` | `channel_discord`（`src/dalton/capabilities.py:784`） |
| `finance` | `sec` | 11 个操作：`list-filings`、`financials`、`ratios`、`facts`、`smart-facts`、`search-concepts`、`search-text`、`read-item`、`attachments`、`eight-k`、`insider` | `scripts/dalton/researchdata_bridge.py:2220-2302`、`scripts/dalton/research_runtime/scripts/data_gateway.py:143-187` | `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:932-943`、`plugins/dalton/skills/dalton-bullbear-tracker/references/amzn-rules.md:1-60`（及同目录 `googl`/`meta`/`msft`/`ant`/`spcx` 规则文件）、`plugins/dalton/skills/dalton-short-screen/SKILL.md:33-51`、`plugins/dalton/skills/dalton-filing-review/SKILL.md:15-33`、`plugins/dalton/skills/dalton-tob-industry-weekly/SKILL.md:70`、`plugins/dalton/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py:1147`、`plugins/dalton/skills/dalton-ai-app-weekly-monitor/SKILL.md:188-191`、`plugins/langalpha_market_data/fundamentals_mcp_server.py:845-858` | `findata-analyst/run.sh`、`SEC_USER_AGENT`、SEC EDGAR | `src/dalton/data_map/sources.yaml:61-66` 挂 `researchdata_bridge` |
| `finance` | `estimates` | `consensus`（支持 `params.shares_bn`、`params.tax`） | `scripts/dalton/researchdata_bridge.py:2303-2338`、`scripts/dalton/research_runtime/scripts/data_gateway.py:215-225` | `plugins/dalton/skills/dalton-bullbear/scripts/implied_model.py:21,200`、`plugins/dalton/skills/dalton-earnings-preview/SKILL.md:38`、`plugins/dalton/skills/dalton-tob-industry-weekly/SKILL.md:32`、`plugins/dalton/skills/dalton-init-company/templates/data_consensus_README.md:3,10` | `consensus_estimates.sh` | `src/dalton/data_map/sources.yaml:67-72` 挂 `researchdata_bridge` |
| `finance` | `cn-hk` | `filings`、`ask`、`quote` | `scripts/dalton/researchdata_bridge.py:2339-2374`、`scripts/dalton/research_runtime/scripts/data_gateway.py:194-211` | `plugins/dalton/skills/dalton-daily-filing-alert/SKILL.md:63-80`、`plugins/dalton/skills/dalton-stock-move/scripts/cn_price_fallback.py:253-277`、`plugins/dalton/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py:1116-1118`、`plugins/dalton/skills/dalton-tob-industry-weekly/SKILL.md:71-74`、`plugins/dalton/skills/dalton-financial-lenses/SKILL.md:41-54`、`src/dalton/autonomy/probes/cnhk.py:184-310` | `cn-hk-findata/run.sh`、巨潮/AkShare/腾讯/新浪 | `src/dalton/data_map/sources.yaml:73-78` 挂 `researchdata_bridge` |
| `macro` | `fred` | `macro`（`series`、`start`、`end`、`vintage`） | `scripts/dalton/researchdata_bridge.py:2375-2399`、`scripts/dalton/research_runtime/scripts/data_gateway.py:188-190` | 沙箱宏观序列查询（`src/dalton/data_map/sources.yaml:79-84`） | `findata-analyst/run.sh macro`、`FRED_API_KEY` | `src/dalton/data_map/sources.yaml:79-84` 挂 `researchdata_bridge` |
| `macro` | `treasury` | `treasury`（`dataset="tga" \| "debt"`） | `scripts/dalton/researchdata_bridge.py:2400-2422`、`scripts/dalton/research_runtime/scripts/data_gateway.py:191-193` | 沙箱财政部 TGA 余额与联邦债务查询（`src/dalton/data_map/sources.yaml:85-90`） | 美国财政部 Fiscal Data 公开 API | `src/dalton/data_map/sources.yaml:85-90` 挂 `researchdata_bridge` |
| `macro` | `market-regime` | `snapshot`、`us_topbottom`、`crowding`、`cn_leverage` | `scripts/dalton/researchdata_bridge.py:2423-2459,13208-13539` | `src/ptc_agent/agent/prompts/templates/components/tool_guide.md.j2:27`、`plugins/dalton/skills/dalton-ct-semi-sentiment/SKILL.md:54-55`、`src/dalton/data_map/sources.yaml:91-96` | `data_jobs` 产出的状态 JSON/CSV | `src/dalton/data_map/sources.yaml:91-96` 挂 `researchdata_bridge` |
| `community` | `xueqiu`、`employee`、`last30days`、`x-timeline` | 雪球 5 操作、员工评论 4 操作、`last30days`、`x-timeline` 4 操作 | `scripts/dalton/researchdata_bridge.py:2976-3093,12578-12750` | `plugins/dalton/skills/dalton-bullbear-tracker/scripts/collect.py:2198,2238`、`plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py:1435,1503`、`plugins/dalton/skills/dalton-humanoid-robot-weekly/scripts/humanoid_robot_weekly.py:160-170`、`plugins/dalton/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py:1122-1124`、`plugins/dalton/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py:1145`、`plugins/dalton/skills/dalton-full-coverage/references/branch-d-offstreet.md:18-29`、`plugins/dalton/skills/dalton-x-accounts-daily/SKILL.md:34-35,94` | `xueqiu_posts.py`、`employee_reviews_cli.py`、`last30days.py`、本机 `xreach` | `src/dalton/data_map/sources.yaml:97-120` 挂 `researchdata_bridge` |

---

## 2. 目标形态

### 2.1 全路由与全子路由唯一归宿总表

| 原桥入口与子路由 / 操作 | 唯一目标归宿（插件 / 后端模块 / 服务 / 网关） | Everflow 专属 | 对应能力开关（`CapabilityId`） | 交付批次 | 替代后数据等价性与差异处理 |
|---|---|---|---|---|---|
| `GET /healthz` | 各服务独立提供 `/healthz`：`dalton-research-host`（`:8951/healthz`，保留从 JSON `usage_file` 读当日 AlphaEngine 读文统计与熔断器状态）、`dalton-transcription`（`:8965/healthz`）、`host_task_daemon`（`:8952/healthz`）；缩减桥保留 `:8960/healthz` 至第 ③ 阶段 | 否（按运行的服务各自暴露） | 随各服务对应能力开启 | 第 2–5 批 | 等价；`scripts/dalton/check_bridge.sh` 等运维探针按服务职责分别探活 |
| `/mcp` → `catalog` | 各连接器标准 MCP `tools/list` schema + `plugin.json` `instruction`；全局可用源汇总由 `src/dalton/data_map/builder.py` 渲染至 `<data_map>` | 否 | 随各连接器能力开启 | 第 3–4 批 | 等价；沙箱在 `<mcp-servers>` 与 `.agents/tools/docs/<server>/` 读取参数说明 |
| `research` → `alphaengine`（9 个操作，含 `auth_status`、`list_filters`）+ `transcript` | 插件 `dalton-alphaengine`（服务名 `dalton_alphaengine`，`streamable-http` 连宿主机 `:8951/mcp/alphaengine`） | 是（`DALTON_HOST_PLATFORM=macos`） | `alphaengine` | 第 4 批 | 完全等价；继续走本机 `:8950` `server.mjs`、钥匙串与桌面端，单实例维护 SQLite v4 缓存、单飞、熔断与 JSON 当日计数 |
| `research` → `guidepoint`（`search_library`） | 插件 `dalton-guidepoint`（服务名 `dalton_guidepoint`，`streamable-http` 连宿主机 `:8951/mcp/guidepoint`） | 否（商业私有实现，跨平台宿主可部署） | `guidepoint` | 第 3 批 | 完全等价；Auth0 刷新令牌仅由宿主机 `dalton-research-host` 单一异步锁刷新写盘 |
| `research` → `company-wiki`（`search`/`read`/`list`/`topic`）+ `POST /wiki/ingest` | 检索归公开插件 `plugins/dalton_company_wiki/`（服务名 `dalton_company_wiki`，`streamable-http` 连 `${DALTON_RESEARCH_HOST_BASE_URL}/mcp/company-wiki`）；入库归后端调管理口 `POST /ingest/wiki`（携带独立管理凭据） | 否（公开插件 + 开源研究服务容器 `research-host`，核心安装可开启） | `company_wiki` | 第 3 批 | 完全等价；保留 `media="images"`、UTC `indexed_since`、`topic`、配图引用与幂等审计 |
| `research` → `sales-notes`（`search`/`read`/`list`）+ `POST /sales-notes/ingest` | 私有插件 `dalton-sales-notes`（服务名 `dalton_sales_notes`，连 `:8951/mcp/sales-notes`）；入库调管理口 `POST :8951/ingest/sales-notes`（独立管理凭据） | 是 | `sales_notes` | 第 3 批 | 完全等价；宿主机按路由限定只读销售笔记分区 |
| `research` → `tmtb`（`search`/`read`/`list`） | 私有插件 `dalton-tmtb`（服务名 `dalton_tmtb`，连 `:8951/mcp/tmtb`；同步脚本归 `scripts/dalton/ops/prod/`） | 是 | `tmtb` | 第 3 批 | 完全等价；宿主机强制过滤 `message_id LIKE 'alphaengine:%'` |
| `research` → `funda-ai`（`search`/`read`/`list`） | 私有插件 `dalton-funda-ai`（服务名 `dalton_funda_ai`，连 `:8951/mcp/funda-ai`） | 是 | `funda_ai` | 第 3 批 | 完全等价 |
| `research` → `all`（`search`/`read`/`list`/`get_document`）及 `sales-mail` | 公开插件 `plugins/dalton_research_all/`（服务名 `dalton_research_all`，`streamable-http` 连 `${DALTON_RESEARCH_HOST_BASE_URL}/mcp/federated`） | 否（核心安装启用 `company_wiki` 时由开源 `research-host` 容器一并提供） | 挂载门控：任一内部研究源开启；执行时**逐来源核对有效能力**（`sales_mail` 仅访问已开启的 `sales_notes` 与 `tmtb`） | 第 3 批 | 完全等价；保留 `alphaengine`/`guidepoint`/`source_status`、部分失败状态 `"partial"`、前缀 `read`（含 `image:`）及第 3 批向旧桥转发 AlphaEngine 单处理方 |
| `research` → `tmeet`（3 操作）与 `gcal`（3 操作） | 私有插件 **`dalton-office`**（服务名 `dalton_office`，连 `:8951/mcp/office`，宿主机调 `tmeet` 与 `gws` CLI） | 是 | `fund_automations`（`dalton-tmeet-weekly-minutes` 技能同时要求 `fund_automations,feishu_docs`） | 第 4 批 | 完全等价 |
| `finance` → `sec`（全部 11 个操作） | **扩展公开插件 `plugins/dalton_sec/`**（服务名 `dalton_sec`，沙箱 `stdio`，仅需 `SEC_USER_AGENT`） | 否（核心安装与 Everflow 默认共用） | `CORE_CAPABILITY` | 第 3 批 | **逐操作参数与形状对齐**（见 2.3 节：兼容 `form` 单值与 `10-Q` 缺省、`concept` 单值及部分失败状态） |
| `finance` → `estimates`（`consensus`，含 `shares_bn`、`tax`） | 私有插件 `dalton-estimates`（服务名 `dalton_estimates`，连 `:8951/mcp/estimates`） | 是 | 新增 `CapabilityId.ESTIMATES`（`everflow` 默认 `True`，`core` 默认 `False`） | 第 4 批 | **Everflow 完全等价**（保留 `implied_model` 复合快照供 `plugins/dalton/skills/dalton-bullbear/scripts/implied_model.py:200` 消费）；**核心安装差异**：关闭时经 `DALTON_CAP_ELSE` 改调 `yf_analysis` 并以显式 CLI 参数运行 `implied_model.py` |
| `finance` → `cn-hk`（`filings`、`ask`、`quote`） | 私有插件 `dalton-cnhk`（服务名 `dalton_cnhk`，连 `:8951/mcp/cnhk`） | 是 | 新增 `CapabilityId.CN_HK`（`everflow` 默认 `True`，`core` 默认 `False`） | 第 4 批 | **Everflow 完全等价**；**核心安装差异**：关闭时行情走 `yfinance`，公告原文走 `WebSearch`（`site:hkexnews.hk` / `cninfo.com.cn`） |
| `macro` → `fred`（含 `vintage`）与 `treasury`（`dataset="tga" \| "debt"`） | **新增公开插件 `plugins/dalton_macro/`**（服务名 `dalton_macro`，沙箱 `stdio`） | 否（随核心包发布） | `CORE_CAPABILITY` | 第 3 批 | **完全等价**：Fiscal Data TGA/债务免密钥直连，与内置 `plugins/langalpha_market_data/macro_mcp_server.py:304` 的国债收益率曲线互补并存 |
| `macro` → `market-regime`（4 个操作） | 私有插件 `dalton-market-regime`（服务名 `dalton_market_regime`，连 `:8951/mcp/market-regime`） | 是 | `data_jobs` | 第 4 批 | 完全等价 |
| `community` → `xueqiu`、`employee`、`last30days`、`x-timeline` | 私有插件 **`dalton-community`**（服务名 `dalton_community`，连 `:8951/mcp/community`） | 是 | 新增 `CapabilityId.COMMUNITY_SOURCES`（`everflow` 默认 `True`，`core` 默认 `False`） | 第 4 批 | **Everflow 完全等价**（保留 `xreach` `oldest_fetched_at` 水位字段）；**核心安装差异**：关闭时改用内置 `x_api` 与 `WebSearch`/`scrape` |
| `POST /embed` | 后端内置嵌入模块 `src/dalton/embeddings/` + 元数据表 `dalton_embedding_metadata` | 否 | 随 `conversation_archive` 开启 | 第 1 批 | 完全等价；保留 `gemini-embedding-2-preview` → `gemini-embedding-2` 映射与逐文本独立 `Content` 批量行为 |
| `GET /jobs/ops` | `scripts/dalton/ops/prod/export_ops_status.py` + 后端 `src/dalton/autonomy/system_jobs.py` 读状态文件 | 是 | `DALTON_PROFILE=everflow` 且状态文件存在时展示 | 第 1 批 | 完全等价 |
| `/transcriptions*`（6 个端点） | 独立服务 `dalton-transcription`（`:8965`，排他锁接管 `DALTON_ASR_JOBS_DIR` 与 `DALTON_ASR_USAGE_FILE`） | 否（公开仓库含开源服务端与通用 Whisper 适配器，私有仓库供 Seed-ASR 2.0） | `transcription`（含转写对象存储门控） | 第 2 批 | 完全等价；保留 6 个端点、历史结果与原始逐字稿流式下载 |
| `/internal/{market-digest,ai-finance}/*` | 宿主机 `dalton-research-host` 管理口 `:8951/internal/*`（校验独立管理凭据） | 是 | `fund_automations` | 第 5 批 | 完全等价 |
| `/internal/data-tasks/{host-run,host-kill}` | 宿主机守护进程 `scripts/dalton/ops/prod/host_task_daemon.py`（`:8952`，独立管理凭据 + 排他锁） | 是 | `data_jobs` | 第 5 批 | 完全等价；完整保留 `generation`、`pgid`+`lstart`、`fence_lost`、白名单与按任务凭据注入 |
| `POST /notify`、`POST /feishu/*`、受限 `/mcp` 下的 `research(route="discord-history")` | **第 4、5 批继续保留在缩减后的桥 `:8960` 上**（`:8960/mcp` 仅开放 `discord-history`，沙箱侧通过受 `channel_discord` 门控的过渡插件 `plugins/dalton_discord_history/` 供 `wiki_ingest.py` 调用）；**第 ③ 阶段并入 `integrations/dalton-channel-gateway/`** | 否 | `channel_feishu`、`feishu_docs`、`channel_discord` | 第 ③ 阶段接手（第 4–5 批保留受限入口） | 第 ② 阶段保持现行接口与行为不变，且彻底腾退旧服务名 `researchdata` |

### 2.2 宿主机研究服务形态与三类鉴权隔离

1. **宿主机研究服务推荐形态**：采用 **单进程多模块研究服务 `dalton-research-host`（监听 `:8951`）** 对外按连接器路径暴露 `/mcp/<connector>`，仅保留 `:8950` `alphaengine-mcp`（`server.mjs`）独立运行。原因：① `sales-notes`、`tmtb` 与 `market-digest` 共用 `skills/market-digest/vectors.db`（`scripts/dalton/research_runtime/scripts/research_library.py:163,172`），同进程连接管理可在 SQL 层强制按来源过滤并协调写入；② Guidepoint 的 Auth0 令牌刷新（`scripts/dalton/research_runtime/scripts/guidepoint_mcp_proxy.py:57,99`）由宿主机唯一异步锁串行写盘；③ AlphaEngine 的公平并发限流、SQLite v4 缓存与 JSON 当日读文账本跨全部沙箱共享单实例状态；④ `/mcp/federated`（`all`）在同一进程内按有效能力扇出。核心安装启用 `company_wiki` 时，由同一公开代码入口启动开源 `research-host` 容器（仅装载公开的 `/mcp/company-wiki`、`/mcp/federated` 与 `/ingest/wiki` 模块）。
2. **三类凭据与逐来源权限检查**：
   - **逐来源能力检查**：旧桥、`dalton-research-host`、`all`（`search`/`read`/`list`）与 `sales_mail` 统一加载当前部署的有效能力集合 `effective_capabilities()`。每次联搜或按文档 ID 读取全文时，服务端逐一核对目标来源对应的 `CapabilityId` 是否开启（例如仅开启 `tmtb` 而关闭 `sales_notes` 时，`all.search` 与 `sales_mail.search` 只检索 `tmtb` 分区，`all.read("mail:123")` 立即拒绝并返回 `403 capability_disabled`）。
   - **管理接口独立鉴权**：沙箱仅持有只读 MCP 调用令牌 `DALTON_RESEARCH_HOST_TOKEN`（及每次请求由后端签发的 `X-Dalton-Caller-Token`，见 `src/dalton/sandbox_caller_identity.py:94-118`）；`/ingest/wiki`、`/ingest/sales-notes`、`/internal/market-digest/*`、`/internal/ai-finance/*` 强制校验仅注入 `backend` 容器的独立管理令牌 `DALTON_RESEARCH_ADMIN_TOKEN`（延续 `scripts/dalton/researchdata_bridge.py:25316-25342` `_require_admin_token` 的管理隔离）。

### 2.3 返回结构契约与关键操作兼容规范

#### （1）返回信封、部分失败状态与溯源契约

对照 `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py` 的 `_extract_non_empty_error`（`:648-660`）、`_extract_route_page_info`（`:946-1004`）、`_extract_route_items_with_page_info`（`:1006-1010`）、`_extract_route_items`（`:1013-1055`）及 `src/dalton/provenance/declared_sources.py:48-120`：

1. **条目与扁平分页字段**：常规研究与金融连接器在顶层或 `data.structuredContent` 下返回 `results`（`guidepoint` 保持 `data.structuredContent.data`；`transcript` 保持专有 `data.transcripts` + `data.coverage` 供财报技能专用解析器读取），并在同一字典层级返回扁平分页键 `cursor`、`has_more`、`total_upstream`、`returned`。
2. **`all.search` 部分失败状态契约**：对齐 `scripts/dalton/research_runtime/scripts/research_library.py:144-145` 与 `scripts/dalton/researchdata_bridge.py:14355-14380`，顶层 `status` 取值为 `"ok"` | `"partial"` | `"empty"` | `"error"`。当任一已启用子来源超时（超过 20 秒 `fanout_grace_s`）、触发熔断或产生分块告警，同时其余已启用子来源正常返回时，顶层保持 `"ok": true`、`"status": "partial"`，并在 `"source_status"` 中按来源逐项记录 `{"ok": false, "status": "timeout"|"error", "reason": "...", "count": 0}` 及保留 `"alphaengine"`、`"guidepoint"` 顶层摘要块。
3. **溯源字段契约**：对照 `src/dalton/provenance/declared_sources.py:56-60`（顶层若带空的 `"_declared_sources": []` 会跳过 `"_sources"`），连接器统一写入非空的顶层 `"_sources": [...]`（省略空的 `_declared_sources` 键），并在 `src/dalton/provenance/declared_sources.py:13` 的 `_ALLOWED_SERVERS` 中登记全部新增连接器服务名（含过渡期 `dalton_discord_history`）。

#### （2）核心连接器关键签名与逐操作兼容要点（各批开发任务书须逐字段冻结）

1. **`dalton-company-wiki` 与 `dalton-research-all`（补齐图片与 UTC 增量参数）**：
   - `dalton_company_wiki`（`list`、`search`、`read`、`topic`）：对照 `scripts/dalton/research_runtime/scripts/research_library.py:56,62`，`params.media` 完整支持 `"all"` | `"text"` | `"images"`；`params.indexed_since` 同时支持日期 `YYYY-MM-DD` 与 **UTC ISO 时间戳**（如 `2026-11-20T08:00:00Z`）。
   - `dalton_research_all`（`search`、`read`、`list`、`get_document`、`sales_mail`）：`read`（及别名 `get_document`）的前缀分派表完整覆盖 **`wiki:` → `company-wiki`、`mail:`/`digest:` → `sales-notes`、`tmtb:` → `tmtb`、`funda:` → `funda-ai`、`image:` → 研究配图读取**（对齐 `scripts/dalton/research_runtime/scripts/research_library.py:450-458`，`image:` 核验所属来源能力开启并返回图片元数据与路径）。
2. **`dalton-alphaengine`（9 个操作 + `transcript`）**：
   - 包含 `search_library`、`get_document`、`get_document_info`、`list_attachments`、`list_recording_tasks`、`get_recording_status`、`get_recording_task`、`list_filters`、`auth_status` 及 `transcript`（对照 `scripts/dalton/researchdata_bridge.py:2520-2658,2704-2738`）。其中 `list_filters` 支持全部 **9 类** `filter_type`（`categories`、`upstream_categories`、`document_types`、`sources`、`industries`、`companies` 必填 `params.query`、`regions`、`watchlist_groups`、`watchlist_stocks` 必填 `params.group_id`）。
3. **公开插件 `plugins/dalton_sec/` 逐操作参数兼容契约**：
   对照现有 `plugins/dalton_sec/dalton_sec_mcp_server.py:968-1055` 与原桥 `scripts/dalton/researchdata_bridge.py:2220-2302` 的差异，第 3 批开发任务按以下规则逐操作对齐：
   - **`list_filings`（对应原 `list-filings`）**：在现有 `forms: str = "10-K,10-Q,20-F,6-K"` 旁新增兼容单值参数 `form: str | None = None`；当传入顶层 `form`（如 `"8-K"`、`"4"`、`"424B5"`）时严格按该单一表单过滤；迁移原桥调用点（如 `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:932-943`）时显式传入目标 `form`（原桥省略 `form` 的调用点显式补传 `form="10-Q"` 以保持原桥缺省 `10-Q` 行为，见 `scripts/dalton/research_runtime/scripts/data_gateway.py:172`）。
   - **`get_company_facts` 与新增 `get_concept_facts`（对应原 `facts`）**：保留现有 `get_company_facts(ticker, concepts, ...)` 的归一化多概念结构供既有核心调用方使用；新增兼容单值 `concept` 的原始时间序列工具 `get_concept_facts(ticker, concept, form="10-Q", limit=10, as_of=None)` 返回与原 `facts` 一致的 `rows` 列表。
   - **其余 9 个操作逐一对应**：`get_financials`（`statement="all"|"income"|"balance"|"cash"`，默认 `form="10-Q"`）、`get_ratios`、`search_smart_facts`(`query`)、`search_concepts`(`query`)、`search_filing_text`(`query`, `form="10-Q"`, `window`, `max_matches`)、`read_filing_item`(`item`, `form="10-Q"`, `max_chars` 1–500000)、`get_filing_attachments`(`form="10-Q"`, 顶层 `document` 或 `accession`+`document_type` 互斥, `max_chars`)、`list_eight_k_events`、`list_insider_transactions`。当多期报表或附件列表存在单条目解析告警但主体成功时，返回 `"ok": true, "status": "partial", "warnings": [...]` 与 `_declared_sources`。

### 2.4 插件地址按形态装配、公开/私有边界与发布包白名单

1. **同一插件声明按部署形态装配地址（装配入口定义）**：
   - 在 `src/dalton/settings.py` 新增形态感知的服务地址解析函数 `resolve_connector_env_defaults()`（内部调用 `src/dalton/profile.py` 的 `resolve_profile()`），并在插件配置装配入口 `src/ptc_agent/config/plugins.py:198-205,256-260`（`_server` 执行 `_expand` 展开 `mcp.json` 中 `${VAR}` 时）作为形态缺省字典合并生效，同时在 `scripts/standalone/init.sh` 与 `scripts/dalton/ops/prod/deploy_main.sh` 写入 `.env`：
     - **Everflow 形态（`DALTON_PROFILE=everflow`）**：`DALTON_RESEARCH_HOST_BASE_URL` 解析为 `http://host.docker.internal:8951`，`DALTON_TRANSCRIPTION_SERVICE_URL` 解析为 `http://host.docker.internal:8965`，`DALTON_DISCORD_BRIDGE_MCP_URL` 解析为 `http://host.docker.internal:8960/mcp`。
     - **核心安装形态（`DALTON_PROFILE=core`）**：`DALTON_RESEARCH_HOST_BASE_URL` 解析为沙箱网络上的 Compose 服务名 `http://research-host:8951`，`DALTON_TRANSCRIPTION_SERVICE_URL` 解析为 `http://transcription:8965`。
   - 由此，公开仓库中的 `plugins/dalton_company_wiki/mcp.json`（`"url": "${DALTON_RESEARCH_HOST_BASE_URL}/mcp/company-wiki"`）与 `plugins/dalton_research_all/mcp.json`（`"url": "${DALTON_RESEARCH_HOST_BASE_URL}/mcp/federated"`）以**同一份公开插件声明**同时适配 Everflow 宿主机与核心安装容器栈。
2. **唯一私有插件目录 `DALTON_EXTRA_PLUGINS_DIR` 与仓库边界**：
   - **公开仓库（纳入核心发布包）**：公开 `stdio` 插件 `plugins/dalton_sec/`、`plugins/dalton_macro/`；公开 `streamable-http` 插件 `plugins/dalton_company_wiki/`、`plugins/dalton_research_all/` 及第 ③ 阶段前的受限过渡插件 `plugins/dalton_discord_history/`；开源研究宿主框架 `src/dalton/research_host/`（承载 Wiki 与跨库联邦外壳）、后端嵌入模块 `src/dalton/embeddings/`、开源转写作业框架 `src/dalton/transcription_service/`（含通用 OpenAI 兼容 Whisper ASR Worker）；共享技能适配模块 `plugins/dalton/skills/dalton-conventions/scripts/connector_transport.py`（位于带 `SKILL.md` 的 `dalton-conventions` 内，经 `src/ptc_agent/core/sandbox/assets.py:314-335` 同步进全部沙箱）。
   - **私有仓库（经 `DALTON_EXTRA_PLUGINS_DIR` 装配）**：商业连接器插件包（`dalton-alphaengine`、`dalton-guidepoint`、`dalton-sales-notes`、`dalton-tmtb`、`dalton-funda-ai`、`dalton-office`、`dalton-estimates`、`dalton-cnhk`、`dalton-market-regime`、`dalton-community`）及宿主机商业服务端模块与火山引擎 Seed-ASR 2.0 Worker。扩展 `src/ptc_agent/config/plugins.py:100-125` 的 `_bundle_dirs()` 追加扫描 `DALTON_EXTRA_PLUGINS_DIR`（`bundled_mcp_servers` 于 `:278-345` 与 `bundled_skill_dirs` 于 `:348-356` 均自动继承），且全部插件名预先静态登记在 `src/dalton/capabilities.py:603-637` 的 `MCP_SERVER_CAPABILITY_MAP` 与 `BUNDLED_PLUGIN_CAPABILITY_MAP` 中（核心安装 `core_default` 均为 `False`）。
3. **核心发布包白名单（`scripts/standalone/package_release.sh:219-227`）与镜像构建来源**：
   - 第 2 批：开源转写服务直接复用 `scripts/dalton/Dockerfile.dalton`（`docker-compose.core.yml` 中 `*backend-build` 构建的 `dalton-core-backend:local` 镜像，已内置 `ffmpeg`）以 `python -m src.dalton.transcription_service` 启动。
   - 第 3–4 批：在 `scripts/standalone/package_release.sh:219-227` 的 `ALLOWLIST_OPTIONAL` 中新增 `"plugins/dalton_macro"`、`"plugins/dalton_company_wiki"`、`"plugins/dalton_research_all"`（第 3 批）与 `"plugins/dalton_discord_history"`（第 4 批过渡项）；开源 `research-host` 服务同样复用 `*backend-build`（`dalton-core-backend:local`）以 `python -m src.dalton.research_host` 启动。

### 2.5 嵌入模块：分离库内标签与 API 模型名及客户端行为保真

对照 `scripts/dalton/research_runtime/skills/company-wiki/scripts/company_wiki_cli.py:54-61,205-254` 的生产行为：
1. **库内模型标签与上游 API 请求模型名分开记录**：`dalton_embedding_metadata` 表分别定义 `model_label`（写入数据库卡片与索引元数据的模型标识）与 `api_model_name`（实际向提供商发起 HTTP 请求的模型名），外加 `provider`、`dimensions`、`task_type_doc`、`task_type_query`。Everflow 锁定 `model_label='gemini-embedding-2-preview'`、`api_model_name='gemini-embedding-2'`、`dimensions=3072`、`task_type_doc='RETRIEVAL_DOCUMENT'`、`task_type_query='RETRIEVAL_QUERY'`。
2. **保留逐文本独立 `Content` 的批量行为**：针对 `gemini-embedding-2` 将纯字符串数组折叠为单向量的特性（`scripts/dalton/research_runtime/skills/company-wiki/scripts/company_wiki_cli.py:219-225`），`src/dalton/embeddings/` 在批量嵌入（每批上限 100 条）时为每段文本构造独立的 `Content(parts=[Part(text=t)])`，并强制校验返回向量条数等于输入文本条数（`:226-229`）。
3. **维度一致性与核心 1536 维回退数据规则**：核心新装首次启用归档时按所选模型维度 `dim` 重设 `dalton_conversation_cards.embedding` 为 `vector(<dim>)` 并建立 `halfvec(<dim>)` HNSW 索引（原定义见 `src/dalton/migrations/versions/d009_conversation_archive.py:60,71-74`），查询端 `src/dalton/database/conversations.py:534-558` 按锁定维度构造距离表达式，配置与锁定元数据偏离时立即报错。**若核心安装已按 1536 维写入卡片数据，不支持原地回退至只认 3072 维的旧版本；执行此类跨版本回退前须使用升级前生成的数据库备份恢复**。

---

## 3. 调用方迁移

### 3.1 沙箱技能、工作流与内置插件迁移清单

1. **沙箱传输适配层 `plugins/dalton/skills/dalton-conventions/scripts/connector_transport.py`**：
   - 修正现状描述：`plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:569-578` 现为显式 `DALTON_RESEARCHDATA_URL` 覆盖优先、缺省走 `tools.researchdata` 包装器。
   - 新适配层读取 `.agents/tools/mcp_client_config.json`（`:595-627`），优先调用已挂载的新连接器包装模块（`tools.dalton_*`）；过渡回退旧桥时，旧桥与宿主机服务均按同一份有效能力配置逐来源鉴权，已关闭的能力直接返回 `capability_disabled`。
2. **Discord 历史读取过渡保留与客户端切流（E10）**：
   - `plugins/dalton/skills/dalton-wiki-auto-ingest/scripts/wiki_ingest.py` 在 `:91-103` 定义 `call_research`，供 `:474-480`（频道消息分页拉取）与 `:718-724`（失效附件链接单条重取刷新）调用 `research(route="discord-history", operation="list_messages")`。
   - 第 4 批将旧桥 `:8960/mcp` 切换为仅开放 `discord-history` 的受限模式，同步上线受 `CapabilityId.CHANNEL_DISCORD` 门控的专属过渡插件 `plugins/dalton_discord_history/`（服务名 `dalton_discord_history`），并将 `plugins/dalton/skills/dalton-wiki-auto-ingest/scripts/wiki_ingest.py:91-103` 的 `call_research` 切至 `tools.dalton_discord_history.research`（回退时兼容回落 `tools.researchdata.research`），持续服务至第 ③ 阶段渠道网关接手，同时让旧 `plugins/dalton_data/`（`researchdata`）在第 4 批观察期后彻底退役。
3. **其余技能、工作流与文档迁移范围**：
   - **采集与周报脚本**：`plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:569-646,870-944`（6 研究源走对应连接器，`:932-943` 显式传 `form` 改调 `dalton_sec.list_filings`）、`plugins/dalton/skills/dalton-bullbear-tracker/scripts/collect.py:600,1410-1418,2198,2238,2733`（`:1410` 调 `dalton_research_all.read`）、`plugins/dalton/skills/dalton-sector-delta/scripts/collect_sector.py:89-90`、`plugins/dalton/skills/dalton-mark-weekly-newenergy/scripts/rd_transport.py:50-115`、`plugins/dalton/skills/dalton-mark-weekly-newenergy/scripts/mark_weekly.py:590-620,1918,2323`、`plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/richard_cov_transport.py:12-45`、`plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/sales_notes_search.py:192-210`、`plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/richard_coverage_weekly_cli.py:1435,1503`、`plugins/dalton/skills/dalton-humanoid-robot-weekly/scripts/humanoid_robot_weekly.py:148-170`、`plugins/dalton/skills/dalton-china-consumer-weekly/scripts/china_consumer_weekly.py:1110-1125`、`plugins/dalton/skills/dalton-global-consumer-weekly/scripts/global_consumer_weekly.py:1143-1147`、`plugins/dalton/skills/dalton-tob-industry-weekly/scripts/tob_industry_weekly.py:241`、`plugins/dalton/skills/dalton-bullbear/scripts/implied_model.py:21,200`、`plugins/dalton/skills/dalton-tmeet-weekly-minutes/scripts/tmeet_minutes.py:85-89,358-501`、`plugins/dalton/skills/dalton-richard-earnings/scripts/richard_earnings.py:195-199,565-840`、`plugins/dalton/skills/dalton-stock-move/scripts/cn_price_fallback.py:253-277`（`plugins/dalton/skills/dalton-transcript-polish/scripts/dalton_asr.py:49` 走后端 API 保持原样）。
   - **JS 工作流、内置插件与技能 Markdown**：`workflows/topic-research/workflow.js:165,181-276` + `workflows/topic-research/contract.json:1-60`、`workflows/bullbear-doc/workflow.js:249`、`workflows/initial-coverage/workflow.js:321`、`plugins/langalpha_market_data/fundamentals_mcp_server.py:845-858` 及 `plugins/dalton/` 下全部技能 `SKILL.md`/规则模板中的 `<!-- DALTON_CAP:... -->` 细粒度拆分。

### 3.2 后端、前端、根目录配置、提示词模板与宿主脚本迁移全表

| 类别 | 文件路径与精确行号 | 当前逻辑 | 对应批次与迁移说明 |
|---|---|---|---|
| 归档嵌入与重渲染校验 | `src/dalton/conversations/embedder.py:75-207`、`src/dalton/database/conversations.py:525-558`、`src/dalton/conversations/search.py:250-285`、`scripts/dalton/bridge_subprocess_env.py:29,881,1437-1447,1946-1950` | 调桥 `/embed`；`bridge_subprocess_env.py` 在 `cmd == "render"` 时强制要求 `DALTON_BRIDGE_EMBED_URL` 指向 `127.0.0.1:8960` 且配置 `DALTON_BRIDGE_ADMIN_TOKEN` | **第 1 批**：切至 `src/dalton/embeddings/` 并核验 `dalton_embedding_metadata`；同步更新 `scripts/dalton/bridge_subprocess_env.py:1437-1447,1946-1950` 使归档 `render` 子命令校验后端直连嵌入凭据 |
| 运维任务状态与提示词 | `src/dalton/autonomy/system_jobs.py:232-315`、`src/ptc_agent/agent/prompts/loader.py:104-107`、`src/ptc_agent/agent/prompts/templates/components/chief_of_staff.md.j2:58`、`src/ptc_agent/agent/prompts/templates/components/user_profile.md.j2:92` | 调桥 `/jobs/ops`；两处模板通过 `researchdata_bridge_enabled` 控制“运维任务只读”文案 | **第 1 批**：改为读取 `var/dalton/ops_jobs_status.json`；`loader.py` 新增 `ops_jobs_enabled`（`everflow` 形态为真）替换两处模板中的 `researchdata_bridge_enabled` |
| 转写客户端 | `src/dalton/services/transcriptions.py:461-584,2038-2075` | 校验 `RESEARCHDATA_BRIDGE` 并请求旧桥 | **第 2 批**：校验 `CapabilityId.TRANSCRIPTION`，请求 `DALTON_TRANSCRIPTION_SERVICE_URL` + `DALTON_TRANSCRIPTION_TOKEN` |
| 前端能力枚举与自动入库 | `web/src/hooks/useCapabilities.ts:16-50`、`web/src/pages/ChatAgent/components/dalton/AutoIngestToggle.tsx:26`、`plugins/langalpha_service/skills/langalpha-doc/references/settings-map.md:27` | `CAPABILITY_IDS` 与 `EVERFLOW_DEFAULT_CAPABILITIES` 缺少 `company_wiki`/`sales_notes` 等子能力；自动入库检查 `researchdata_bridge` | **第 3 批**（第 4 批补齐新能力）：在 `web/src/hooks/useCapabilities.ts:16-50` 的 `CAPABILITY_IDS` 与 `EVERFLOW_DEFAULT_CAPABILITIES` 同步加入 `company_wiki`、`sales_notes`、`tmtb`、`funda_ai`、`fund_automations`（及第 4 批 `estimates`、`cn_hk`、`community_sources`）；`web/src/pages/ChatAgent/components/dalton/AutoIngestToggle.tsx:26` 改为 `hasCapability('company_wiki') \|\| hasCapability('sales_notes')` |
| 入库工具、数据地图与内容装配 | `src/dalton/tools/__init__.py:370-404`、`src/dalton/tools/wiki_tools.py:447-485`、`src/dalton/tools/ingest_tools.py:72-105,578-677`、`src/dalton/data_map/builder.py:287-355`、`src/dalton/data_map/health.py:173-235`、`src/dalton/data_map/sources.yaml:1-140`、`src/dalton/content_assembly.py:240-253,281-353` | 依赖 `RESEARCHDATA_BRIDGE` 总开关门控注册、数据地图构建与文案替换 | **第 3 批**：入库改调 `dalton-research-host` `/ingest/*`（带独立管理凭据），`ingest_research` 改挂 `company_wiki \| sales_notes`，`builder.py` 与 `content_assembly.py` 改为按实际已启用研究连接器集合门控 |
| 宿主 Guidepoint 脚本、部署与迁移脚本 | `scripts/dalton/research_runtime/scripts/data_gateway.py:40,283`、`scripts/dalton/research_runtime/skills/guidepoint-transcript-search/scripts/gp_search.py:13`、`scripts/dalton/ops/prod/deploy_main.sh:633,658,662,915-924`、`scripts/dalton/ops/research_runtime_migrate.sh:806-812` | 硬编码 `:8943/mcp`；`deploy_main.sh` 只管理旧桥，待补新增宿主服务的拉起、失败回退与探活 | **第 3 批**：切流前先将 `scripts/dalton/research_runtime/scripts/data_gateway.py:40,283` 与 `scripts/dalton/research_runtime/skills/guidepoint-transcript-search/scripts/gp_search.py:13` 改为读取配置变量 `DALTON_GUIDEPOINT_MCP_URL`；同批更新 `deploy_main.sh` 与 `research_runtime_migrate.sh` 纳入 `com.dalton.research-host` 的启停、失败回退交接与探活 |
| 根配置超时、工具引导模板与探针脚本 | `agent_config.dalton.yaml:51-52`、`src/ptc_agent/agent/prompts/loader.py:104-111`、`src/ptc_agent/agent/prompts/templates/components/tool_guide.md.j2:23-27`、`src/dalton/autonomy/probes/cnhk.py:184-310`、`scripts/dalton/check_bridge.sh:38-65`、`scripts/dalton/install_bridge.sh:51-90`、`scripts/dalton/export_review_window.sh:222`、`scripts/dalton/realrun/check_env.py:835`、`scripts/dalton/realrun/g7_bridge.py:44-110`、`scripts/dalton/realrun/g4_autonomy.py:412-420` | `agent_config.dalton.yaml` 仅为 `researchdata` 设 `call_timeout_s: 625`；`src/ptc_agent/agent/prompts/templates/components/tool_guide.md.j2:24,27` 用 `researchdata_bridge_enabled` 门控研究总览与 `market-regime`；`scripts/dalton/check_bridge.sh:39` 请求旧路径 `/mcp/<token>` | **第 3–4 批**：`agent_config.dalton.yaml` 为各新连接器（含过渡期 `dalton_discord_history`）设置 `call_timeout_s: 625`；`src/ptc_agent/agent/prompts/templates/components/tool_guide.md.j2:24` 改绑 `research_connectors_enabled`，`:27` 改绑 `market_regime_enabled` 并指向 `dalton_market_regime`；`check_bridge.sh` 等探针改为 `Authorization: Bearer` 分别校验 `:8951` 各连接器与 `:8960` 受限 `discord-history` |
| 摘要、AI Finance 与宿主定时任务 | `src/dalton/tools/market_digest_tools.py:42-43,286-289`、`src/dalton/tools/market_digest_output_tools.py:471-480`、`src/dalton/tools/ai_finance_inputs_tools.py:309`、`src/dalton/tools/ai_finance_signals_tools.py:706`、`src/dalton/tools/ai_finance_report_tools.py:779`、`src/dalton/data_jobs/host_runner.py:23-29,108-181`、`src/dalton/feishu.py:106-130` | 统一请求 `:8960` | **第 5 批**：摘要与 AI Finance 切至 `:8951/internal/*`；`host_runner.py` 切至 `:8952` `host_task_daemon.py`；`feishu.py` 细化检查 `FEISHU_DOCS`/`CHANNEL_FEISHU` 并继续走缩减桥 `:8960` 直至第 ③ 阶段 |

### 3.3 退役判定标准

每批旧路由进入步骤 C 清理前，连续 **14 个自然日**同时满足：① 旧入口目标路由请求数为 0（心跳日志每日正常续写）；② 新入口成功调用量 > 0 且错误率、缓存命中率、`prov:` 溯源率与切流前基线一致；③ 所有周频/低频技能与宿主任务在观察期内至少完整实跑成功 1 次（排程待触发项由人工主动触发验证）。

---

## 4. 能力与开关

### 4.1 宿主平台配置、能力映射与前端同步

1. **宿主平台配置 `DALTON_HOST_PLATFORM`**：`src/dalton/capabilities.py` 通过 `resolve_host_platform()` 读取 `DALTON_HOST_PLATFORM`（`macos` | `linux`），仅在 `macos` 下允许 `CapabilityId.ALPHAENGINE` 启用。
2. **单能力注册与多源逐项门控**：
   - 每个独立连接器绑定单一 `CapabilityId`（复用 `alphaengine`、`guidepoint`、`company_wiki`、`sales_notes`、`tmtb`、`funda_ai`、`fund_automations`、`data_jobs`、`transcription`、`conversation_archive`、`feishu_docs`、`channel_discord`，第 4 批新增 `estimates`、`cn_hk`、`community_sources`）。
   - 针对聚合连接器 `dalton_research_all` 与旧桥兼容别名 `sales-mail`：扩展 `MCP_SERVER_CAPABILITY_MAP`、`BUNDLED_PLUGIN_CAPABILITY_MAP` 与 `BRIDGE_ROUTE_CAPABILITY_MAP`（`src/dalton/capabilities.py:603-676,775-801`）支持元组表示的“任一子能力开启即挂载入口”（`dalton_research_all` 对应 6 个内部研究源元组，`sales-mail` 对应 `(SALES_NOTES, TMTB)`），同时在服务端执行器内按 `effective_capabilities()` 对当次请求涉及的每个具体来源做二次鉴权。
   - 补齐 `tmeet` 与 `gcal` 在 `BRIDGE_ROUTE_CAPABILITY_MAP` 中的 `CapabilityId.FUND_AUTOMATIONS` 登记。
3. **前端首屏与运行时能力同步**：在 `web/src/hooks/useCapabilities.ts:16-50` 将 `company_wiki`、`sales_notes`、`tmtb`、`funda_ai`、`fund_automations`、`estimates`、`cn_hk`、`community_sources` 同步加入 `CAPABILITY_IDS` 与 `EVERFLOW_DEFAULT_CAPABILITIES`（`CORE_DEFAULT_CAPABILITIES` 保持空数组 `[]`），保证 Everflow 首屏零闪烁显示自动入库开关与设置项，核心安装按 `/api/v1/dalton/capabilities` 返回值动态开启。

### 4.2 能力关闭时全栈各层的表现

当某连接器能力关闭时：① `is_bundled_plugin_enabled_by_capability`（`src/dalton/capabilities.py:656-676`）与 `filter_mcp_servers_by_capability`（`:678-690`）在装配期剔除该服务并跳过后台重连（`:709-728`），宿主机服务端同步拒绝该源查询；② `src/dalton/tools/__init__.py` 省去关闭项对应的入库或自动化工具；③ `src/dalton/content_assembly.py` 通过 `evaluate_capability_expr`（`:70-130`）与 `_assemble_stacked`（`:156-229`）裁剪 Markdown、JS 工作流与 JSON 契约；④ `src/dalton/data_map/builder.py` 从可用路由表隐去该源并归入公开披露缺省提示；⑤ 前端 `useCapabilities` 与帮助站点隐藏对应设置与说明。

---

## 5. 运行与部署

### 5.1 Everflow 生产与独立核心安装的网络及容器拓扑

1. **Everflow 生产（macOS 宿主 + Colima 容器栈，`docker-compose.dalton.yml`）**：
   - 宿主机运行 `com.dalton.alphaengine-mcp`（`:8950`）、`com.dalton.research-host`（`:8951`）、`com.dalton.transcription`（`:8965`，按需）、`com.dalton.host-task-daemon`（`:8952`）及缩减后的通知/飞书/Discord 过渡桥 `com.dalton.researchdata-bridge`（`:8960`）。
   - `backend` 容器挂载私有插件声明目录 `/opt/dalton-extra-plugins:ro`（`DALTON_EXTRA_PLUGINS_DIR`）；Everflow 沙箱保留 `ExtraHosts = ["host.docker.internal:host-gateway"]`（`src/ptc_agent/core/sandbox/providers/docker.py:774-783`），通过 `http://host.docker.internal:8951/mcp/<connector>` 直连宿主机服务并由 `src/ptc_agent/core/sandbox/mcp_client_runtime.py:2177-2198` 自动附带 `X-Dalton-Caller-Token`。

2. **独立核心安装（Linux / macOS Docker，`docker-compose.yml` + `docker-compose.core.yml`）**：
   - 核心安装的沙箱运行在隔离网络 `sandbox-net`（`${COMPOSE_PROJECT_NAME:-dalton-core}_sandbox`，见 `docker-compose.core.yml:214,315-319`），仅保留容器间服务名解析（`ExtraHosts` 仅在 Everflow 形态注入，见 `src/ptc_agent/core/sandbox/providers/docker.py:780-782`）。
   - 核心安装在 `docker-compose.core.yml` 中新增两个按需启用的 Compose profile 服务（均直接复用 `scripts/dalton/Dockerfile.dalton` 构建的 `dalton-core-backend:local` 镜像），并**同时接入 `core_internal` 与 `sandbox-net` 双网络**：
     - **`research-host` 服务（profile: `wiki`）**：挂载持久化卷 `dalton-wiki-data:/var/lib/dalton/wiki`，同时提供 `/mcp/company-wiki`、`/mcp/federated`（`dalton_research_all` 针对已启用核心源的跨库检索与 `read`）及后端管理口 `/ingest/wiki`。沙箱通过 `sandbox-net` 上的服务名 `http://research-host:8951/mcp/...` 直连，后端通过 `http://research-host:8951/ingest/wiki` 写入。
     - **`transcription` 服务（profile: `transcription`）**：挂载 `dalton-asr-jobs:/var/lib/dalton/asr_jobs`，接入 `core_internal` 与 `sandbox-net`，通过服务名 `http://transcription:8965` 提供转写服务（同时配置转写专用对象存储以通过 `src/dalton/capabilities.py:425-433` 门控）。

### 5.2 容器环境更新与 Dalton 迁移执行铁律（E16）

1. **修改 `.env` 后强制重建容器**：生产与核心部署中，常规 `docker restart` 会沿用旧容器创建时的环境快照（见 `scripts/dalton/ops/prod/deploy_main.sh:500-510`）。**所有涉及 `.env` 配置变更的切换步与回退步，一律执行 `docker compose up -d --no-deps backend` 重建后端容器**（涉及前端构建参数或静态包变更时同步重建 `edge`）。
2. **Dalton 迁移显式指定配置文件**：仓库根目录 `alembic.ini` 对应上游表，Dalton 迁移必须按 `scripts/dalton/docker_entrypoint_dalton.sh:18-21` 与 `scripts/dalton/ops/prod/deploy_main.sh:650,913` 的方式显式指定 `-c src/dalton/alembic.ini`（即 `uv run alembic -c src/dalton/alembic.ini upgrade head` 或 `downgrade <rev>`）。

### 5.3 macOS `launchd` 服务清单（Everflow 生产）

- **长期保留**：`com.dalton.alphaengine-mcp`（`:8950`）、`com.dalton.alphaengine-desktop-watchdog`、`com.dalton.colima`、`com.dalton.pg-backup`、`com.dalton.backend-watchdog`、`com.dalton.portwatch`、`com.dalton.channel-gateway`（`:8962`）、`com.dalton.monitor-feeds`。
- **分批新增**：第 1 批新增 `com.dalton.ops-status-exporter`（定时刷新状态文件）；第 2 批新增 `com.dalton.transcription`（`:8965`）；第 3 批新增 `com.dalton.research-host`（`:8951`）；第 5 批新增 `com.dalton.host-task-daemon`（`:8952`）。
- **分批缩减与下线**：`com.dalton.guidepoint-mcp-proxy`（`:8943`）在第 3 批观察期满后卸载；`com.dalton.researchdata-bridge`（`:8960`）在第 4、5 批缩减为仅服务 `/notify`、`/feishu/*` 与受限 `/mcp`（`discord-history`），待第 ③ 阶段渠道网关接手后卸载。

---

## 6. 分批计划

### 6.1 数据库迁移编号顺序

本设计第 1 批迁移编号为 **`d052`**（合入主干时接在 `d049` 之后）：
- **第 1 批**：**`d052_embedding_metadata_and_dimension`**（切换时随容器入口或手动升级执行）
- **第 2、3、4 批**：零数据库迁移
- **第 5 批观察期结束后单独执行**：清理迁移（先备份、按内置标记清理，`downgrade()` 从备份表恢复；设计原占位 `d051_cleanup_legacy_researchdata_mcp_rows`，合入时顺接当时唯一迁移头）

---

### 6.2 第 1 批：宿主平台配置 `DALTON_HOST_PLATFORM`、后端嵌入模块（`d052`）与 `/jobs/ops` 状态文件化

- **前置条件**：部署前完成 Postgres 全量备份（`com.dalton.pg-backup`）；合入主干时将 `d052` 顺接至当时唯一迁移头。
- **实施范围**：
  1. `src/dalton/capabilities.py` 实现 `resolve_host_platform()` 驱动 `alphaengine` 平台门控；`scripts/standalone/init.sh`、`scripts/dalton/bridge_subprocess_env.py`、`scripts/dalton/ops/prod/deploy_main.sh` 写入 `DALTON_HOST_PLATFORM`（8 项子能力的 `depends_on` 本批保持原样）。
  2. 新增 `src/dalton/embeddings/` 与迁移 **`d052_embedding_metadata_and_dimension`**（`dalton_embedding_metadata` 分列记录 `model_label` 与 `api_model_name`，保留 `scripts/dalton/research_runtime/skills/company-wiki/scripts/company_wiki_cli.py:54-61,219-231` 的模型映射与逐文本独立 `Content` 批量行为）；改造 `src/dalton/conversations/embedder.py`、`src/dalton/database/conversations.py`、`src/dalton/conversations/search.py`，并同步更新 `scripts/dalton/bridge_subprocess_env.py:1437-1447,1946-1950` 对归档 `render` 的环境校验。
  3. 新增 `scripts/dalton/ops/prod/export_ops_status.py` 与 `com.dalton.ops-status-exporter`；改造 `src/dalton/autonomy/system_jobs.py:232-315` 读状态文件，并将 `src/ptc_agent/agent/prompts/templates/components/chief_of_staff.md.j2:58`、`src/ptc_agent/agent/prompts/templates/components/user_profile.md.j2:92` 的“运维任务只读”门控切至 `ops_jobs_enabled`。
  4. *本批开发任务书须定义*：`dalton_embedding_metadata` 完整 DDL、Gemini/OpenAI 批量请求重试退避表、以及 `ops_jobs_status.json` 字段 schema 与过期阈值。
- **切换步骤顺序**：
  1. 加载 `com.dalton.ops-status-exporter`，确认 `var/dalton/ops_jobs_status.json` 生成；
  2. 更新 `.env` 写入 `DALTON_HOST_PLATFORM=macos`；
  3. 执行数据库备份，运行 `uv run alembic -c src/dalton/alembic.ini upgrade d052`（或由 `scripts/dalton/docker_entrypoint_dalton.sh:20` 在容器启动时执行）；
  4. 执行 `docker compose up -d --no-deps backend` 重建后端容器使新 `.env` 与代码生效。
- **回退步骤顺序**：
  - *仅切回桥 `/embed` 与 `/jobs/ops`（保持 `d052` 表）*：在 `.env` 设 `DALTON_EMBED_VIA_BRIDGE=true`、`DALTON_OPS_JOBS_VIA_BRIDGE=true`，执行 `docker compose up -d --no-deps backend` 重建后端容器。
  - *连同数据库与版本回退*：Everflow（3072 维保持原样）先停 `backend`，执行 `uv run alembic -c src/dalton/alembic.ini downgrade <pre_rev>`，切回上一版代码并 `docker compose up -d --no-deps backend`；**核心安装按 1536 维写入数据后，如需回退至 3072 维旧版本，须先使用升级前备份恢复数据库，再回退容器版本**。
- **验收条件**：① 对同一批多段中英文研究文本，对比旧桥 `/embed` 与新客户端输出向量的余弦相似度（浮点误差内 = 1.0）及历史归档查询 Top-K 结果完全一致；② 容器内 `sys.platform == "linux"` 下 Everflow `alphaengine` 保持开启；③ 核心安装 1536 维纯语义检索命中 HNSW 索引；④ 观察 14 天达标后移除桥上 `/embed` 与 `/jobs/ops`。

---

### 6.3 第 2 批：独立转写连接器服务 `dalton-transcription` 与排他锁前置交接

- **前置条件**：第 1 批已在生产稳定运行；已核对生产实际配置的 `DALTON_ASR_JOBS_DIR`（`scripts/dalton/researchdata_bridge.py:1477-1479`）与 `DALTON_ASR_USAGE_FILE`（`scripts/dalton/asr_jobs.py:2301-2306`）路径。
- **实施范围**：
  1. 交付 `src/dalton/transcription_service/`（含开源作业框架、通用 Whisper Worker 与私有 Seed-ASR 2.0 Worker 扩展口），在 `docker-compose.core.yml` 增加接入 `core_internal` + `sandbox-net` 的 `transcription` 服务，在 Everflow 增加 `com.dalton.transcription`（`:8965`）。
  2. **锁前置改造**：在旧桥 `_asr_job_manager()`（`scripts/dalton/researchdata_bridge.py:19039`）与新服务启动入口中，强制要求**先获取 `<DALTON_ASR_JOBS_DIR>/.owner.lock` 排他文件锁，再构造 `AsrJobManager` 实例、执行 `recover_incomplete_jobs()`（`scripts/dalton/asr_jobs.py:2321-2328`）及注册接单路由**；当设置停止接单/禁用标志时，跳过构造管理器与锁获取并直接返回 `503`。
  3. 同步更新 `src/dalton/services/transcriptions.py:461-584` 请求 `DALTON_TRANSCRIPTION_SERVICE_URL` + 独立管理凭据 `DALTON_TRANSCRIPTION_TOKEN`，移除 `CapabilityId.TRANSCRIPTION` 对 `RESEARCHDATA_BRIDGE` 的 `depends_on`（保留 `src/dalton/capabilities.py:425-433` 转写对象存储校验）。
  4. *本批开发任务书须定义*：停止接单排水（drain）控制信号、`.owner.lock` 超时与持有者 PID 诊断格式、核心安装 MinIO/S3 转写存储初始化说明。
- **切换步骤顺序**：
  1. **停止旧入口接新单**：向旧桥发送转写停止接单指令（或在维护窗口设置 `DALTON_BRIDGE_ASR_DISABLED=true`），等待当前转码/上传子步骤自然落盘或进入可恢复检查点，重启旧桥确认其已释放 `.owner.lock`；
  2. **新处理方拿锁恢复**：启动 `com.dalton.transcription`（配置与旧桥完全一致的 `DALTON_ASR_JOBS_DIR` 与 `DALTON_ASR_USAGE_FILE`），获取 `.owner.lock` 后构造 `AsrJobManager` 恢复未完成任务并监听 `:8965`；
  3. **重建后端切流**：在 `.env` 写入 `DALTON_TRANSCRIPTION_SERVICE_URL` 与 `DALTON_TRANSCRIPTION_TOKEN`，执行 `docker compose up -d --no-deps backend` 重建后端容器。
- **回退步骤顺序**：
  1. 先令 `com.dalton.transcription` 停止接单，确认进行中任务落盘后停止该服务并释放 `.owner.lock`；
  2. 将旧桥 `DALTON_BRIDGE_ASR_DISABLED=false` 重启，由旧桥重新获取 `.owner.lock` 构造管理器恢复任务；
  3. 恢复 `.env` 转写配置，执行 `docker compose up -d --no-deps backend` 重建后端容器。
- **验收条件**：切流前创建的历史已完成任务与进行中任务在切流后查询状态、下载 `/result/raw`、取消、重试及 `DALTON_ASR_USAGE_FILE` 计量全部正常；核心安装开启 `transcription` profile 与对象存储后跑通端到端转写。

---

### 6.4 第 3a 批：公开插件、开源 `research-host`、能力门控与核心安装

- **前置条件**：第 2 批已稳定运行。Everflow 生产保持原有旧桥运行，本批部署后生产行为保持不变。
- **实施范围**：
  1. **公开插件与打包白名单**：扩展公开插件 `plugins/dalton_sec/`（默认按未切流基线仅注册原有 2 项工具，由后端按能力显式传入 `DALTON_SEC_EXTENDED_TOOLS=1` 时启用全部 11 项兼容操作，见 2.3 节）；新增 `plugins/dalton_macro/`（`fred` 含 vintage、`treasury` TGA/债务）；新增公开声明插件 `plugins/dalton_company_wiki/` 与 `plugins/dalton_research_all/`；同步更新 `scripts/standalone/package_release.sh:219-227` 的 `ALLOWLIST_OPTIONAL`。
  2. **开源 `research-host` 服务（`src/dalton/research_host/`）**：提供 `python -m src.dalton.research_host`、`/healthz`、`/admin/drain`（停止接单与 `in_flight` 原子记账）、`/mcp/company-wiki`、`/mcp/federated`（动态按生效能力暴露工具清单与文档前缀，按来源白名单过滤查询参数，同步 SQLite 查询通过 `asyncio.to_thread` 卸载出事件循环）、`/ingest/wiki`（复用 PDF/DOCX/图片正文提取、保留原始附件文件、SQLite 事务内原子写入幂等回执并支持审计日志补写），并与后端读取同一份 `.env` 能力配置。
  3. **核心安装接入**：在 `docker-compose.core.yml`（默认随栈启动）与 `scripts/standalone/init.sh` / `upgrade.sh` / `backup.sh` / `restore.sh` 接入 `research-host`（同处 `sandbox-net` 与 `core_internal`），使独立核心安装开箱具备 `company_wiki` 与 `dalton_research_all`，并在升级与备份恢复中保持令牌与 Wiki 卷一致。
  4. **能力门控、数据地图与调用方适配**：更新 `src/dalton/capabilities.py`（解除 `guidepoint`、`company_wiki`、`sales_notes`、`tmtb`、`funda_ai` 对 `RESEARCHDATA_BRIDGE` 的 `depends_on`，并在未配置宿主连接器地址时保持 Everflow 生产默认清单不变）、`src/dalton/provenance/declared_sources.py`、`src/dalton/tools/__init__.py`、`web/src/hooks/useCapabilities.ts`、`web/src/pages/ChatAgent/components/dalton/AutoIngestToggle.tsx`、`src/dalton/data_map/builder.py`、`src/dalton/data_map/health.py`（独立核心按连接器探活、数据地图缓存键区分传输形态）、`src/dalton/content_assembly.py`；上线 `connector_transport.py` 并按工具与参数签名级别适配 `collect.py`、`rd_transport.py` 与 `workflows/topic-research/workflow.js`；使 `data_gateway.py` 与 `gp_search.py` 支持 `DALTON_GUIDEPOINT_MCP_URL`（缺省 `:8943/mcp`）。
  5. **商业连接器代码预置（默认关闭）**：`scripts/dalton/research_runtime/host_connectors/` 与 `extra_plugins/` 代码可预置在仓库（严格排除在核心安装包之外），在 Everflow 默认配置中处于未加载状态，留待第 3b 批正式切换启用。
- **验收条件**：
  1. Everflow 生产基线不变：`test_everflow_profile_parity.py` 全部通过；未切流工作区挂载旧桥与 2 工具基线 `dalton_sec` 时，技能脚本对 SEC 与研究路由的调用请求与改前完全一致；`dalton_sec` 在无 `src` 路径下启动时 `tools/list` 完整结构与基线一致。
  2. 独立核心能力完备：`package_release.sh` 白名单包含 4 个公开插件且排除商业连接器；`research-host` 默认随核心安装启动，跑通 `company_wiki` 读写（含 PDF/DOCX/图片附件入库、原始文件保留、图片索引、审计补写重试、`media="images"` 与 UTC `indexed_since`）、`dalton_research_all` 仅展示已启用来源与工具、`dalton_sec` 11 操作与 `dalton_macro` TGA/FRED 查询；共享 `.env` 关闭某来源后，后端与直连 `research-host` 同步返回 `403`；前端 `AutoIngestToggle` 待能力加载完成后按能力显示。

---

### 6.4b 第 3b 批（Everflow 切换）：宿主机 `com.dalton.research-host` 切流、Guidepoint 令牌刷新权交接与部署回退

- **前置条件**：第 3a 批合入并稳定运行；以下由第 3a 批双审确认的切换前置要求全部落地后方可开始切换。
- **实施范围与切换前置要求**：
  1. **宿主机服务与私有连接器启用**：新增 `scripts/dalton/launchd/com.dalton.research-host.plist.template`、`scripts/dalton/ops/prod/switch_research_host.sh`、`scripts/dalton/ops/prod/RESEARCH_HOST_SWITCH_RUNBOOK.md`，并更新 `scripts/dalton/ops/prod/deploy_main.sh` 与 `scripts/dalton/ops/research_runtime_migrate.sh` 纳入 `com.dalton.research-host`（`:8951`）拉起、探活与回退；在 Everflow 启用 `host_connectors`（`guidepoint`、`sales-notes`、`tmtb`、`funda-ai`）与 `DALTON_EXTRA_PLUGINS_DIR`，`all` 的 AlphaEngine 分支继续经旧桥 `:8960` 单处理方转发。
  2. **Guidepoint 单刷新方排他锁与真实路径对齐**：`guidepoint_mcp_proxy.py`（`:8943`）与 `host_connectors/guidepoint.py`（`:8951`）共用同一令牌文件路径与同一排他文件锁（读取令牌与开始接单前先取得锁）；切换脚本、LaunchAgent 模板与运维手册全部使用真实可部署绝对路径（仓库根、研究数据目录与持久化回滚快照目录）。
  3. **`:8943` 在途请求排空后再交接锁**：切换前先对 `:8943` 停止接单并确认在途请求归零后再停止代理、释放锁并启动 `:8951`，避免交接瞬间发生双刷新或请求中断；`/admin/drain` 与请求入口在读取请求体前完成原子 `draining` 检查与 `in_flight` 记账。
  4. **旧桥地址切换早于生产前端构建与持久化回滚快照**：切换时先将旧桥与宿主脚本的 `DALTON_GUIDEPOINT_MCP_URL` 指向 `:8951/mcp/guidepoint`、核验健康后再构建生产前端与重建后端；切换与部署开始即在持久化目录留存上一版配置（`.env`、bridge/host env、plist）与前端 `dist` 产物快照，保障重启后仍可一键完整回退。
  5. **`deploy_main.sh` 失败自动回退顺序**：部署开始先落盘快照；回退时先排空并停止当前 Guidepoint 令牌持有方，再恢复上一版配置并拉起目标持有方（`:8943` 或 `:8951`），随后重建后端容器并核验真实生效的 Guidepoint 入口与旧桥连通性。
  6. **前后端与宿主共享同一份能力配置**：生产 `com.dalton.research-host` 明确指向仓库 `.env` 读取 `DALTON_CAP_*`，与后端能力门控保持单一真相源。
- **切换步骤顺序**：
  1. 在持久化目录保存当前 `.env`、宿主配置、plist 与前端静态产物快照；
  2. 令 `com.dalton.guidepoint-mcp-proxy`（`:8943`）停止接单、待在途请求归零后停止进程释放令牌锁；
  3. 启动 `com.dalton.research-host`（`:8951`）独占取得令牌锁并探活 `/healthz`，将旧桥与宿主脚本的 `DALTON_GUIDEPOINT_MCP_URL` 切至 `http://127.0.0.1:8951/mcp/guidepoint` 并确认旧桥连通；
  4. 构建前端静态包，更新 `.env` 连接器地址与令牌，执行 `docker compose up -d --no-deps backend`（及重建 `edge`），并重刷全部活跃工作区沙箱同步新包装器。
- **回退步骤顺序**：
  1. 令 `com.dalton.research-host` 停止接单、待 `in_flight` 归零后停止进程释放令牌锁；
  2. 从持久化快照恢复 `DALTON_GUIDEPOINT_MCP_URL` 为 `http://127.0.0.1:8943/mcp` 并拉起 `com.dalton.guidepoint-mcp-proxy`；
  3. 恢复上一版代码、前端构建产物与 `.env`，执行 `docker compose up -d --no-deps backend` 重建容器，重刷活跃沙箱恢复旧包装器并核验真实入口。
- **验收条件**：Everflow 生产 `all.search` 正常联搜本地四库 + Guidepoint + 旧桥 AlphaEngine；实操演练一次完整切换与回退后，旧桥与 `gp_search.py` 均正常检索 Guidepoint 且无双刷新锁冲突。

---

### 6.5 第 4 批：`dalton-alphaengine`、`dalton-office`、金融/宏观/社区扩展切流与旧桥 `/mcp` 收窄为仅 `discord-history`

- **前置条件**：第 3 批观察期达标并完成旧子路由清理。
- **实施范围**：
  1. 在 `com.dalton.research-host`（`:8951`）启用 `/mcp/alphaengine`（含 `transcript`、SQLite v4 缓存、并发限流、熔断与 JSON `usage_file` 计数），并将 `/mcp/federated`（`all`）的 AlphaEngine 分支切为进程内直调；同步启用 `/mcp/office`（`tmeet` + `gcal`）、`/mcp/estimates`、`/mcp/cnhk`、`/mcp/market-regime`、`/mcp/community`。
  2. 新增 `CapabilityId.ESTIMATES`、`CN_HK`、`COMMUNITY_SOURCES`（同步加入 `web/src/hooks/useCapabilities.ts:16-50`），解除 `alphaengine` 对 `RESEARCHDATA_BRIDGE` 的 `depends_on`；更新 `src/ptc_agent/agent/prompts/templates/components/tool_guide.md.j2:23-27`、`agent_config.dalton.yaml:51-52`、`scripts/dalton/check_bridge.sh:38-65` 等探针脚本，并完成剩余全部技能脚本与 `SKILL.md` 迁移。
  3. **保留受限 `/mcp` 与专属过渡插件专供 `discord-history`（E10）**：旧桥 `:8960/mcp` 切换为 `DALTON_BRIDGE_MCP_MODE=discord_only` 受限模式，仅响应 `research(route="discord-history", operation="list_messages")`（`scripts/dalton/researchdata_bridge.py:13718-13719`），关闭其余全部 `/mcp` 工具与路由；同步上线受 `CapabilityId.CHANNEL_DISCORD` 门控的过渡插件 `plugins/dalton_discord_history/`（服务名 `dalton_discord_history`），将 `plugins/dalton/skills/dalton-wiki-auto-ingest/scripts/wiki_ingest.py:91-103` 切至 `tools.dalton_discord_history.research`，持续运行直至第 ③ 阶段渠道网关接手。
  4. *本批开发任务书须定义*：旧桥 `discord_only` 模式的拒绝错误码、各新增连接器在 `agent_config.dalton.yaml` 的超时表。
- **切换步骤顺序**：
  1. 在旧桥配置设置 `DALTON_BRIDGE_MCP_MODE=discord_only` 并重启旧桥，使其停止处理 AlphaEngine 请求并释放 `alphaengine_search_cache.sqlite3` 与 `usage_file` 写权；
  2. 热加载/重启 `com.dalton.research-host`（`:8951`）唯一接管 AlphaEngine 缓存库、当日读文账本及 `all` 内部 AlphaEngine 分支；
  3. 更新 `.env` 与前端构建（含新增能力枚举与 `DALTON_DISCORD_BRIDGE_MCP_URL`），执行 `docker compose up -d --no-deps backend` 重建后端容器，并重刷全部活跃沙箱包装器。
- **回退步骤顺序**：
  1. 先在 `com.dalton.research-host` 停用 AlphaEngine 模块释放缓存与计数文件写锁；
  2. 将旧桥 `DALTON_BRIDGE_MCP_MODE` 恢复并重启接管缓存；
  3. 回退代码与 `.env`，执行 `docker compose up -d --no-deps backend` 重建容器并重刷活跃沙箱。
- **验收条件**：① Everflow 跑通 AlphaEngine 全 9 项操作、`transcript`、`dalton-office`（`tmeet`/`gcal`）、`estimates`、`cn-hk`、`market-regime`、`community`；② **专项实测 `plugins/dalton/skills/dalton-wiki-auto-ingest/scripts/wiki_ingest.py` 的 Discord 频道消息分页拉取（`:474-480`）与失效附件链接单条刷新（`:718-724`）两项操作全部通过**；③ 观察 14 天达标后退役 `plugins/dalton_data/`（`researchdata`）并移除桥内除 `discord-history` 外的其余 `/mcp` 路由代码。

---

### 6.6 第 5 批：宿主任务守护剥离、桥缩减为渠道过渡代理及观察期后内置配置行清理（`d051`）

- **前置条件**：第 4 批观察期达标并完成旧 `plugins/dalton_data/` 及非 Discord `/mcp` 代码清理。
- **实施范围**：
  1. `/internal/{market-digest,ai-finance}/*` 迁入 `com.dalton.research-host`（`:8951`）独立管理口。
  2. 上线 `scripts/dalton/ops/prod/host_task_daemon.py`（`com.dalton.host-task-daemon`，监听 `:8952`），在构造状态管理器、`recover_host_runner_state_sync()`（`scripts/dalton/researchdata_bridge.py:19342`）与接单前先获取排他锁 `<data_root>/.dalton-host-runner.owner.lock`；同步更新 `scripts/dalton/ops/prod/deploy_main.sh` 纳入该守护进程。
  3. **缩减 `researchdata_bridge.py`**：仅保留 `/healthz`、`/notify`、`/feishu/*`（8 个子路径）及受限 `/mcp` 下的 `discord-history`，与第 ③ 阶段完全解耦。
  4. **观察期结束后单独执行的清理迁移 `d051_cleanup_legacy_researchdata_mcp_rows`**：仅识别带**内置桥标记**的记录——即 `workspace_mcp_servers` 中 `source = 'builtin' AND name = 'researchdata'`（见 `migrations/versions/012_add_mcp_servers.py:59-72`、`src/server/app/mcp_servers.py:516-528`）及 `user_mcp_builtin_disables` 中 `name = 'researchdata'`（见 `migrations/versions/027_user_mcp_builtin_disables.py:28-34`），完整保留 `user_mcp_servers`（`migrations/versions/012_add_mcp_servers.py:29-47`）中所有用户自建同名连接器；删除前将命中行原样写入备份表 `dalton_mcp_builtin_backup_d051`，`downgrade()` 从该备份表完整恢复。
  5. *本批开发任务书须定义*：`host_task_daemon.py` 停止接单排水接口、`d051` 备份表 DDL 与恢复校验 SQL。
- **切换步骤顺序（业务切流）**：
  1. **旧桥停止接宿主任务新单**：向旧桥下发宿主任务停止接单开关 `DALTON_BRIDGE_HOST_TASKS_DISABLED=true`，轮询确认旧桥内全部已启动 `active_runs` 进程组自然结束（或已完成状态落盘）后重启旧桥，确认其已释放 `.dalton-host-runner.owner.lock`；
  2. **新守护拿锁恢复**：启动 `com.dalton.host-task-daemon`（`:8952`），获取 `.dalton-host-runner.owner.lock` 后加载 `.dalton-host-runner-state.json`（延续 `max_seen_generation`、`pgid`+`lstart` 与 `fence_lost` 保护）；
  3. **重建后端切流**：更新 `.env` 中 `DALTON_HOST_RUNNER_URL`、摘要管理口地址及独立管理凭据，执行 `docker compose up -d --no-deps backend` 重建后端容器。
- **回退步骤顺序**：
  1. 令 `com.dalton.host-task-daemon` 停止接单，确认运行中 `pgid` 归零后停止进程释放 `.owner.lock`；
  2. 开启旧桥宿主任务处理并重启拿锁恢复，还原 `.env` 后执行 `docker compose up -d --no-deps backend` 重建容器。
  3. 若在观察期结束后已单独执行 `d051` 需回退，运行 `uv run alembic -c src/dalton/alembic.ini downgrade d050` 从 `dalton_mcp_builtin_backup_d051` 无损恢复原内置标记行。
- **观察期后清理（步骤 C）**：14 天双指标与 7 个宿主任务实跑全部通过后，先执行 Postgres 备份，再单独运行 `uv run alembic -c src/dalton/alembic.ini upgrade d051` 清理旧内置桥标记行，并合入瘦身后的 `researchdata_bridge.py`。
- **验收条件**：第 5 批独立上线运行，研究、嵌入、转写、宿主任务全部脱离 `:8960`；飞书文档、卡片通知与 `wiki_ingest.py` 的 `discord-history` 经缩减桥平稳运行直至第 ③ 阶段接手。

---

## 7. 测试

### 7.1 单元测试与全仓静态门控测试（`uv run pytest tests/unit/`）

1. **宿主平台判定解耦单测（`tests/unit/dalton/test_core_capabilities_gate.py:244-540`）**：断言 `DALTON_HOST_PLATFORM=macos|linux` 唯一决定 `alphaengine` 可用性，修改容器内 `sys.platform` 结果恒定。
2. **返回信封、部分失败状态与逐操作兼容契约测试**：
   - 用 `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py:946-1055` 的真实解析函数校验各连接器分页字段、`all.search` 的 `"partial"` 状态与 `source_status`、`all.read` 的 `image:` 前缀分派、Wiki 的 `media="images"` 与 UTC `indexed_since`、AlphaEngine 的 `auth_status` 与 9 类 `list_filters`；
   - 专项验证扩展后的 `dalton_sec` 对单值 `form`、缺省 `10-Q`、单值 `concept` 及 11 个操作返回结构的兼容性；验证 `expand_declared_sources`（`src/dalton/provenance/declared_sources.py:81-120`）对全部新服务名的溯源展开。
3. **逐来源权限与三类凭据隔离测试**：
   - 断言仅开启 `tmtb`、关闭 `sales_notes` 时，`all.search`、`sales_mail.search` 与 `all.read("mail:...")` 严格拒绝访问销售笔记数据；断言持沙箱调用令牌请求 `/ingest/*` 或 `/internal/*` 返回 `401/403`；断言沙箱容器挂载与环境变量零商业数据库、零供应商凭据。
4. **嵌入模型映射、独立 `Content` 批量与内置标记清理单测**：
   - 断言 `src/dalton/embeddings/` 将 `gemini-embedding-2-preview` 映射为 API 请求 `gemini-embedding-2` 且按每段文本独立 `Content` 构造批量请求；
   - 断言 `d051` 仅备份并删除 `workspace_mcp_servers(source='builtin')` 与 `user_mcp_builtin_disables` 中的 `researchdata` 行，完整保留 `user_mcp_servers` 中用户自建同名记录，且 `downgrade()` 精确还原备份行。
5. **转写与宿主任务锁前置竞争测试**：模拟旧进程仍持有 `.owner.lock` 时构造新 `AsrJobManager` 或 `host_task_daemon`，断言新进程在构造与 `recover_incomplete_jobs()` 之前即被锁拦截，消除双恢复窗口。
6. **全仓调用点全方位静态门控测试（新增 `tests/unit/dalton/test_researchdata_callers_accounted.py`）**：
   - 扫描范围覆盖：**代码与技能目录**（`src/`、`plugins/`、`workflows/`、`web/src/`、`scripts/`）、**仓库根目录配置文件**（`agent_config*.yaml`、`config*.yaml`、`docker-compose*.yml`、`.env*.example`）、**Jinja2 提示词模板与模板上下文变量**（`src/**/*.j2`、`researchdata_bridge_enabled`）、**环境变量名**（`DALTON_RESEARCHDATA_URL`、`DALTON_BRIDGE_*`）以及**拼接 URL 路径**（`/mcp/`、`:8960`、`/embed`、`/wiki/ingest`、`/sales-notes/ingest`、`/internal/`），确保每一处历史入口均按批次白名单受控收敛直至清零（仅保留第 ③ 阶段过渡白名单）。

### 7.2 GitHub Actions 从零安装与跨版升级集成测试（`.github/workflows/standalone-core-e2e.yml`）

由 `scripts/standalone/` 在零密钥 Ubuntu Runner 上执行：
1. **纯净发布包从零启动检查**：验证 `scripts/standalone/package_release.sh:219-227` 导出的核心包包含 `dalton_sec`、`dalton_macro`、`dalton_company_wiki`、`dalton_research_all`；启用 `wiki` profile 启动接入 `sandbox-net` 的 `research-host` 容器，验证沙箱通过 `http://research-host:8951` 完成 `company_wiki` 读写与 `all` 检索；验证 1536 维正交向量在零词面重叠卡片上真实命中 pgvector HNSW 检索；验证 `transcription` profile 端到端转写。
2. **从上一版升级与沙箱重装配检查**：在含存量沙箱与上一版数据库的环境中执行 `uv run alembic -c src/dalton/alembic.ini upgrade head` 与 `docker compose up -d --no-deps backend`，验证既有沙箱重连后更新包装器、关闭能力后旧路径拒绝访问，并验证 `d051` 在含用户自建同名 MCP 服务器场景下的升级与降级恢复。

### 7.3 Everflow 生产 Mac 实机验证与回滚演练

由 `scripts/dalton/ops/prod/` 在生产 Mac 上逐批执行：① 第 1 批实测新旧嵌入客户端对多段真实研报/对话文本的向量一致性与历史归档检索一致性；② 第 3、4 批实跑全部日常与周频研究技能，并专项验证第 4 批受限 `/mcp` 与 `dalton_discord_history` 下 `wiki_ingest.py` 的 Discord 消息分页与附件链接刷新；③ 每批上线观察前按 6.2–6.6 节步骤顺序实操演练一次回退（含 `docker compose up -d --no-deps backend` 重建容器、排他锁交接与 `deploy_main.sh` 失败回退链路）。

---

## 8. 风险与待用户决定的事项

### 8.1 主要技术风险与应对措施

| 风险点 | 影响说明 | 应对措施 |
|---|---|---|
| 修改 `.env` 后误用 `docker restart` 导致容器沿用旧环境快照 | 后端继续请求已停用的旧端口或旧开关 | 在运维手册与自动化切流脚本中固化 `docker compose up -d --no-deps backend` 重建容器，并在启动日志打印当前生效的各服务 URL 指纹核验 |
| 转写或宿主任务切换时 `AsrJobManager` 构造期自动触发恢复 | 若锁放在构造之后，运行事件循环中实例化即会抢跑恢复任务 | 将 `.owner.lock` 获取严格前置到进程入口（早于管理器实例化、状态恢复与 HTTP 监听），切换前先停止接单并确认旧进程释放锁 |
| 核心安装写入 1536 维向量后尝试原地降级旧代码 | 旧代码固定按 `halfvec(3072)` 查询会报错 | 在升级说明与 `d052.downgrade()` 中设置维度守卫，明确要求跨版本回退前使用升级前数据库备份恢复 |
| 过渡期 `all` 与 `sales-mail` 多源查询潜在越权 | 单一入口挂载开关仅控制连接器可见性，需进一步约束内部扇出范围 | 在 `dalton-research-host` 与旧桥内部对每次请求的候选来源与文档前缀逐一核对 `effective_capabilities()` |

### 8.2 待用户拍板的事项

无。
