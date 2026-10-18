# PK：公司知识层、发布治理、自主研究议程（开发指南草稿）

版本 0.3，2026-10-18。基座：LangAlpha main `8e0dce94`，分支 `dalton/main`。对应蓝图 §3、§4.6、§4.7、§4.8、§4.10。

**修订记录 v0.3（2026-10-18）**
- **按 owner 原则做减法**：核验只在发布时卡一次，草稿永不被阻止。删除拦截写入的 `DaltonKnowledgeGuardMiddleware` 和 DRIFT 自动还原；FilePanel、Write、Bash 的直接修改一律放行，本轮结束后检测并记为 `direct_edit` 版本，不合 schema 时只给分析师警告（PK.5）。硬性不变量只留三条：证据台账只追加；`official`（正式 house view）只能由 `publish_report` 写；目标价必须指向模型单元格。其余改为警告或删除（PK.4）。总工时由约 44 人日降到约 31 人日。
- **代码位置**统一到 `src/dalton/`（P0 的仓库卫生规则）：`src/dalton/{knowledge,publish,autonomy,database,tools,app}`；行为配置从 `config.yaml dalton:` 改到 `src/dalton/dalton.yaml`，经 `src/dalton/settings.py` 读取。路由并入 P0 的 `src/dalton/app/router.py`。
- **迁移**改用 P0 定下的独立 Dalton 链（`alembic_version_dalton`）：074/075/076 改为 `d003_knowledge`、`d005_publications`、`d006_autonomy`；`d004_provenance_locator` 由 P0D-P1 §P0-D.3b 提供。Dalton 表不建指向上游表的外键，去掉级联删除和 `dalton.allow_cascade` 开关。
- **工作区名 = ticker**（如 `META`），公司名和 tier 写在工作区描述里；兄弟路径写 `../META/thesis.md`。
- **模型键**改为 P0.4 的 `ocb-<provider>-<model>`；审稿家族用 P0.6 的 `role_policy.family_of()`，读 P0.4 overlay 里的 `dalton.family`。lead 模型可切换，不再假设作者是 Claude。
- **预算接口名**对齐 P0.8：`get_budget_today(user_id)["remaining_usd"]`、`get_run_charge_usd(run_ref)`。时区定为 `Asia/Shanghai`；日额度与旧 Dalton 分开，金额待 owner 定。
- **五词结论**用 Playbook 正式词形：`NO_CHANGE`、`THESIS_STRENGTHENED`、`THESIS_WEAKENED`、`THESIS_BROKEN`、`NEW_THESIS`；md 和界面显示 无变化／强化／削弱／证伪／新论点。
- `publish_report` 的 kind 加 `bullbear`（PS 用）。
- **财报日期**只认公司官方来源；FMP 等日历只作"待确认"候选；每周六刷新（由 PS 的 `dalton-earnings-calendar.json` 执行）。
- **自动任务命名**对齐 PS：`dalton-<t>-delta.json`、`dalton-<t>-move-up.json`、`dalton-<t>-move-down.json`、`dalton-earnings-calendar.json`，预检逐一识别；夜间 thesis 增量的预检用后端经 researchdata 桥的一致预期零 LLM 探针。
- **Home 给公司工作区写自动任务**改走后端 REST helper `PUT /api/v1/dalton/automations/{name}`，不跨工作区写文件。
- **权限已授予**：研究总监派单自动批准（`auto_approve_handoffs`）为必做配置；分析师可自主建、改自己的 `dalton-*` 自动任务文件。
- 基金 wiki 只读：本章不含任何 wiki 回写。合规口径已定（专家访谈、管理层会议、Guidepoint 可用，写身份不写姓名）。本阶段只做美股，港股、A 股探针移到 P6。
- 去掉所有"次数"类限制（例如研究总监"一次最多派 3 个"改为按额度和 Redis 槽位自然排队）；只保留美元额度、按时间退避，以及供应商配额（80% 告警，不设闸门）。
- 开放问题只保留 owner 的 6 项（§6.3），其余均已决定。

本章交付三块 LangAlpha 没有的能力，外加界面要做的最小改动：

| 模块 | 步骤 | 对应蓝图阶段 |
|---|---|---|
| 公司知识层 | PK.1 – PK.7 | P1（入职迁移）起就要用，P4 随事件更新 |
| 发布治理 | PK.8 – PK.13 | P3（完整覆盖后正式发布） |
| 自主研究议程 | PK.14 – PK.20 | P4 – P5 |
| 界面 | PK-UI.1 – PK-UI.5 | 贯穿各阶段 |

> 本章只给方案和步骤，不改代码。凡标【待验证】的点，实施者动手前必须先读代码或真实调用确认；标【依赖】的点由其他章节提供。

---

## 0. 总览

### 0.1 本章做出的关键决定

| # | 决定 | 理由（含代码依据） |
|---|---|---|
| D0 | **核验只在发布时卡一次；草稿永不被阻止**（owner 原则）。知识层的写入不拦截；直接修改事后检测、记录、提示，不还原。硬性不变量只有三条（PK.4） | 避免重建旧 Dalton 的治理负担；把确定性的硬约束集中在 `publish_report` |
| D1 | **每轮工作区快照不能满足版本对比，要另建 `dalton_knowledge_versions` 表；再写一个给人看的 `history/` 日期副本目录** | `workspace_files` 每个路径只有一行，`UNIQUE (workspace_id, file_path)`，每轮备份时 upsert 覆盖（`src/server/database/workspace_file.py` L74/L109）。旧 blob 变孤儿，7 天后标记待删，再过 24h 被删（`src/server/services/workspace_file_gc.py`，`blob_keys.py` 的 `GC_GRACE_DAYS=7`、`GC_CONDEMNED_GRACE_HOURS=24`）。代码里没有任何"列历史版本"或"取旧版本"的 API。蓝图 §3.1 第 5 条要求"历史观点不被静默覆盖"，所以真值放 Postgres。 |
| D2 | **知识层由后端原生直连工具写入**（`knowledge_update`、`knowledge_append_verdict`）。不走 MCP，也不走 PTC 沙箱封装 | 原生 LangChain 工具是模型的一次 JSON 调用，UI 能逐个渲染，中间件也能看见。它和 `Read`/`Write` 共用同一个 `filesystem_backend`（`src/ptc_agent/agent/agent.py` L416），能直接读写沙箱文件、写 Postgres。`tool_binding.py` 只管 MCP 工具（见 0.2），原生工具天然就是 direct 路径。 |
| D3 | **文件布局：人读的 `.md` 放工作区根目录，机器读的 JSON 放 `data/`**。`.md` 由工具从 JSON 渲染生成。**已定**，P0D-P1 的 P1.3 骨架和 P1.4 迁移都以 PK.1 为准 | owner 要求 FilePanel 打开就能读懂（FilePanel 渲染 markdown；JSON 只有语法高亮，没有专用查看器：`filePanel/FileViewer.tsx`、`fileMeta.ts`）。 |
| D4 | **PM 审批卡直接复用现有的 `ToolApprovalCard`**。interrupt 带上 `attempt_id`，前端零改动就会路由到这张卡；裁决走 `order_decisions[attempt_id]` | `isToolApprovalRequest` 只要看到 `request.attempt_id` 就判为工具审批（`web/src/pages/ChatAgent/session/interrupts/toolApprovalCard.ts` L19-22）。`buildToolApprovalResumeEntry` 回传 `order_decisions` 按 attempt 键入的裁决（`answerBoard.ts` L44-52）。后端 `HITLResponse.order_decisions` 已经存在（`src/server/models/chat.py` L68-88）。 |
| D5 | **独立审稿的权威结论由服务端产生**：`publish_report` 内部调用 `LLMService.complete`，用另一模型家族的模型，结论写进数据库。另外为草稿阶段的自愿审阅定义一个 subagent `.md`，两处共用同一份 prompt | 子智能体没有只读开关。`filesystem` 工具组本身就意味着可写（`subagents/compiler.py:227`，`writes_files=... or "filesystem" in bound`）。如果靠 subagent 产出审稿结论，分析师可以不派、改写或伪造。服务端调用不带工具，天然只读，而且绕不过去；也因此 CLI gateway 类的 text-only 模型可以当审稿人（P0.3、P0.6）。 |
| D6 | **事件触发不扩展 `TriggerType`，用"轮询 cron + 零成本预检"** | `TriggerType = Literal["cron","once","price"]`（`src/server/models/automation.py` L309）。数据库 CHECK、文件格式、调度器和前端 `TriggerSection` 都按这三种写死。轮询 cron 已经有多 worker 安全的抢占（`claim_due_automations` 用 `FOR UPDATE SKIP LOCKED`，`src/server/database/automation.py` L484/L518），预检能把空跑成本压到 $0。 |
| D7 | **预检挂在 `AutomationExecutor.execute()` 里、`pending→running` 之前**。新增 `Outcome.PRECHECK_SKIPPED`（`from_statuses=("pending",)`），新增 `SkipReason` 值 `"precheck"` | 这个位置在信用闸门（L857）、建线程、启动投递（L888）和 `astream_ptc_workflow`（L741）之前。现有的 `Outcome.SKIPPED` 只允许从 `waiting` 结束（`automation_settlement.py` L187-192），不能复用。 |
| D8 | **界面原生复用，不新建页面**。只做三件事：隐藏交易、行情、券商入口（Dalton 模式下 `/dashboard` 也隐藏）；发布审批复用卡片；中文界面 | owner 2026-10-18 的澄清。可点击的版本对比页、覆盖总览页列入"以后，仅在 owner 要求时做"。 |
| D9 | **工作区名 = ticker**（`META`），公司名、tier、一句话判断写在工作区描述 | 和 P0D-P1 的 P1.2/P1.3 一致；兄弟路径简单（`../META/thesis.md`） |
| D10 | **删除工作区不删 Dalton 记录**。Dalton 表只存 id、不建外键，工作区删除后版本、结论、发布记录留作档案 | P0.8 的"不建外键"规则；省掉级联删除和只追加触发器之间的冲突处理 |

### 0.2 已核实的代码事实（实施时直接引用）

| 主题 | 事实 | 位置 |
|---|---|---|
| 工作区备份 | 每轮结束 `on_background_workflow_complete` → `ws_manager.backup_changed_projects(...)` → `sync_to_db` | `src/server/handlers/chat/ptc_run.py` L907/L926；`src/server/services/persistence/backup.py` L106 |
| 备份排除 | `.agents/user`、`.agents/workflows`、`.agents/memory`、`.agents/transcripts`、`.git` 等不进镜像 | `src/server/services/persistence/transfer.py` L60-93；`src/ptc_agent/core/paths.py` L443-472 |
| 后端写沙箱 | `PTCSandbox.awrite_file_text`、`aupload_file_bytes` | `src/ptc_agent/core/sandbox/ptc_sandbox.py` L1727/L1732 |
| 文件工具后端 | `backend.normalize_path`、`aread_text`、`awrite_text`、`aedit_text` | `src/ptc_agent/agent/tools/file_ops.py` L128-L383 |
| agent.md | 每轮从沙箱读取（不缓存，跨 worker 安全），上限 `MAX_AGENT_MD_SIZE=32768`，按 epoch 冻结；改动以 `agent_md_changed` 行送达模型 | `runtime_context/baseline.py` L453/L745；`agent/tools/context_file_policy.py`；`runtime_context/epoch.py` L871 |
| 记忆索引 | `MAX_MEMORY_BLOCK_SIZE=32768`，每轮读 `memory.md`，按 epoch 冻结 | 同上；`components/memory.md.j2`、`envelope/baseline_memory.md.j2` |
| 写入中间件 | `FileOperationMiddleware.awrap_tool_call` 拦截 `Write`/`Edit`，能看到 `file_path`；`ProtectedPathMiddleware` 演示了如何短路一次工具调用 | `middleware/file_operations/sse_middleware.py` L101；`middleware/tool/protected_path.py` |
| 共享中间件顺序 | CreditGate → RepeatCallGuard → ToolArgumentParsing → ProtectedPath → CodeValidation → ToolErrorHandling → LeakDetection → ToolResultNormalization → FileOperation (L502) → Provenance → TodoWrite | `src/ptc_agent/agent/agent.py` L474-516 |
| 主智能体专属工具挂点 | `if role == "chief_of_staff": tools = [*tools, *CHIEF_OF_STAFF_TOOLS]` 在 L997-1000，其后 L1001 接直连 MCP 工具，L1004 调 `create_agent(...)` | `src/ptc_agent/agent/agent.py` |
| HITL | `interrupt({"action_requests":[...]})`；resume 经 `POST /api/v1/threads/{id}/messages` 的 `hitl_response`；SSE 事件类型 `interrupt`，转发 `kind` | `src/tools/secretary/_commands.py`；`src/server/services/runs/sse_producer.py` L994；`src/server/handlers/chat/ptc_run.py` L775-783 |
| 批一次执行一次 | `consume_attempt`：`UPDATE … SET status='submitting' WHERE attempt_id=%s AND status='approved' RETURNING`；proposal 按 `UNIQUE(user_id,message_id,tool_call_id)` 幂等 | `src/ptc_agent/agent/middleware/order_governance.py` L256/L390；`src/server/database/order_attempts.py`；`migrations/versions/043_order_attempts.py` |
| 无人值守 run 中的 interrupt | 自动任务 run 停在 HITL interrupt 上（信用暂停除外）记为 FAILED | `src/server/services/automation_settlement.py` 约 L202-235 |
| 线程来源 | `conversation_threads.metadata->'origin'->>'type'` 取值 `agent` / `automation` / `system`；没有这个键表示用户发起 | `src/server/database/conversation/threads_write.py` L97-105；`threads_read.py` L362 |
| 来源记录 | `provenance_records` 按 thread、turn、response 记录，没有 `workspace_id`；结果正文在 `provenance_result_bodies`，按 sha256 寻址；文件读取只存 sha、size、snippet | `migrations/versions/013_*`、`015_*`；`middleware/provenance/middleware.py` L707-734 |
| 来源 API | `GET /api/v1/threads/{id}/provenance`、`/provenance/bodies`、`/provenance/{rid}/body?full=true` | `src/server/app/threads/provenance.py` L31/L106/L182 |
| 子智能体文件 | `plugins/<bundle>/agents/<name>.md`，YAML frontmatter：`description`（必填）、`model`、`effort`、`tools`、`preferred_skills`、`available_to` | `src/server/services/plugins/agents.py`；`subagents/merge.py` L24/L208/L237 |
| 一次性 LLM 调用 | `LLMService.complete(*, user_id, user_prompt, system_prompt, response_schema, slot, request_model, reasoning_effort, return_token_usage)` | `src/server/services/llm/service.py` L54 |
| 自动任务文件 | `.agents/user/automations/<name>.json`，名称正则 `[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.json`；`_Entry` 设 `extra="forbid"`，文件里不能写 `metadata`；一次性任务跑完后不能再改（"Write a new file for another run."） | `src/server/services/automations/file.py` L84/L262/L452；`paths.py` L87/L497 |
| 预检插入点 | `execute()`：L812 `pending→running`，L857-858 信用闸门，L861-867 建线程，L888 投递，L891 `_run_turn` | `src/server/services/automation_executor.py` |
| 迁移编号 | 上游最新 `073_drop_sse_events.py`。Dalton 不接上游链：用 P0.8 定下的独立链 `src/dalton/migrations/`（`version_table="alembic_version_dalton"`，revision 为 `d001`、`d002`…），由 `scripts/dalton/docker_entrypoint_dalton.sh` 在上游迁移之后执行；同样用 `op.execute` 执行原生 SQL | `migrations/versions/`；P0.1、P0.8 |
| 路由注册 | Dalton 的路由统一挂在 P0.7 建立的 `src/dalton/app/router.py`（`prefix="/api/v1/dalton"`），已经由钩子 `DALTON-HOOK(router)` 注册进 `src/server/app/setup.py`，本章不再改 setup.py | P0.7 第 2 节 |
| 依赖 | `jsonschema>=4.0` 和 `referencing` 是主依赖；`openpyxl` 只装在沙箱镜像里；PDF 渲染在 `src/server/services/pdf_render.py`，属于 `pdf` 可选依赖 | `pyproject.toml` L111/L138/L156 |

### 0.3 新增代码的总体位置

按 P0.1 的仓库卫生规则，Dalton 后端代码只放在 `src/dalton/`；上游文件只打带 `DALTON-HOOK(<id>)` 标记的钩子级补丁，清单见 §5。

```
src/dalton/                                 P0 已建的后端包根（settings.py、dalton.yaml、budget/、llm/、app/、database/、migrations/）
  dalton.yaml                               本章加 knowledge / publish / autonomy 段（§0.5），经 src/dalton/settings.py 读取
  knowledge/{paths.py,schemas/,ops.py,validate.py,render.py,templates/,service.py,reconcile.py}
  publish/{numbers.py,match.py,audit.py,review.py,service.py,files.py}
  autonomy/{preflight.py,probes/,backoff.py,board.py,report.py}
  database/                                 原生 SQL（P0 已有 budget.py、openclaw_sync.py）
    knowledge.py publications.py verdicts.py watch.py plans.py
  tools/                                    原生直连工具
    __init__.py (build_dalton_tools) knowledge_tools.py publish_tools.py director_tools.py
  app/router.py                             P0.7 已建；本章追加只读与运维 API（前缀 /api/v1/dalton）
  migrations/versions/d003_knowledge.py     新建（down_revision="d002"）
  migrations/versions/d005_publications.py  新建（down_revision="d004"）
  migrations/versions/d006_autonomy.py      新建（down_revision="d005"）
plugins/dalton/agents/dalton-reviewer.md    新建（plugins/dalton/ 包本身由 P0D-P1 的 P1.1 创建【依赖】）
plugins/dalton/skills/{dalton-knowledge,dalton-publish,dalton-agenda,dalton-research-director}/SKILL.md 新建
```

> 迁移链（全部在 Dalton 独立链里，和上游编号不会冲突）：`d001_budget`（P0.8）→ `d002_openclaw_sync_runs`（P0.7）→ `d003_knowledge`（PK.3）→ `d004_provenance_locator`（P0D-P1 §P0-D.3b，P3 执行）→ `d005_publications`（PK.8）→ `d006_autonomy`（PK.16）。Dalton 表**不建指向上游表的外键**，只存 id（P0.8 的规则）。

### 0.4 外部依赖（其他章节）

- 【依赖】`plugins/dalton/` 插件包骨架和 `dalton-conventions` skill（P0D-P1 的 P1.1）；覆盖名单 `config/dalton/coverage.yaml`（P1.2）；公司工作区骨架（P1.3）；旧认知迁移脚本 `scripts/dalton/migrate_legacy_knowledge.py`（P1.4，经本章 PK.7 的 import API 写入）。
- 【依赖】本地预算模块（P0.8）。本章只调用 `src/dalton/budget/service.py` 的两个函数，字段名与 `GET /api/v1/dalton/budget/today` 相同：
  - `async get_budget_today(user_id) -> dict`：取 `remaining_usd`、`spent_usd`、`cap_usd`、`open_reserved_usd` 等（未结算预留按全额占用，P0.8 第 4 节）；
  - `async get_run_charge_usd(run_ref) -> Decimal | None`：单次运行的入账额（= `runs[].charge_usd`）。
- 【依赖】模型角色策略（P0.6 的 `src/dalton/llm/role_policy.py`）：`family_of(model_key)`，以及 P0.4 overlay 里每个 `ocb-*` 条目的 `dalton.family`、`dalton.upstream_provider`、`dalton.openclaw_ref`。
- 【依赖】数据接入（P0D-P1）：researchdata 桥 `DALTON_RESEARCHDATA_URL`（后端容器可直达，P0-D.0 已验证），预检的一致预期探针和"内部新材料"探针都经它查询，零 LLM；拿不到时对应探针关闭，不影响其他探针。数字核验的逐文档来源依赖 §P0-D.3b 的 `d004_provenance_locator`（P3）。
- 运行环境：本地 Docker 沙箱（colima），所有公司工作区共用一台 computer（P0.1）；不用 Daytona。

### 0.5 `src/dalton/dalton.yaml` 新增段（PK 各步共用）

P0.1 已建 `src/dalton/dalton.yaml`（行为默认值）和 `src/dalton/settings.py`（读取器，P0.8 的 `budget:` 段也在这里）。本章追加：

```yaml
enabled: true
pm_user_ids: ["local-dev-user"]          # 单用户阶段：owner 一人。多用户阶段改为角色表
timezone: "Asia/Shanghai"                # 已定：研究总监排程、议程到期、财报日历刷新都按此时区（与 P0.8 预算日一致）
knowledge:
  max_doc_bytes: 1048576                 # 单个 JSON 文档上限 1 MiB（超出只警告，见 PK.3）
publish:
  # 按顺序取第一个"家族 ≠ 作者家族、且可用"的模型；键名是 P0.4 生成的 manifest 键
  reviewer_models: ["ocb-openai-gpt-6.1-sol", "ocb-google-gemini-4-argon",
                    "ocb-claude-cli-gateway-claude-opus-5-5", "claude-opus-5-5-oauth"]
  extract_model: null                    # 正文数字的"指标/期间"标注用后台 slot；null = 后台默认模型
  approval_ttl_hours: 168
  max_file_bytes: 20971520
  consensus_providers: ["estimates:", "yf_analysis"]   # PK.9：一致预期数字只能匹配 provider 以这些前缀开头的来源记录
autonomy:
  enabled: true
  min_wake_usd: 0.50                     # 当日 remaining_usd 低于此值，预检一律 skip(budget)
  event_reserve_ratio: 0.2               # 研究总监给事件唤醒留的额度比例
  preflight_timeout_s: 20
  backoff: { base_hours: 24, max_days: 30 }
  vendor_quota_alert_ratio: 0.8          # 供应商配额（如 AlphaEngine 全文）用到 80% 只告警，不设闸门
```

读取方式：`src/dalton/settings.py` 提供 `get_dalton_config()`，Pydantic 模型也放在这里。不允许直接 `os.getenv`（`src/server/AGENTS.md`）。日额度金额由 owner 定（§6.3），写在 P0.8 的 `budget.default_daily_cap_usd` 或设置页里，本章不另设额度。

---

## 1. 公司知识层（PK.1 – PK.7）

### PK.1 工作区文件布局与命名

**目标**：每家覆盖公司对应一个工作区，文件布局固定，在 FilePanel 文件树里一眼能读懂；人读的和机器读的分开放。

**前置依赖**：无。

**改动/新增文件**：
- 新建 `src/dalton/knowledge/paths.py`：只放常量和路径函数。
- 新建 `plugins/dalton/skills/dalton-knowledge/SKILL.md`：教分析师布局和写法，见 PK.6。

**具体做法**：

工作区命名：**工作区名就是 ticker**（例如 `META`，与 P0D-P1 的 `coverage.yaml` 里 `workspace.name` 一致）。工作区列表就是覆盖名单（D8）。工作区描述写"公司名 · Core 或 Watch · 一句话判断"，由 `knowledge_update` 写 `company` 文档时顺带同步（PK.4）。

本节的布局是**权威版本**：P0D-P1 的 P1.3（骨架）和 P1.4（迁移）都指向这里。P1.3 只建 `agent.md`、各目录和 README；`data/*.json` 由 PK.7 的 import（4 家迁移公司）或分析师第一次 `knowledge_update(op=create)` 生成。

```
<工作区根>/                 文件夹名 = ticker，如 META
  agent.md                  这家公司的目标、当前一句话判断、各文件索引；托管块由工具维护（PK.6）
  thesis.md                 [渲染] thesis 支柱、阈值、证据台账、house view、正式版本
  debate.md                 [渲染] 当前 top debates
  drivers.md                [渲染] 关键驱动、领先指标、最新读数、敏感度
  management.md             [渲染] 管理层与指引风格、指引对实际的历史
  lessons.md                [渲染] 经验教训（含已撤回）
  agenda.md                 [渲染] 研究议程
  verdicts.md               [渲染] 事件五词结论日志（最近 60 条，全量在数据库）
  data/                     机器真值；正常经 knowledge_* 工具写，直接修改会被记录（PK.5）
    company.json thesis.json debate.json drivers.json management.json lessons.json agenda.json
    models.json             财务模型版本登记
    consensus/<YYYY-MM-DD_HHMM>.json   一致预期快照（P0D-P1 的 P0-D.6；不受版本表管理）
  model/                    财务模型：<TICKER>-model-v<NN>.xlsx（每个版本一个新文件，不覆盖旧版）
  research/                 事件笔记、专题研究、取证材料（自由文件）
    <YYYY-MM-DD>-<slug>.md
  drafts/                   待发布草稿：<slug>.md，可选 <slug>.html、<slug>.xlsx；核验侧车 <slug>.audit.md
  published/                正式发布；按约定不改写，哈希记在数据库（PK.8），被改动时只警告
    INDEX.md                [渲染] 发布清单
    <YYYY-MM-DD>-<slug>.md / .pdf / .xlsx
  history/                  给人看的日期副本（真值在数据库）
    <doc>/<YYYYMMDDTHHMMSSZ>-v<NNNN>.json
  sources/                  迁移和原件副本（P1.4），只读约定
```

`paths.py` 常量：

```python
DOC_KEYS = ("company", "thesis", "debate", "drivers", "management", "lessons", "agenda", "models")
DATA_DIR = "data"; HISTORY_DIR = "history"; PUBLISHED_DIR = "published"; DRAFTS_DIR = "drafts"; MODEL_DIR = "model"
RENDERED = {"thesis": "thesis.md", "debate": "debate.md", "drivers": "drivers.md",
            "management": "management.md", "lessons": "lessons.md", "agenda": "agenda.md"}
VERDICTS_MD = "verdicts.md"; PUBLISHED_INDEX = "published/INDEX.md"
WATCHED_GLOBS = ("data/*.json", "published/**", *RENDERED.values())   # 只用于事后检测（PK.5），不拦写
def data_path(doc_key: str) -> str: ...          # "data/thesis.json"
def history_path(doc_key: str, version_no: int, at: datetime) -> str: ...
```

生成的 `.md` 第一行固定写：`<!-- 由 data/<doc>.json 渲染（版本 v<N> · <时间>）；直接修改会被记录，请用 knowledge_update 合入 -->`。

**验证方式**：先跑 PK.4，在一个测试工作区（例如 `ZZTEST`）初始化后，在 LangAlpha 界面的 FilePanel 里展开文件树截图。

**验收标准**：
- 文件树和上面一致；
- 打开 `thesis.md` 是可读的中文 markdown 表格，不是 JSON；
- `data/` 下都是 JSON，能语法高亮。

**风险与注意**：
- 工作区文件夹名跟着工作区名走，改名后下次获取 computer 时才改（根 `AGENTS.md`）。路径一律用相对工作区根的路径，不要写死绝对路径。
- `.agents/` 下的目录不进备份；知识文件必须放在工作区根下的普通目录里，才会进 `workspace_files` 镜像。

**预估工时**：0.5 人日。

---

### PK.2 JSON Schema（全部写出）

**目标**：给每个知识文档一份 JSON Schema（draft 2020-12）。`knowledge_update` 写入时校验结构（结构不对就无法渲染）；直接修改的内容不合 schema 时只记录并警告（PK.5）。schema 本身也给分析师读，教它怎么写。schema 只管结构，不承载治理规则。

**前置依赖**：PK.1。

**改动/新增文件**：新建 `src/dalton/knowledge/schemas/` 下的 `common.schema.json`、`company.schema.json`、`thesis.schema.json`、`debate.schema.json`、`drivers.schema.json`、`management.schema.json`、`lessons.schema.json`、`agenda.schema.json`、`models.schema.json`、`verdict.schema.json`。

**具体做法**：

通用约定：
- 所有对象带 `"additionalProperties": false`。
- id 一律由服务端分配（工具入参里可以省略，见 PK.4）。
- 时间用 ISO 8601 带时区；日期用 `YYYY-MM-DD`。
- `evidence_refs` 统一用 `EvidenceRef` 结构。
- 校验器用 `jsonschema.Draft202012Validator`，配一个空的 `referencing.Registry`，不允许远程 `$ref`（`pyproject.toml` L111-114 的约定）。
- `$id` 一律用绝对 URI `https://dalton.local/schemas/<name>`，跨文件引用写相对形式 `common#/$defs/...`，由它解析到 `https://dalton.local/schemas/common`。所有 schema 在启动时预先装进 `Registry`（这不是网络地址，不会去拉取）。

#### PK.2.1 `common.schema.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://dalton.local/schemas/common",
  "$defs": {
    "isoTime": {"type": "string", "format": "date-time"},
    "isoDate": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
    "period": {"type": "string", "pattern": "^(FY\\d{4}|[1-4]Q\\d{2}|1H\\d{2}|2H\\d{2}|LTM-\\d{4}-\\d{2}|\\d{4}-\\d{2}(-\\d{2})?|NTM|spot)$",
               "description": "FY2026 / 3Q26 / 1H26 / LTM-2026-09 / 2026-09 / 2026-09-30 / NTM / spot"},
    "unit": {"type": "string", "enum": ["USD","USD_mn","USD_bn","HKD","HKD_mn","HKD_bn","CNY","CNY_mn","CNY_bn","CNY_100mn","pct","bp","x","count","count_mn","per_share","other"]},
    "label": {"type": "string", "enum": ["reported","company_guidance","consensus","estimate","assumption","research_judgement"],
              "description": "蓝图 §3.1 第 2 条：估计、假设、公司指引、一致预期分层标注"},
    "sourceTier": {"type": "integer", "minimum": 1, "maximum": 6,
                   "description": "1 filing > 2 公司公告 > 3 署名研报 > 4 官方/协会数据 > 5 专家(标身份) > 6 新闻(只作线索)"},
    "evidenceRef": {
      "type": "object", "additionalProperties": false,
      "properties": {
        "kind": {"enum": ["provenance","file","model_cell","evidence","url","publication"]},
        "provenance_record_id": {"type": "string", "format": "uuid"},
        "path": {"type": "string"},
        "cell": {"type": "string", "description": "Sheet!A1，如 Model!F42"},
        "evidence_id": {"type": "string", "pattern": "^E-\\d{8}-\\d{2,3}$"},
        "url": {"type": "string"},
        "publication_id": {"type": "string", "format": "uuid"},
        "chain_id": {"type": "string", "description": "同一份研报被多处转述只算一条来源链（§3.1-3）"}
      },
      "required": ["kind"]
    },
    "figure": {
      "type": "object", "additionalProperties": false,
      "properties": {
        "metric": {"type": "string"}, "value": {"type": "number"}, "unit": {"$ref": "#/$defs/unit"},
        "period": {"$ref": "#/$defs/period"}, "label": {"$ref": "#/$defs/label"}
      },
      "required": ["metric","value","unit","period","label"]
    },
    "threshold": {
      "type": "object", "additionalProperties": false,
      "properties": {
        "id": {"type": "string", "pattern": "^T\\d+$"},
        "metric": {"type": "string"},
        "operator": {"enum": [">=","<=",">","<","between","event"]},
        "value": {"type": "number"}, "value2": {"type": "number"},
        "unit": {"$ref": "#/$defs/unit"}, "period": {"$ref": "#/$defs/period"},
        "event_text": {"type": "string", "description": "operator=event 时描述触发事件"},
        "label": {"$ref": "#/$defs/label"},
        "source_hint": {"type": "string", "description": "去哪里取读数：如 10-Q 分部表、公司电话会"}
      },
      "required": ["id","metric","operator","label"]
    },
    "signal": {"enum": ["strongly_confirming","confirming","mildly_confirming","neutral","mildly_weakening","weakening","strongly_weakening","mixed","invalidating","untested"],
               "description": "沿用 thesis-tracker 的刻度；禁止相加成分数"},
    "pillarStatus": {"enum": ["strengthening","intact","watch","impaired","broken","changed","untested","retired"]},
    "verdictWord": {"enum": ["NO_CHANGE","THESIS_STRENGTHENED","THESIS_WEAKENED","THESIS_BROKEN","NEW_THESIS"],
                    "description": "Playbook 正式词形；md 和界面显示 无变化/强化/削弱/证伪/新论点"}
  }
}
```

#### PK.2.2 `company.schema.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://dalton.local/schemas/company",
  "type": "object", "additionalProperties": false,
  "properties": {
    "schema_version": {"const": 1},
    "ticker": {"type": "string", "pattern": "^[A-Z0-9.]{1,12}$"},
    "exchange": {"enum": ["NASDAQ","NYSE","HKEX","SSE","SZSE","OTHER"]},
    "name": {"type": "string"}, "name_zh": {"type": "string"},
    "coverage_tier": {"enum": ["core","watch"]},
    "coverage_stage": {"enum": ["onboarding","initial","full","maintenance","paused"]},
    "sector": {"type": "string"}, "currency": {"type": "string"},
    "fiscal_year_end": {"type": "string", "pattern": "^\\d{2}-\\d{2}$"},
    "identifiers": {"type": "object", "additionalProperties": false,
      "properties": {"cik": {"type": "string"}, "hk_code": {"type": "string"}, "cn_code": {"type": "string"}}},
    "peers": {"type": "array", "items": {"type": "string"}},
    "benchmarks": {"type": "array", "items": {"type": "string"}, "description": "板块 ETF / 指数，股价异动时用来分 beta 和 alpha"},
    "one_liner": {"type": "string", "maxLength": 200},
    "covered_since": {"$ref": "common#/$defs/isoDate"},
    "autonomy": {"type": "object", "additionalProperties": false,
      "properties": {"enabled": {"type": "boolean"}, "max_usd_per_day": {"type": "number", "minimum": 0}}}
  },
  "required": ["schema_version","ticker","exchange","name","coverage_tier","coverage_stage","one_liner"]
}
```

#### PK.2.3 `thesis.schema.json`（支柱、只追加的证据台账、阈值、house view、状态、正式版本）

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://dalton.local/schemas/thesis",
  "type": "object", "additionalProperties": false,
  "properties": {
    "schema_version": {"const": 1},
    "as_of": {"$ref": "common#/$defs/isoTime"},
    "one_liner": {"type": "string", "maxLength": 200},
    "aggregate_status": {"$ref": "common#/$defs/pillarStatus"},
    "aggregate_override": {"type": "string", "description": "一条核心支柱 impaired 而总体好于 watch 时，必须写一句带证据的理由（thesis-tracker 第 3 步规则）"},
    "house_view": {"$ref": "#/$defs/houseView"},
    "house_view_history": {"type": "array", "items": {"$ref": "#/$defs/houseViewChange"}, "description": "只追加"},
    "market_view": {"type": "object", "additionalProperties": false,
      "description": "市场基准与一致预期，和 house case 分开（§3.1-2）；一致预期不能自动覆盖研究判断",
      "properties": {"summary": {"type": "string"}, "as_of": {"$ref": "common#/$defs/isoTime"},
                     "figures": {"type": "array", "items": {"$ref": "common#/$defs/figure"}},
                     "evidence_refs": {"type": "array", "items": {"$ref": "common#/$defs/evidenceRef"}}}},
    "pillars": {"type": "array", "minItems": 1, "maxItems": 7, "items": {"$ref": "#/$defs/pillar"}},
    "evidence": {"type": "array", "items": {"$ref": "#/$defs/evidence"}, "description": "证据台账，只追加；更正用 supersedes 新增一条"},
    "official": {"oneOf": [{"type": "null"}, {"$ref": "#/$defs/official"}], "description": "只能由 publish_report 写"}
  },
  "required": ["schema_version","as_of","one_liner","aggregate_status","house_view","house_view_history","pillars","evidence","official"],
  "$defs": {
    "houseView": {"type": "object", "additionalProperties": false,
      "properties": {
        "stance": {"enum": ["long","short","neutral","no_view"]},
        "conviction": {"enum": ["high","medium","low","none"], "description": "没有足够预期差时写 none（§3.1-8）"},
        "horizon_months": {"type": "integer", "minimum": 1, "maximum": 60},
        "summary": {"type": "string"},
        "variant_perception": {"type": "string", "description": "我们和市场哪里不同、为什么市场错、什么会让市场改变看法"},
        "target": {"type": "object", "additionalProperties": false,
          "properties": {"value": {"type": "number"}, "currency": {"type": "string"}, "method": {"type": "string"},
                         "model_ref": {"$ref": "common#/$defs/evidenceRef"}},
          "required": ["value","currency","method","model_ref"], "description": "目标价必须指向模型单元格"},
        "position_suggestion": {"enum": ["none","starter","core","max","reduce","exit"]},
        "updated_at": {"$ref": "common#/$defs/isoTime"}
      },
      "required": ["stance","conviction","summary","updated_at"]},
    "houseViewChange": {"type": "object", "additionalProperties": false,
      "properties": {"at": {"$ref": "common#/$defs/isoTime"}, "from": {"$ref": "#/$defs/houseView"}, "to": {"$ref": "#/$defs/houseView"},
                     "reason": {"type": "string"},
                     "evidence_ids": {"type": "array", "items": {"type": "string"}}, "verdict_id": {"type": "string"}},
      "required": ["at","to","reason"]},
    "pillar": {"type": "object", "additionalProperties": false,
      "properties": {
        "id": {"type": "string", "pattern": "^P\\d+$"},
        "claim": {"type": "string"},
        "priority": {"enum": ["core","supporting"]},
        "baseline": {"type": "object", "additionalProperties": false,
          "properties": {"text": {"type": "string"}, "figure": {"$ref": "common#/$defs/figure"}, "as_of": {"$ref": "common#/$defs/isoDate"}},
          "required": ["text","as_of"]},
        "expected_path": {"type": "string"},
        "thresholds": {"type": "object", "additionalProperties": false,
          "properties": {"confirm": {"type": "array", "items": {"$ref": "common#/$defs/threshold"}},
                         "warn": {"type": "array", "items": {"$ref": "common#/$defs/threshold"}},
                         "break": {"type": "array", "items": {"$ref": "common#/$defs/threshold"}, "description": "为空时给警告，不拒写"}},
          "required": ["confirm","warn","break"]},
        "status": {"$ref": "common#/$defs/pillarStatus"},
        "status_reason": {"type": "string"},
        "status_history": {"type": "array", "description": "只追加",
          "items": {"type": "object", "additionalProperties": false,
            "properties": {"at": {"$ref": "common#/$defs/isoTime"}, "from": {"$ref": "common#/$defs/pillarStatus"},
                           "to": {"$ref": "common#/$defs/pillarStatus"}, "reason": {"type": "string"},
                           "evidence_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["at","to","reason"]}},
        "next_proof_point": {"type": "object", "additionalProperties": false,
          "properties": {"what": {"type": "string"}, "due": {"$ref": "common#/$defs/isoDate"}, "source": {"type": "string"}, "validation_id": {"type": "string"}},
          "required": ["what"]},
        "model_line": {"type": "string", "description": "对应模型里的行，如 Model!Row 18 Reality Labs opex"},
        "provenance": {"enum": ["inherited","draft","approved"]},
        "created_at": {"$ref": "common#/$defs/isoTime"},
        "retired_at": {"$ref": "common#/$defs/isoTime"}
      },
      "required": ["id","claim","priority","baseline","expected_path","thresholds","status","provenance","created_at"]},
    "evidence": {"type": "object", "additionalProperties": false,
      "properties": {
        "id": {"type": "string", "pattern": "^E-\\d{8}-\\d{2,3}$"},
        "at": {"$ref": "common#/$defs/isoTime"},
        "period": {"$ref": "common#/$defs/period"},
        "source": {"type": "object", "additionalProperties": false,
          "properties": {"type": {"enum": ["filing","company_release","broker_report","official_data","expert","news","model","internal_note"]},
                         "identity": {"type": "string", "description": "研报署名机构/作者、专家身份、文件名"},
                         "tier": {"$ref": "common#/$defs/sourceTier"},
                         "ref": {"$ref": "common#/$defs/evidenceRef"},
                         "unconfirmed_by_management": {"type": "boolean", "description": "电话会里分析师提问口径、管理层未确认的数字"}},
          "required": ["type","identity","tier","ref"]},
        "fact": {"type": "string"},
        "figures": {"type": "array", "items": {"$ref": "common#/$defs/figure"}},
        "prior_expectation": {"type": "string"},
        "market_expectation": {"type": "object", "additionalProperties": false,
          "properties": {"text": {"type": "string"}, "vintage": {"$ref": "common#/$defs/isoDate"}}},
        "interpretation": {"type": "string"},
        "pillar_ids": {"type": "array", "items": {"type": "string", "pattern": "^P\\d+$"}},
        "signal": {"$ref": "common#/$defs/signal"},
        "quality": {"enum": ["high","medium","low"]},
        "impact": {"type": "string"},
        "follow_up": {"type": "string"},
        "supersedes": {"type": "string", "pattern": "^E-\\d{8}-\\d{2,3}$"}
      },
      "required": ["id","at","source","fact","pillar_ids","signal","quality"]},
    "official": {"type": "object", "additionalProperties": false,
      "properties": {"version_no": {"type": "integer"}, "publication_id": {"type": "string", "format": "uuid"},
                     "approved_by": {"type": "string"}, "approved_at": {"$ref": "common#/$defs/isoTime"},
                     "house_view": {"$ref": "#/$defs/houseView"}, "aggregate_status": {"$ref": "common#/$defs/pillarStatus"},
                     "pillars": {"type": "array", "items": {"type": "object"}, "description": "批准时 pillars 的冻结副本"}},
      "required": ["version_no","publication_id","approved_by","approved_at","house_view","aggregate_status"]}
  }
}
```

#### PK.2.4 `debate.schema.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://dalton.local/schemas/debate",
  "type": "object", "additionalProperties": false,
  "properties": {
    "schema_version": {"const": 1}, "as_of": {"$ref": "common#/$defs/isoTime"},
    "debates": {"type": "array", "items": {"$ref": "#/$defs/debate"}}
  },
  "required": ["schema_version","as_of","debates"],
  "$defs": {
    "side": {"type": "object", "additionalProperties": false,
      "properties": {"case": {"type": "string"}, "who": {"type": "string", "description": "持这一方的典型机构/卖方，按来源链去重"},
                     "evidence_ids": {"type": "array", "items": {"type": "string"}},
                     "evidence_refs": {"type": "array", "items": {"$ref": "common#/$defs/evidenceRef"}}},
      "required": ["case"]},
    "debate": {"type": "object", "additionalProperties": false,
      "properties": {
        "id": {"type": "string", "pattern": "^D\\d+$"},
        "variable": {"type": "string", "description": "分歧变量，如 FY26 Capex 指引上限"},
        "question": {"type": "string"},
        "why_it_matters": {"type": "string", "description": "对 EPS / 倍数 / 股价的量级"},
        "rank": {"type": "integer", "minimum": 1},
        "bull": {"$ref": "#/$defs/side"}, "bear": {"$ref": "#/$defs/side"},
        "strongest_rebuttal": {"type": "object", "additionalProperties": false,
          "properties": {"against": {"enum": ["bull","bear","my_view"]}, "text": {"type": "string"}},
          "required": ["against","text"], "description": "对我方判断最强的反驳"},
        "my_view": {"type": "object", "additionalProperties": false,
          "properties": {"side": {"enum": ["bull","bear","neutral","undecided"]}, "confidence": {"enum": ["high","medium","low"]}, "text": {"type": "string"}},
          "required": ["side","confidence","text"]},
        "market_lean": {"type": "object", "additionalProperties": false,
          "properties": {"side": {"enum": ["bull","bear","split","unknown"]}, "evidence": {"type": "string"}}},
        "flip_condition": {"type": "object", "additionalProperties": false,
          "properties": {"text": {"type": "string"}, "driver_ids": {"type": "array", "items": {"type": "string"}},
                         "threshold": {"$ref": "common#/$defs/threshold"}},
          "required": ["text"]},
        "linked_pillars": {"type": "array", "items": {"type": "string"}},
        "status": {"enum": ["open","resolved","dormant"]},
        "opened_at": {"$ref": "common#/$defs/isoTime"},
        "last_reviewed_at": {"$ref": "common#/$defs/isoTime"},
        "history": {"type": "array", "description": "只追加",
          "items": {"type": "object", "additionalProperties": false,
            "properties": {"at": {"$ref": "common#/$defs/isoTime"}, "change": {"type": "string"}, "reason": {"type": "string"},
                           "evidence_ids": {"type": "array", "items": {"type": "string"}}, "verdict_id": {"type": "string"}},
            "required": ["at","change","reason"]}}
      },
      "required": ["id","variable","question","bull","bear","strongest_rebuttal","my_view","flip_condition","status","opened_at","history"]}
  }
}
```

#### PK.2.5 `drivers.schema.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://dalton.local/schemas/drivers",
  "type": "object", "additionalProperties": false,
  "properties": {
    "schema_version": {"const": 1}, "as_of": {"$ref": "common#/$defs/isoTime"},
    "historical_valuation_driver": {"type": "object", "additionalProperties": false,
      "description": "历史上股价由 EPS 驱动还是倍数驱动",
      "properties": {"summary": {"type": "string"}, "window": {"type": "string"},
                     "eps_share_pct": {"type": "number"}, "multiple_share_pct": {"type": "number"},
                     "model_ref": {"$ref": "common#/$defs/evidenceRef"}}},
    "drivers": {"type": "array", "items": {"$ref": "#/$defs/driver"}}
  },
  "required": ["schema_version","as_of","drivers"],
  "$defs": {
    "reading": {"type": "object", "additionalProperties": false,
      "properties": {"value": {"type": "number"}, "unit": {"$ref": "common#/$defs/unit"}, "period": {"$ref": "common#/$defs/period"},
                     "as_of": {"$ref": "common#/$defs/isoTime"}, "ref": {"$ref": "common#/$defs/evidenceRef"},
                     "label": {"$ref": "common#/$defs/label"}},
      "required": ["value","unit","period","as_of","ref","label"]},
    "indicator": {"type": "object", "additionalProperties": false,
      "properties": {
        "id": {"type": "string", "pattern": "^K\\d+\\.\\d+$"},
        "name": {"type": "string"}, "source": {"type": "string"},
        "frequency": {"enum": ["daily","weekly","monthly","quarterly","event"]},
        "lead_months": {"type": "number", "minimum": 0},
        "good_direction": {"enum": ["up","down","band"]},
        "latest": {"$ref": "#/$defs/reading"},
        "readings": {"type": "array", "items": {"$ref": "#/$defs/reading"}, "description": "只追加，最多保留 200 条，更早的由服务端滚到 history/"},
        "probe": {"type": "object", "additionalProperties": false, "description": "预检能否零成本取这个读数（PK.16）",
          "properties": {"kind": {"enum": ["none","price","fmp_metric","internal_query"]}, "params": {"type": "object"}}}
      },
      "required": ["id","name","source","frequency","good_direction"]},
    "driver": {"type": "object", "additionalProperties": false,
      "properties": {
        "id": {"type": "string", "pattern": "^K\\d+$"},
        "name": {"type": "string"},
        "kind": {"enum": ["volume","price","mix","margin","capex","multiple","macro","capital_return","other"]},
        "horizon": {"enum": ["short","long","both"]},
        "description": {"type": "string"},
        "linked_pillars": {"type": "array", "items": {"type": "string"}},
        "linked_debates": {"type": "array", "items": {"type": "string"}},
        "leading_indicators": {"type": "array", "items": {"$ref": "#/$defs/indicator"}},
        "sensitivity": {"type": "object", "additionalProperties": false,
          "properties": {"target_metric": {"type": "string", "description": "如 FY2026 EPS"},
                         "per_change": {"type": "string", "description": "如 每 +1pp 广告增速"},
                         "impact_value": {"type": "number"}, "impact_unit": {"$ref": "common#/$defs/unit"},
                         "model_ref": {"$ref": "common#/$defs/evidenceRef"}},
          "required": ["target_metric","per_change","impact_value","impact_unit","model_ref"]},
        "latest_assessment": {"type": "string"},
        "updated_at": {"$ref": "common#/$defs/isoTime"}
      },
      "required": ["id","name","kind","description","leading_indicators","updated_at"]}
  }
}
```

#### PK.2.6 `management.schema.json`（指引风格 + 指引对实际的历史）

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://dalton.local/schemas/management",
  "type": "object", "additionalProperties": false,
  "properties": {
    "schema_version": {"const": 1}, "as_of": {"$ref": "common#/$defs/isoTime"},
    "team": {"type": "array", "items": {"type": "object", "additionalProperties": false,
      "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "since": {"type": "string"}, "notes": {"type": "string"}},
      "required": ["name","role"]}},
    "guidance_style": {"type": "object", "additionalProperties": false,
      "properties": {"bias": {"enum": ["conservative","neutral","aggressive","mixed","insufficient_data"]},
                     "summary": {"type": "string"},
                     "sample_n": {"type": "integer", "minimum": 0},
                     "beat_rate": {"type": "number", "minimum": 0, "maximum": 1, "description": "由服务端按 guidance_history 计算，分析师不手填"},
                     "median_surprise_pct": {"type": "number", "description": "同上，服务端计算"},
                     "patterns": {"type": "array", "items": {"type": "string"}, "description": "如 Q4 指引总是给区间下沿"}},
      "required": ["bias","summary"]},
    "guidance_history": {"type": "array", "description": "只追加；actual 和 outcome 允许从 null 补填一次",
      "items": {"type": "object", "additionalProperties": false,
        "properties": {
          "id": {"type": "string", "pattern": "^G-\\d{3,4}$"},
          "metric": {"type": "string"}, "period": {"$ref": "common#/$defs/period"},
          "issued_at": {"$ref": "common#/$defs/isoDate"},
          "guide": {"type": "object", "additionalProperties": false,
            "properties": {"low": {"type": "number"}, "high": {"type": "number"}, "point": {"type": "number"},
                           "unit": {"$ref": "common#/$defs/unit"}, "text": {"type": "string"}},
            "required": ["unit"]},
          "guide_ref": {"$ref": "common#/$defs/evidenceRef"},
          "actual": {"oneOf": [{"type": "null"}, {"type": "object", "additionalProperties": false,
            "properties": {"value": {"type": "number"}, "unit": {"$ref": "common#/$defs/unit"}, "reported_at": {"$ref": "common#/$defs/isoDate"},
                           "ref": {"$ref": "common#/$defs/evidenceRef"}},
            "required": ["value","unit","reported_at","ref"]}]},
          "outcome": {"enum": ["above_high","in_range_upper","in_range_lower","below_low","met_point","pending","withdrawn"]},
          "delta_vs_mid_pct": {"type": "number", "description": "服务端计算"}
        },
        "required": ["id","metric","period","issued_at","guide","guide_ref","actual","outcome"]}},
    "capital_allocation": {"type": "object", "additionalProperties": false,
      "properties": {"summary": {"type": "string"}, "buyback": {"type": "string"}, "dividend": {"type": "string"},
                     "m_and_a": {"type": "string"}, "capex": {"type": "string"},
                     "shareholder_return_score": {"enum": ["R1","R2","R3","unrated"]}}},
    "credibility_notes": {"type": "array", "description": "只追加",
      "items": {"type": "object", "additionalProperties": false,
        "properties": {"at": {"$ref": "common#/$defs/isoTime"}, "note": {"type": "string"},
                       "evidence_refs": {"type": "array", "items": {"$ref": "common#/$defs/evidenceRef"}}},
        "required": ["at","note"]}}
  },
  "required": ["schema_version","as_of","guidance_style","guidance_history"]
}
```

#### PK.2.7 `lessons.schema.json`（适用条件、反例、可撤回，不可删除）

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://dalton.local/schemas/lessons",
  "type": "object", "additionalProperties": false,
  "properties": {
    "schema_version": {"const": 1},
    "lessons": {"type": "array", "items": {"type": "object", "additionalProperties": false,
      "properties": {
        "id": {"type": "string", "pattern": "^L-\\d{3}$"},
        "statement": {"type": "string"},
        "applies_when": {"type": "string", "description": "适用条件，必须具体到可判断"},
        "does_not_apply_when": {"type": "string"},
        "counterexamples": {"type": "array", "items": {"type": "object", "additionalProperties": false,
          "properties": {"text": {"type": "string"}, "ref": {"$ref": "common#/$defs/evidenceRef"}, "at": {"$ref": "common#/$defs/isoTime"}},
          "required": ["text","at"]}},
        "origin": {"type": "object", "additionalProperties": false,
          "properties": {"event": {"type": "string"}, "at": {"$ref": "common#/$defs/isoDate"}, "verdict_id": {"type": "string"},
                         "ref": {"$ref": "common#/$defs/evidenceRef"}},
          "required": ["event","at"]},
        "scope": {"enum": ["company","sector","fund"], "description": "sector/fund 级教训由研究总监提升到用户层记忆（PK.6）"},
        "confidence": {"enum": ["high","medium","low"]},
        "status": {"enum": ["active","retracted"]},
        "retracted": {"type": "object", "additionalProperties": false,
          "properties": {"at": {"$ref": "common#/$defs/isoTime"}, "reason": {"type": "string"}}, "required": ["at","reason"]},
        "created_at": {"$ref": "common#/$defs/isoTime"}
      },
      "required": ["id","statement","applies_when","origin","scope","confidence","status","created_at"]}}
  },
  "required": ["schema_version","lessons"]
}
```

#### PK.2.8 `agenda.schema.json`

议程是自主运行的核心输入，预检（PK.16）和研究总监（PK.19）都只读它的数据库版本。

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://dalton.local/schemas/agenda",
  "type": "object", "additionalProperties": false,
  "properties": {
    "schema_version": {"const": 1}, "as_of": {"$ref": "common#/$defs/isoTime"},
    "last_session": {"type": "object", "additionalProperties": false,
      "properties": {"at": {"$ref": "common#/$defs/isoTime"}, "thread_id": {"type": "string"}, "trigger": {"type": "string"},
                     "summary": {"type": "string"}, "verdict_id": {"type": "string"}},
      "required": ["at","summary"]},
    "open_questions": {"type": "array", "items": {"$ref": "#/$defs/question"}},
    "next_validations": {"type": "array", "items": {"$ref": "#/$defs/validation"}},
    "watch": {"type": "object", "additionalProperties": false,
      "properties": {
        "events": {"type": "array", "items": {"type": "object", "additionalProperties": false,
          "properties": {"id": {"type": "string", "pattern": "^W\\d+$"},
                         "type": {"enum": ["earnings","filing","guidance_update","investor_day","regulatory","product","macro","other"]},
                         "date": {"$ref": "common#/$defs/isoDate"},
                         "date_status": {"enum": ["confirmed","estimated","unknown"], "description": "只有公司官方来源（IR、新闻稿、8-K）才算 confirmed；FMP 等日历给的是 estimated，显示为\"待确认\""},
                         "source": {"type": "string"}, "plan": {"type": "string", "description": "如 T-3 preview / T0 快讯 / T+1 深度点评"}},
          "required": ["id","type","date_status"]}},
        "indicators": {"type": "array", "items": {"type": "object", "additionalProperties": false,
          "properties": {"indicator_id": {"type": "string"}, "rule": {"$ref": "common#/$defs/threshold"}},
          "required": ["indicator_id","rule"]}},
        "price": {"type": "object", "additionalProperties": false,
          "properties": {"abs_move_pct_1d": {"type": "number", "minimum": 0}, "excess_vs_benchmark_pct": {"type": "number", "minimum": 0},
                         "benchmark": {"type": "string"}}},
        "filings": {"type": "object", "additionalProperties": false,
          "properties": {"sources": {"type": "array", "items": {"enum": ["sec","hkex","cninfo"]}},
                         "forms": {"type": "array", "items": {"type": "string"}, "description": "如 8-K 10-Q 10-K 6-K DEF 14A"}}},
        "internal": {"type": "object", "additionalProperties": false, "description": "内部研究库新材料（经 researchdata 桥，P0D-P1）",
          "properties": {"queries": {"type": "array", "items": {"type": "string"}}}}
      }},
    "wake_conditions": {"type": "array", "items": {"type": "object", "additionalProperties": false,
      "properties": {"id": {"type": "string", "pattern": "^C\\d+$"},
                     "kind": {"enum": ["filing","earnings_window","price_move","consensus_change","indicator","due_validation","pm_question","internal_material","date"]},
                     "params": {"type": "object"},
                     "action": {"type": "string", "description": "被唤醒后做什么，例如 跑 stock-move 并给出五词结论"},
                     "enabled": {"type": "boolean"}},
      "required": ["id","kind","action","enabled"]}},
    "last_verdict": {"type": "object", "additionalProperties": false,
      "properties": {"verdict_id": {"type": "string"}, "word": {"$ref": "common#/$defs/verdictWord"}, "at": {"$ref": "common#/$defs/isoTime"}},
      "required": ["verdict_id","word","at"]},
    "next_wake": {"type": "object", "additionalProperties": false,
      "properties": {"at": {"$ref": "common#/$defs/isoTime"}, "reason": {"type": "string"}, "automation_file": {"type": "string"}},
      "required": ["at","reason"]},
    "pm_handoffs": {"type": "array", "description": "交给 PM 的事项；状态为 waiting 时后台不再为它花钱（PK.20）",
      "items": {"type": "object", "additionalProperties": false,
        "properties": {"id": {"type": "string", "pattern": "^H\\d+$"}, "what": {"type": "string"},
                       "kind": {"enum": ["decision","publish_approval","question","data_access"]},
                       "status": {"enum": ["waiting","answered","withdrawn"]},
                       "since": {"$ref": "common#/$defs/isoTime"}, "blocks": {"type": "array", "items": {"type": "string"}, "description": "被阻塞的 Q-/V- id"}},
        "required": ["id","what","kind","status","since"]}}
  },
  "required": ["schema_version","as_of","open_questions","next_validations","watch","wake_conditions"],
  "$defs": {
    "linked": {"type": "object", "additionalProperties": false,
      "properties": {"pillars": {"type": "array", "items": {"type": "string"}}, "debates": {"type": "array", "items": {"type": "string"}},
                     "drivers": {"type": "array", "items": {"type": "string"}}}},
    "attempt": {"type": "object", "additionalProperties": false,
      "properties": {"at": {"$ref": "common#/$defs/isoTime"}, "method": {"type": "string"},
                     "evidence_fp": {"type": "string", "description": "服务端计算：本次所用来源 identifier 的排序哈希"},
                     "progress": {"enum": ["none","some","resolved"]}, "note": {"type": "string"}, "thread_id": {"type": "string"}},
      "required": ["at","method","progress"]},
    "question": {"type": "object", "additionalProperties": false,
      "properties": {
        "id": {"type": "string", "pattern": "^Q-\\d{3}$"},
        "question": {"type": "string"}, "why": {"type": "string"},
        "value": {"enum": ["high","medium","low"]}, "urgency": {"enum": ["now","this_week","this_quarter","someday"]},
        "origin": {"enum": ["analyst","pm","director","event"]},
        "owner": {"enum": ["analyst","pm"]},
        "status": {"enum": ["open","waiting_pm","resolved","dropped"]},
        "linked": {"$ref": "#/$defs/linked"},
        "attempts": {"type": "array", "items": {"$ref": "#/$defs/attempt"}, "description": "只追加"},
        "stalled_since": {"$ref": "common#/$defs/isoTime", "description": "服务端维护（PK.18）"},
        "next_attempt_after": {"$ref": "common#/$defs/isoTime", "description": "服务端维护（PK.18）"},
        "resolution": {"type": "string"},
        "created_at": {"$ref": "common#/$defs/isoTime"}
      },
      "required": ["id","question","why","value","urgency","origin","owner","status","attempts","created_at"]},
    "validation": {"type": "object", "additionalProperties": false,
      "properties": {
        "id": {"type": "string", "pattern": "^V-\\d{3}$"},
        "what": {"type": "string"}, "due": {"$ref": "common#/$defs/isoDate"},
        "source": {"type": "string"}, "expected": {"type": "string", "description": "我们预期看到什么；和阈值对得上"},
        "linked": {"$ref": "#/$defs/linked"},
        "status": {"enum": ["pending","done","cancelled"]},
        "result_verdict_id": {"type": "string"}
      },
      "required": ["id","what","due","expected","status"]}
  }
}
```

#### PK.2.9 `models.schema.json`（模型版本登记）

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://dalton.local/schemas/models",
  "type": "object", "additionalProperties": false,
  "properties": {
    "schema_version": {"const": 1},
    "current": {"type": "string", "description": "当前版本 id，如 v12"},
    "versions": {"type": "array", "description": "只追加", "items": {"type": "object", "additionalProperties": false,
      "properties": {"id": {"type": "string", "pattern": "^v\\d+$"}, "path": {"type": "string", "pattern": "^model/.+\\.xlsx$"},
                     "sha256": {"type": "string", "description": "服务端读文件计算"}, "created_at": {"$ref": "common#/$defs/isoTime"},
                     "reason": {"type": "string"}, "recalculated": {"type": "boolean", "description": "是否已跑 xlsx skill 的 recalc.py"},
                     "key_outputs": {"type": "array", "items": {"type": "object", "additionalProperties": false,
                       "properties": {"name": {"type": "string"}, "cell": {"type": "string"}, "value": {"type": "number"}, "unit": {"$ref": "common#/$defs/unit"}, "period": {"$ref": "common#/$defs/period"}},
                       "required": ["name","cell"]}}},
      "required": ["id","path","created_at","reason"]}}
  },
  "required": ["schema_version","versions"]
}
```

#### PK.2.10 `verdict.schema.json`（事件五词结论，单条）

结论条目存在 `dalton_verdicts` 表里（PK.3），不进工作区 JSON，避免文件无限增长。`verdicts.md` 渲染最近 60 条。

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://dalton.local/schemas/verdict",
  "type": "object", "additionalProperties": false,
  "properties": {
    "id": {"type": "string", "pattern": "^VD-\\d{8}-\\d{2,3}$"},
    "at": {"$ref": "common#/$defs/isoTime"},
    "trigger": {"type": "object", "additionalProperties": false,
      "properties": {"kind": {"enum": ["filing","earnings","price_move","consensus_change","indicator","due_validation","pm_question","internal_material","agenda","director","manual"]},
                     "ref": {"type": "string", "description": "预检信号 id、accession、V-/Q- id 等"},
                     "automation_execution_id": {"type": "string"}},
      "required": ["kind"]},
    "event": {"type": "string", "description": "一句话事件"},
    "verdict": {"$ref": "common#/$defs/verdictWord"},
    "affected": {"type": "object", "additionalProperties": false,
      "properties": {"pillars": {"type": "array", "items": {"type": "string"}}, "debates": {"type": "array", "items": {"type": "string"}}, "drivers": {"type": "array", "items": {"type": "string"}}}},
    "rationale": {"type": "string"},
    "evidence_ids": {"type": "array", "items": {"type": "string"}},
    "house_view_changed": {"type": "boolean"},
    "next_validation": {"type": "object", "additionalProperties": false,
      "properties": {"what": {"type": "string"}, "due": {"$ref": "common#/$defs/isoDate"}, "validation_id": {"type": "string"}},
      "required": ["what","due"]},
    "progress": {"enum": ["none","some","resolved"], "description": "对议程问题的进展，用于退避（PK.18）"},
    "question_ids": {"type": "array", "items": {"type": "string"}}
  },
  "required": ["at","trigger","event","verdict","rationale","next_validation","progress"]
}
```

规则（都是**警告**，不拒写，见 PK.4）：`verdict ≠ NO_CHANGE` 时 `evidence_ids` 建议至少一条；`THESIS_BROKEN` 时通常应同时把某个支柱的状态改成 `broken` 或 `changed`。

**验证方式**：
- 写一个一次性脚本 `scripts/dalton/validate_samples.py`（新建，不进 CI），用 `jsonschema` 校验 `docs/dalton/dev-guide/samples/` 下 META 的样例文档（实施时随手写，样例取自旧 Dalton 导出）。
- 所有 schema 都要过 `Draft202012Validator.check_schema`。

**验收标准**：
- 10 个 schema 文件都在；
- `check_schema` 全部通过；
- META 样例全部通过；
- 故意删掉 `thesis.pillars[0].claim` 时，报错信息能指出具体路径。

**风险与注意**：
- 本阶段只做美股；`period` 正则和 `exchange` 枚举保留港股、A 股的写法，便于 P6 扩展（年结日期写在 `company.fiscal_year_end`，期间字符串不承载年结信息）。
- schema 版本号：结构有破坏性变化时 `schema_version` 加 1，在 `ops.py` 里写迁移函数。不要静默兼容。

**预估工时**：1.25 人日。

---

### PK.3 版本与结论表（迁移 d003）

**目标**：在 Postgres 里保存每个知识文档的每一个版本（旧值、新值、原因、证据、来源 run、写入途径），以及只追加的事件结论。

**前置依赖**：PK.2；P0.8 建立的 Dalton 迁移链（`d001`、`d002`）。

**改动/新增文件**：
- 新建 `src/dalton/migrations/versions/d003_knowledge.py`
- 新建 `src/dalton/database/knowledge.py`、`src/dalton/database/verdicts.py`

**具体做法**：

```python
"""Dalton d003: company-knowledge versions and event verdicts."""
from alembic import op
revision = "d003"; down_revision = "d002"; branch_labels = None; depends_on = None

def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # 不建指向上游表的外键（P0.8 规则）：只存 id；工作区删除后记录留作档案（D10）
    op.execute("""
    CREATE TABLE IF NOT EXISTS dalton_knowledge_versions (
        knowledge_version_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id        VARCHAR(255) NOT NULL,
        workspace_id   UUID NOT NULL,
        doc_key        TEXT NOT NULL CHECK (doc_key IN ('company','thesis','debate','drivers','management','lessons','agenda','models')),
        version_no     INTEGER NOT NULL CHECK (version_no >= 1),
        source         TEXT NOT NULL CHECK (source IN ('tool','direct_edit','import','publish','server')),
        content        JSONB NOT NULL,
        content_sha256 VARCHAR(64) NOT NULL,
        ops            JSONB,
        reason         TEXT NOT NULL,
        evidence_refs  JSONB NOT NULL DEFAULT '[]',
        warnings       JSONB NOT NULL DEFAULT '[]',   -- 非硬性规则的提示；direct_edit 不合 schema 时也记在这里
        schema_valid   BOOLEAN NOT NULL DEFAULT TRUE, -- direct_edit 可以是 FALSE；渲染时退回上一份 schema_valid 的版本
        edited_path    TEXT,                          -- direct_edit 时被改的文件（data/<doc>.json 或渲染出的 <doc>.md）
        conversation_thread_id UUID,
        run_id         UUID,
        tool_call_id   TEXT,
        files_written_at TIMESTAMPTZ,
        created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (workspace_id, doc_key, version_no)
    )""")
    op.execute("""CREATE INDEX IF NOT EXISTS idx_dalton_kv_latest
                  ON dalton_knowledge_versions (workspace_id, doc_key, version_no DESC)""")
    op.execute("""
    CREATE TABLE IF NOT EXISTS dalton_verdicts (
        verdict_id     TEXT PRIMARY KEY,                -- VD-YYYYMMDD-NN，按 workspace 当日序号由服务端分配
        user_id        VARCHAR(255) NOT NULL,
        workspace_id   UUID NOT NULL,
        verdict_at     TIMESTAMPTZ NOT NULL,
        word           TEXT NOT NULL CHECK (word IN ('NO_CHANGE','THESIS_STRENGTHENED','THESIS_WEAKENED','THESIS_BROKEN','NEW_THESIS')),
        trigger_kind   TEXT NOT NULL,
        trigger_ref    TEXT,
        progress       TEXT NOT NULL CHECK (progress IN ('none','some','resolved')),
        next_due       DATE,
        payload        JSONB NOT NULL,
        conversation_thread_id UUID, run_id UUID, automation_execution_id UUID,
        origin_type    TEXT,                            -- 'automation' | 'agent' | NULL(用户发起)
        created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS idx_dalton_verdicts_ws_time ON dalton_verdicts (workspace_id, verdict_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_dalton_verdicts_user_time ON dalton_verdicts (user_id, verdict_at DESC)")
    # 只追加：结论表拒绝 UPDATE/DELETE；版本表只允许回填 files_written_at
    op.execute("""
    CREATE OR REPLACE FUNCTION dalton_forbid_mutation() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'dalton: % on % is not allowed (append-only)', TG_OP, TG_TABLE_NAME; END $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER trg_dalton_verdicts_immutable BEFORE UPDATE OR DELETE ON dalton_verdicts
                  FOR EACH ROW EXECUTE FUNCTION dalton_forbid_mutation()""")
    op.execute("""
    CREATE OR REPLACE FUNCTION dalton_kv_guard() RETURNS trigger AS $$
    BEGIN
      IF TG_OP = 'DELETE' OR NEW.content IS DISTINCT FROM OLD.content OR NEW.reason IS DISTINCT FROM OLD.reason
         OR NEW.version_no IS DISTINCT FROM OLD.version_no THEN
        RAISE EXCEPTION 'dalton: knowledge versions are immutable';
      END IF; RETURN NEW;
    END $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER trg_dalton_kv_guard BEFORE UPDATE OR DELETE ON dalton_knowledge_versions
                  FOR EACH ROW EXECUTE FUNCTION dalton_kv_guard()""")

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dalton_verdicts")
    op.execute("DROP TABLE IF EXISTS dalton_knowledge_versions")
    op.execute("DROP FUNCTION IF EXISTS dalton_kv_guard()")
    op.execute("DROP FUNCTION IF EXISTS dalton_forbid_mutation()")
```

没有"被拒绝的版本"这一类行：工具路径上的硬错误不落库（直接退回问题列表），直接修改一律落库为 `direct_edit`（不合 schema 时 `schema_valid=false` 加 `warnings`）。

`src/dalton/database/knowledge.py`（原生 SQL，走 app-data 连接池 `src.server.database.pool.get_db_connection`）：

```python
async def latest_version(workspace_id: str, doc_key: str) -> dict | None
async def latest_valid_version(workspace_id: str, doc_key: str) -> dict | None   # 渲染和预检读这个
async def insert_version(*, user_id, workspace_id, doc_key, base_version_no: int | None, content: dict,
                         content_sha256: str, ops: list | None, reason: str, evidence_refs: list,
                         source: str, warnings: list, schema_valid: bool, edited_path: str | None,
                         thread_id: str | None, run_id: str | None, tool_call_id: str | None) -> dict
    # 事务内：SELECT pg_advisory_xact_lock(hashtext('dalton:kv:' || workspace_id || ':' || doc_key));
    # 读最新 version_no；工具路径若 base_version_no 不等于它 → raise VersionConflict(latest)
    # （direct_edit / import / server 不做 CAS，直接接在最新版本之后）
    # INSERT version_no = latest + 1 RETURNING *
async def mark_files_written(knowledge_version_id: str) -> None
async def list_versions(workspace_id: str, doc_key: str, limit: int = 50, before: int | None = None) -> list[dict]
async def get_version(workspace_id: str, doc_key: str, version_no: int) -> dict | None
async def latest_by_user(user_id: str, doc_key: str) -> list[dict]   # 研究总监、预检批量读议程
```

`verdicts.py`：`next_verdict_id(workspace_id, day)`（同样在 advisory lock 内取当日最大序号加 1）、`insert_verdict(...)`、`list_verdicts(workspace_id, limit, since)`、`list_user_verdicts(user_id, since, until)`。

**多 worker 说明**：
- 版本号分配和 CAS 都在单个事务里完成，用 `pg_advisory_xact_lock`，不用进程内锁。
- 两个线程并发用工具写同一文档时，后到的返回 `version_conflict`，工具把最新版本号和差异摘要退回给模型，让它重读后再提交。

**验证方式**：
- `docker compose exec backend uv run alembic -c src/dalton/alembic.ini upgrade head`，然后 `downgrade -1`，再 `upgrade head`；
- 用 psql 手工 `UPDATE dalton_verdicts SET word='NEW_THESIS'`，应该报 `append-only`；
- 两个 psql 会话同时 `BEGIN; SELECT pg_advisory_xact_lock(...)`，第二个会阻塞。

**验收标准**：
- 两张表、两个触发器都在，`alembic_version_dalton` 显示 `d003`（或更新）；
- 升级降级来回不报错；
- 不可变性真实生效（截图或贴 SQL 输出）。

**风险与注意**：
- **存储增长**：thesis 的证据台账越长，每个版本存的全文越大。估算：300 条证据 × 0.8KB ≈ 240KB 一版，200 版约 48MB 一家公司；JSONB 有 TOAST 压缩，大约 1/3。覆盖 30 家公司时可以接受。单份文档超过 `max_doc_bytes`（1MiB）时只给警告，提示把旧证据滚到 `data/evidence-archive-<year>.json`（滚动操作作为二期再做）。
- 不要把这些表挂到 LangGraph 的 checkpointer 连接池上（两个连接池分开用，见 `src/server/AGENTS.md`）。
- 没有外键：工作区被删除后，这些行变成档案。研究总监和 API 都按"工作区仍存在"过滤（join 上游 `workspaces`），不会把已删除的公司排进计划。

**预估工时**：0.75 人日。

---

### PK.4 写入路径：`knowledge_update` / `knowledge_append_verdict` 工具

**目标**：给分析师一条"先校验、再落库、再写文件"的**正常**写入路径。每次修改都留下旧值、新值、原因、证据和来源 run，同时生成人读的 `.md` 和 `history/` 日期副本。这不是唯一路径：直接修改也允许，事后记录（PK.5）。

**前置依赖**：PK.1–PK.3。

**改动/新增文件**：
- 新建 `src/dalton/knowledge/ops.py`：按 id 寻址的操作语言
- 新建 `src/dalton/knowledge/validate.py`：schema 校验、3 条硬性不变量、其余规则的警告
- 新建 `src/dalton/knowledge/render.py`，以及 `src/dalton/knowledge/templates/{thesis,debate,drivers,management,lessons,agenda,verdicts,published_index,agent_block}.md.j2`
- 新建 `src/dalton/knowledge/service.py`
- 新建 `src/dalton/tools/__init__.py`、`src/dalton/tools/knowledge_tools.py`
- 钩子 `DALTON-HOOK(dalton-tools)`：`src/ptc_agent/agent/agent.py` 在 L997-1002 附近加一行挂载（§5 #1）

**具体做法**：

**(1) 工具签名**（原生 `@tool`，只给 `analyst` 角色的主智能体）：

```python
@tool("knowledge_update")
async def knowledge_update(
    doc: Literal["company","thesis","debate","drivers","management","lessons","agenda","models"],
    ops: list[dict],
    reason: str,
    base_version: int | None,
    evidence_refs: list[dict] | None = None,
    *, config: RunnableConfig, tool_call_id: Annotated[str, InjectedToolCallId],
) -> str:
    """Change one company-knowledge document through validated operations; prefer this over editing data/*.json by hand.

    Read data/<doc>.json first and pass its version as base_version (null only to create the document).
    Ops address list items by id, e.g. {"op":"append","path":"/evidence","value":{...}},
    {"op":"set","path":"/pillars/P2/status","value":"watch"}. reason says why, in one or two sentences.
    Returns: the new version and the rendered file, or the list of problems to fix.
    """

@tool("knowledge_append_verdict")
async def knowledge_append_verdict(
    verdict: dict,                     # 符合 verdict.schema.json；id 可省略，由服务端分配
    agenda_ops: list[dict] | None = None,   # 同一次提交里更新议程（关闭验证项、追加 attempt、改 next_wake）
    *, config: RunnableConfig, tool_call_id: Annotated[str, InjectedToolCallId],
) -> str:
    """Record an event verdict (NO_CHANGE/THESIS_STRENGTHENED/THESIS_WEAKENED/THESIS_BROKEN/NEW_THESIS) and its next validation; every research session that looked at new evidence ends with one.
    Returns: the verdict id and the agenda version it updated.
    """
```

docstring 按 `src/tools/AGENTS.md` 的写法：每句都要影响模型的决策；直连工具的 `Returns:` 只写一行。

**(2) 操作语言（`ops.py`）**：

| op | 含义 | 例子 |
|---|---|---|
| `set` | 设字段；路径中的 `P2`、`D1`、`K3`、`K3.1`、`Q-004` 这类段按数组元素的 `id` 匹配 | `{"op":"set","path":"/pillars/P2/status","value":"watch"}` |
| `append` | 向数组末尾追加；`id` 省略时服务端分配 | `{"op":"append","path":"/evidence","value":{...}}` |
| `insert` | 新增带 id 的对象（支柱、debate、驱动、问题） | `{"op":"insert","path":"/debates","value":{...}}` |
| `retire` | 支柱退役（status=retired，写 retired_at），不删除 | `{"op":"retire","path":"/pillars/P4","value":{"reason":"..."}}` |
| `retract` | 撤回 lesson（status=retracted，写 retracted），不删除 | `{"op":"retract","path":"/lessons/L-007","value":{"reason":"..."}}` |
| `remove` | 只允许删除非历史类数组中的元素，例如 `watch.events`、`wake_conditions` | `{"op":"remove","path":"/watch/events/W3"}` |
| `create` | 仅在 `base_version=null` 时用：整份文档 | `{"op":"create","value":{...}}` |

实现大约 150 行：按 `/` 分段解析路径，每段先当键用，遇到数组就按 `id` 查找元素；找不到时报 `path_not_found`，并附上可选的 id 列表。

**(3) 服务端自动维护的字段**（模型写了也会被覆盖）：
- 所有 `id`；
- `as_of`；
- `pillars[].status_history`：`set .../status` 时自动追加一条，`reason` 取自本次提交的 `reason`；
- `house_view_history`：`set /house_view` 时自动追加；
- `management.guidance_style.beat_rate`、`median_surprise_pct`、`guidance_history[].delta_vs_mid_pct`；
- `agenda.questions[].attempts[].evidence_fp`、`stalled_since`、`next_attempt_after`（见 PK.18）；
- `models.versions[].sha256`：服务端在沙箱里读该文件计算。

**(4) 硬性不变量与警告（`validate.py`）**。schema 通过后，再对比旧文档和新文档。**只有下面三条是硬错误**（工具拒写、退回问题列表）：

| 编号 | 硬性不变量 | 错误码 |
|---|---|---|
| I1 | 证据台账只追加：`thesis.evidence` 的旧元素必须原样保留在原位置；更正用 `supersedes` 新增一条 | `append_only_violation` |
| I2 | `thesis.official`（PM 批准的正式 house view 和支柱冻结副本）只能由 `source='publish'` 写入，工具路径一律拒绝 | `official_is_publish_only` |
| I3 | `house_view.target` 必须带 `model_ref.kind='model_cell'`，且该 path 登记在 `models.versions` 里 | `target_needs_model_cell` |

其余规则一律是**警告**：照常保存，警告写进版本行的 `warnings`，并出现在工具返回的 note 里。例如：
- 其他历史类数组（`house_view_history`、`status_history`、`debates[].history`、`guidance_history`、`credibility_notes`、`questions[].attempts`、`models.versions`）被改写或删减；
- 支柱、lesson 被直接删除（建议用 `retire` / `retract`）；
- 核心支柱状态与 `aggregate_status` 不协调（沿用 thesis-tracker 第 3 步的规则，作为提示）；
- `evidence[].source.tier` 和 `source.type` 不对应；
- 结论为 `THESIS_BROKEN` 但没有支柱状态变为 broken/changed；
- 支柱没有 break 阈值；`reason` 过短。

一次最多返回 10 个问题，格式仿照自动任务文件的报错（`file.py` 的 `_MAX_PROBLEMS`）：`- /evidence/3: append-only (E-20261019-02 was changed; add a new entry with supersedes instead)`。

**(5) 提交顺序（`service.py: apply_update`）**：

```
1. 从数据库取 latest_version(workspace, doc)。若不存在且工作区里已有 data/<doc>.json（例如 owner 手工放进来的）→ 先按 PK.5 记成 direct_edit 版本，再继续
2. 检查 base_version == latest.version_no，否则返回 version_conflict（附最新版本号和 ops 摘要）。latest 是一份 schema_valid=false 的 direct_edit 时，ops 应用在它之上；提交后的结果必须通过 schema，这正是把直接修改"合入"的途径
3. 应用 ops → 补服务端维护字段 → schema 校验 → 三条硬性不变量 → 有硬错误就返回问题列表（不写任何东西）；警告只收集，不阻止
4. insert_version(...)：数据库是真值；拿到 version_no
5. 写文件（经同一个 filesystem_backend；任何一步失败都不回滚数据库，`files_written_at` 为空，下一轮开始时补写，见 PK.5）：
   a. data/<doc>.json（canonical：sort_keys=False、ensure_ascii=False、indent=2，末尾换行）
   b. history/<doc>/<UTC>-v<NNNN>.json
   c. 渲染出的 <doc>.md
   d. agent.md 托管块（PK.6）
   e. doc=company 时同步工作区描述（workspace 表的 description，走现有 workspace 服务函数【待验证 函数名】）
6. mark_files_written(version_id)
7. 返回：  Saved thesis v13 (was v12): set /pillars/P2/status watch→impaired; append /evidence E-20261019-02.
           Rendered thesis.md. 1 warning: aggregate_status still "intact" while core P2 is impaired — set it or add aggregate_override.
```

`knowledge_append_verdict` 的顺序相同：
1. 在 advisory lock 内分配 `VD-` id；
2. `insert_verdict`；
3. 有 `agenda_ops` 时，对 agenda 走一次 `apply_update`，`reason` 写成 `verdict VD-...`，并自动执行：`last_verdict` 设为本条；对应的 `V-` 标 `done` 并写 `result_verdict_id`；对应 `Q-` 追加一条 attempt（`progress` 取自结论）；
4. 重渲染 `verdicts.md`（数据库里最近 60 条）。

**(6) 运行身份**：
- `user_id`、`workspace_id`、`thread_id` 取自 `config["configurable"]`。【待验证】键名，参照 `chief_of_staff.py` 的读法。
- `run_id` 取自 configurable 或运行上下文。【待验证】
- `origin_type` 从 `conversation_threads.metadata->'origin'->>'type'` 读（`threads_read.py` 已有类似 SQL）。
- `automation_execution_id`：线程来源为 `automation` 时，用"该线程最近一条 running 的 execution"补齐（`automation_executions.conversation_thread_id`）。

**(7) 挂载**（`src/dalton/tools/__init__.py`）：

```python
def build_dalton_tools(role: AgentRole, filesystem_backend, *, enabled: bool) -> list[BaseTool]:
    if not enabled: return []
    if role == "analyst":
        return [*knowledge_tools(filesystem_backend), *publish_tools(filesystem_backend)]
    if role == "chief_of_staff":
        return [*director_tools()]           # PK.19
    return []
```

`agent.py` 在 L1000 之后加（钩子 `# DALTON-HOOK(dalton-tools)`，2 行）：`from src.dalton.tools import build_dalton_tools` 和 `tools = [*tools, *build_dalton_tools(role, filesystem_backend, enabled=dalton_enabled())]`。只挂给主智能体，子智能体不写知识层（子智能体的工具快照在此之前已经拍好，见 L912-1000 的注释【待验证】）。

**验证方式**（真实调用优先）：
1. `make up`。在测试工作区 `ZZTEST` 里对分析师说："初始化知识层：建 2 条支柱，每条都要有 confirm/warn/break 阈值"。
2. 在 ChatView 里看到 `knowledge_update` 工具卡，参数可见；FilePanel 出现 `thesis.md`、`data/thesis.json`、`history/thesis/...-v0001.json`。
3. 让它"把 P1 的状态改成 watch，原因是……"。数据库里应有 `version_no=2`，`content->'pillars'->0->'status_history'` 多出一条。
4. 让它"删掉证据 E-…"。应返回 `append_only_violation`，文件不变，数据库没有新版本行。再让它"把 P1 状态改成 broken 但不改 aggregate"，应保存成功并带一条警告。
5. 两个浏览器标签各开一个线程，同时改 thesis。后到的应收到 `version_conflict`，重读后成功。重复 3 次（多 worker 路由是概率性的）。

**验收标准**：
- 以上 5 条全部可复现；
- `select doc_key, version_no, source, reason from dalton_knowledge_versions where workspace_id=…` 能读出完整历史；
- 任意两版的 `content` 可以用 `jsonb` 差异脚本对比（`scripts/dalton/kdiff.py <ws> thesis 3 5`，新建，输出 JSON 路径级差异）。

**风险与注意**：
- **上游同步**：对 `agent.py` 只加一行挂钩，其余全部放在新模块里。
- **token 开销**：分析师每次只提交 ops，不提交整份文档，这正是 ops 语言要解决的问题。
- 工具运行在后端进程里。写沙箱文件依赖沙箱在线；沙箱掉线时，数据库已写入，`files_written_at` 为空，由 PK.5 在下一轮补写。
- 不要在工具里调用 `create_llm()`。

**预估工时**：2.5 人日（ops 1、校验与警告 0.25、渲染模板 0.75、工具与挂载 0.5）。

---

### PK.5 直接修改：放行、事后检测、记录与提示

**目标**：owner 在 FilePanel 里、或分析师用 Write、Edit、Bash 直接改了知识文件时，**不拦截、不还原**。本轮结束后检测出来，记成一个 `direct_edit` 版本（历史不丢）；内容不合 schema 时给分析师一条警告，由它下次用 `knowledge_update` 合入。草稿和研究文件从不受影响。

**前置依赖**：PK.3、PK.4。

**改动/新增文件**：
- 新建 `src/dalton/knowledge/reconcile.py`：`detect_direct_edits(workspace_id, user_id)`（每轮结束，读备份镜像）和 `apply_pending(workspace_id, backend)`（下一轮开始，补写文件和提示）
- 新建 `src/dalton/middleware/reconcile.py`：一个轻量 `AgentMiddleware.abefore_agent`，调用 `apply_pending`
- 钩子 `DALTON-HOOK(knowledge-reconcile-mw)`：`src/ptc_agent/agent/agent.py` 主智能体专属中间件里插入上面的中间件（§5 #2）
- 钩子 `DALTON-HOOK(knowledge-reconcile)`：`src/server/handlers/chat/ptc_run.py` 中 `on_background_workflow_complete` 的 `backup_changed_projects`（L926）之后调用 `detect_direct_edits`（§5 #3）

**具体做法**：

**(1) 检测（每轮结束，读 Postgres 镜像，不碰沙箱）**：`detect_direct_edits(workspace_id, user_id)`。

挂点在 `ptc_run.py` 中 `backup_changed_projects(...)` 返回之后，用 `asyncio.create_task` 后台执行，`asyncio.wait_for(…, 30)` 限时，异常只记日志，不进错误漏斗。这是轮次结束后的后台工作，不能影响 run 的终态。

```
对每个 doc_key：
  latest = latest_version(ws, doc)
  if latest and latest.files_written_at is NULL                      → 标记"需要补写"（下一轮开始执行）
  json_mirror = workspace_files 中 data/<doc>.json 那一行（content_hash / blob_sha256【待验证 哪一列是正文 sha256】）
  if json_mirror.sha != latest.content_sha256：
      content = 读镜像正文（persistence 层现有读函数【待验证 名称】）
      解析 JSON + schema 校验 + 三条硬性不变量（以 latest 为旧值）+ 警告规则
      一律 insert_version(source='direct_edit', edited_path='data/<doc>.json',
                          reason='检测到未经 knowledge_update 的直接修改（文件面板或命令行）',
                          schema_valid=<是否通过 schema 和硬性不变量>, warnings=<问题列表>)
      JSON 解析失败时 content 存 {"_raw": <原文前 64KB>}，schema_valid=false
  md_mirror = 渲染出的 <doc>.md
  if md_mirror.sha != 渲染(latest_valid_version) 的 sha：
      insert_version(source='direct_edit', edited_path='<doc>.md', content=latest.content（不变）,
                     schema_valid=true, warnings=[{"code":"rendered_md_edited", "md_sha256":…}],
                     reason='渲染文件被直接修改；修改内容已存入 history/，请用 knowledge_update 合入')
      并标记"需要保存 md 副本"
对 published/**：mirror 中每个文件的 sha 对照 dalton_publication_files（PK.8）；不一致或缺失 → 只记日志并标记"需要提示"，不还原
```

**(2) 补写与提示（下一轮开始，沙箱一定在线）**："需要处理"的状态直接从 Postgres 推导，不新增状态表：`files_written_at IS NULL` 的版本，以及带 `rendered_md_edited` 警告、但还没写 md 副本的版本。

`apply_pending(workspace_id, backend)` 在每轮 `BaselineContextMiddleware.abefore_agent` 之后、以及 `knowledge_*` 工具首次调用前执行，限时 10 秒：
- 对 `direct_edit` 的 JSON：**不回写 JSON**（文件里就是用户改的内容）；写 `history/<doc>/<UTC>-v<NNNN>.json`；`schema_valid=true` 时重渲染 `<doc>.md`，`false` 时 md 保留上一份有效版本的渲染；
- 对被改的渲染 md：先把当前 md 原样存为 `history/<doc>/<UTC>-v<NNNN>-edited.md`，再按数据重渲染 md（文件头写明这一点，不做"还原"警告）；
- 对 `files_written_at IS NULL` 的工具版本：补写（同 PK.4 第 5 步）；
- 在 agent.md 托管块里加提示行，保留到下一次 `knowledge_update` 成功写该文档为止，例如：
  `⚠ data/thesis.json 被直接修改（v14，不合 schema：/pillars/P2/claim 缺失），请用 knowledge_update 修正`；
  `ℹ debate.md 被直接修改，原文在 history/debate/…-edited.md，请把要保留的内容用 knowledge_update 合入`；
  `⚠ published/2026-10-20-x.md 与发布记录的哈希不一致（发布物按约定不改写）`。
- 同样的提示也附在该文档下一次 `knowledge_update` 的返回 note 里。

**(3) 渲染与预检读哪一版**：渲染、托管块、预检、研究总监都读 `latest_valid_version`（最新一份 `schema_valid=true` 的版本），所以一次写坏的直接修改不会让下游出错，只会让分析师看到警告。

**(4) FilePanel 里的人工编辑**：`PUT /api/v1/workspaces/{id}/files/write`（`crud.py` L559）照常写，不拦。【待验证】能否从路由拿到"写入者是用户"（参照 `crud.py` 对 agent.md 的 `session.note_agent_md_write`）；拿得到时 reason 写"用户在文件面板修改"，拿不到时统一写"直接修改"。

**验证方式**：
1. 让分析师 `Write data/thesis.json`，把 `one_liner` 改掉。写入成功（不出现 `NOT SAVED`）；本轮结束后数据库多一行 `source='direct_edit'`、`schema_valid=true`；下一轮开始 `thesis.md` 已按新内容重渲染。
2. 用 Bash 删掉 `evidence[0]`。本轮结束后多一行 `direct_edit`、`schema_valid=false`，`warnings` 含 `append_only_violation`；下一轮开始 agent.md 托管块出现警告行，`thesis.md` 仍是上一份有效版本的渲染；分析师用 `knowledge_update` 修正后，警告行消失。
3. 在 FilePanel 里直接改 `debate.md` 的一句话。下一轮开始，`history/debate/…-edited.md` 保存了改过的原文，`debate.md` 重渲染，托管块有提示行。
4. Bash `rm published/2026-10-20-x.md`（需要先完成 PK.11）。下一轮开始托管块出现哈希不一致的警告；文件不被自动恢复。

**验收标准**：
- 四种情形全部按预期处理，任何直接修改都没有被拒绝或静默丢弃；
- 检测与补写不改变当轮 run 的终态；
- `--workers 2` 下重复 3 次，结果一致。

**风险与注意**：
- **检测读的是"备份镜像"**。如果备份在本轮被跳过（例如沙箱停止），检测会推迟到下一次备份。这是可以接受的延迟，不是丢失。
- 不要在检测里加进程内缓存。所有"待处理"状态都从表里推导（多 worker 规则）。
- 渲染 md 被改后会在下一轮被重渲染覆盖，所以文件头写明"直接修改会被记录，请用 knowledge_update 合入"；改动本身保存在 `history/` 里，不会丢。
- `history/` 只是给人看的副本，真值在数据库；人或 Bash 改了它只记日志。

**预估工时**：1 人日。

---

### PK.6 读侧：每轮怎么加载知识层

**目标**：分析师每轮开始就知道这家公司的当前判断和去哪里找细节；Chief of Staff 能跨公司读；上下文不膨胀（agent.md 和记忆索引都在 32k 字符以内，目标是 agent.md ≤ 8k）。

**前置依赖**：PK.4。

**改动/新增文件**：
- 新建 `src/dalton/knowledge/templates/agent_block.md.j2`
- 新建 `plugins/dalton/skills/dalton-knowledge/SKILL.md`
- 不改 LangAlpha 的 prompt 模板（`workspace_context.md.j2` 和 `memory.md.j2` 都不动）

**具体做法**：

**(1) agent.md 托管块**。由 `knowledge_update` 每次落库后重写，只改两个标记之间的内容，标记以外的文字（分析师自己写的目标、备忘）原样保留：

```markdown
<!-- dalton:summary:start (由 knowledge_update 维护；手改会在下次更新时被重写) -->
# META（Meta Platforms · Core，完整覆盖中）
**一句话**：广告份额与效率双升，FY26 Capex 是最大分歧；house view：long / medium（v13，2026-10-19）。
**正式版本**：thesis v9，2026-10-02 PM 批准（published/2026-10-02-meta-full-coverage.md）
**Thesis 状态**：aggregate intact｜P1 intact｜P2 watch｜P3 strengthening
**Top debates**：D1 Capex 上限（我方：bear 侧 medium）｜D2 Reels 变现斜率（bull/high）
**下一项验证**：V-014 10-29 3Q26 业绩：Family DAP 同比 ≥ 5%（T3）
**上次结论**：VD-20261017-01 无变化（股价 −3.1%，板块 beta 解释 80%）
**等待 PM**：H2 是否把 conviction 升到 high
**文件**：thesis.md · debate.md · drivers.md · management.md · lessons.md · agenda.md · verdicts.md · model/（当前 v12）· published/INDEX.md
**规则**：改知识层用 knowledge_update / knowledge_append_verdict；直接修改会被记录并提示合入；published/ 按约定不改写。
**提示**：（PK.5 的直接修改警告行，没有就不显示）
<!-- dalton:summary:end -->
```

长度控制：
- 托管块的模板保证 ≤ 2,500 字符：每个列表截前 3 项，句子按字符截断；
- 整个 agent.md 超过 `MAX_AGENT_MD_SIZE × 0.85`（约 27.8k，见 `context_file_policy.py` 的 warn 阈值）时，工具返回 note，提醒分析师压缩自由区。

**(2) 详细内容按需读取**。分析师用 `Read thesis.md` 等读取。`dalton-knowledge` skill 规定读的顺序：
1. 回答 PM 问题：先读 thesis.md 和 debate.md，再按需读 drivers.md 和 verdicts.md；
2. 事件唤醒：先读 agenda.md，再读唤醒原因提到的支柱、驱动；
3. 写报告：先读 thesis.md、debate.md、management.md、model/ 当前版本。

**(3) 记忆层分工**：
- 工作区记忆（`<ws>/.agents/memory/memory.md`）只放跨会话的工作习惯，例如"这家公司 IR 只在 8-K 附件放 deck"。**不放观点**，观点只在知识层。
- 用户层记忆（`.agents/user/memory/memory.md`）放基金级方法和 `scope=fund` 的 lessons 摘要，每条一行加指针 `（../META/lessons.md#L-007）`。由研究总监每周整理一次，保证 ≤ 20k 字符。
- 上限 `MAX_MEMORY_BLOCK_SIZE=32768`，超出部分在读取时截断（`epoch.py` L871）。

**(4) Chief of Staff 跨公司读**：
- 通过兄弟工作区路径 `../<folder>/thesis.md` 读取（`web/AGENTS.md` 的同机兄弟文件夹约定；后端文件路由同理）；
- 批量概览用 PK.19 的 `dalton_coverage_board` 工具，从 Postgres 读取，不逐个开文件。

**(5) `dalton-knowledge/SKILL.md` 的要点**（全文由实施者按 `plugins/README.md` 格式写）：文件地图；ops 示例（新增证据、改支柱状态、改 house view、补填指引实际值、撤回教训）；什么时候必须写结论；三条硬性规则（证据只追加、正式版本只经发布、目标价指向模型单元格）；看到托管块里的直接修改提示时如何合入；来源层级表（专家写身份不写姓名）；"本轮未取得 ≠ 公司未披露"。

**验证方式**：
1. 在 META 工作区新开线程，问"你对 META 的看法和主要 debate"。回答应引用托管块中的 house view 和 D1/D2，并主动去 Read debate.md（工具卡里能看到）。
2. 在 Home 问"META 和 AMZN 现在谁的 thesis 更脆弱"。应调用 `dalton_coverage_board` 或读取兄弟目录，而不是重新检索网络。

**验收标准**：
- 托管块每次版本更新后都会刷新；
- 自由区文字不丢；
- agent.md 总长 < 8k（`wc -c`）；
- 第 1 条验证的回答里没有和知识层矛盾的说法。

**风险与注意**：
- agent.md 按 epoch 冻结；同一轮里工具改了 agent.md，模型会收到 `agent_md_changed` 更新行（baseline 机制）。不需要另外通知。
- 托管块里的数字（例如阈值 5%）也受发布核验约束吗？不受，它不是发布物。但它们都取自已校验的 JSON。

**预估工时**：1 人日。

---

### PK.7 迁入已有认知的写入接口（META、AMZN、GOOGL、MSFT）

**目标**：给 P0D-P1 的 P1.4 迁移脚本提供一个写入口，把旧 Dalton 和 OpenClaw 的 thesis、debate、驱动、跟踪状态，作为 PK.2 格式的初始版本（`source='import'`）写进知识层，作为 P1 的验收输入。提取和渲染由 P1.4 负责，本步只提供接口和映射规则。

**前置依赖**：PK.4；P0D-P1 §P1.4 的 `scripts/dalton/migrate_legacy_knowledge.py`（它调用本接口）；P1.3 已建好工作区骨架。

**改动/新增文件**：
- `src/dalton/app/router.py` 中的 `POST /api/v1/dalton/knowledge/{workspace_id}/{doc}/import`（只在 `dalton.enabled` 时注册，仅 `pm_user_ids` 可调用）
- 不再单独写 `import_knowledge.py`：P1.4 的迁移脚本直接调用本接口

**具体做法**：
- 请求体：`{"content": <整份 PK.2 文档>, "reason": "迁入自 <来源>@<sha>", "provenance": [...]}`；
- import API 走与工具相同的 `service.apply_update`（op=`create`；文档已存在时返回 `409 already_exists` 和当前版本号），设 `source='import'`；同样只检查三条硬性不变量，其余规则作为警告随响应返回，供迁移报告统计；
- 迁入的支柱 `provenance='inherited'`；`official` 一律为 null（旧系统正式 thesis 为 0 版，蓝图 §2），迁移观点在 thesis.md 里标"迁移观点，待复核"；
- 文件写入需要沙箱：API 先落库（`files_written_at` 为空），再触发一次 PK.5 的 `apply_pending`。【待验证】从路由获取沙箱用 `workspace_files/_shared.py` L390 的 `_acquire_sandbox_to_change`；
- 不能映射的旧字段由 P1.4 写进 `sources/legacy/` 和 `research/legacy-import-<date>.md`，不进知识文档；
- 一致预期快照（`data/consensus/`）、`model/legacy/`、`sources/` 由 P1.4 经普通的工作区文件接口写入，不走本接口。

**验证方式**：P1.4 对 4 家公司跑 `--apply`，然后在 UI 打开各自的 `thesis.md` 和 `debate.md`。

**验收标准**：
- 4 家公司各有迁入的 company、thesis、debate、drivers、management、lessons、agenda 文档（`models` 有旧模型时才建），且都通过 schema；
- `debate.md` 的 top debates 和迁入来源一致（抽查 2 条）；
- 重复执行：第二次返回 `already_exists`，迁移脚本报告"已存在 vN，跳过"。

**风险与注意**：旧 debate map 自 10-16 起停更，迁入时由 P1.4 在 `agenda.open_questions` 里加一条"复核 10-16 以来的变化"（`value=high`、`urgency=now`）。

**预估工时**：1 人日（接口 0.5、与 P1.4 联调 0.5；不含提取本身）。

---

## 2. 发布治理（PK.8 – PK.13）

整体流程：

```
草稿（drafts/<slug>.md，随时可读，随时可改）
  │ check_draft(path)            ← 草稿期：只给反馈，生成 <slug>.audit.md 和带"未核实"标注的副本，不阻止任何事
  ▼
publish_report(draft_path, ...)  ← 原生直连工具，只在分析师主智能体上
  1 数字核验  ──有未匹配──▶ 返回清单给分析师（状态 audit_failed，不审稿，不发卡）
  2 独立审稿  ──revise────▶ 返回审稿意见（状态 review_revise）
              ──无可用审稿模型──▶ review_unavailable：草稿照常可读，不能发布
  3 PM 审批   ──在交互线程里：interrupt → ToolApprovalCard（approve / reject + 理由）
              ──在无人值守 run 里：不发 interrupt，状态停在 awaiting_pm，写入议程 pm_handoffs
  4 消费审批（批一次、执行一次）→ 写 published/ 文件 + dalton_publications 行（含文件哈希和字节）+ thesis.official
```

这是整条研究链里**唯一的闸门**：草稿、研究笔记、知识层的日常修改都不受它约束。发布物只落在工作区的 `published/` 和数据库里；基金 wiki 只读，不做任何回写。

### PK.8 发布相关表（迁移 d005）

**目标**：在数据库里记录发布请求的状态机，以及不可变的发布记录和文件字节。

**前置依赖**：PK.3（复用 `dalton_forbid_mutation()`）；`d004_provenance_locator`（P0D-P1 §P0-D.3b，同在 P3 执行）。

**改动/新增文件**：
- 新建 `src/dalton/migrations/versions/d005_publications.py`（`revision="d005"`、`down_revision="d004"`）
- 新建 `src/dalton/database/publications.py`

**具体做法**：

```sql
CREATE TABLE IF NOT EXISTS dalton_publish_requests (
    publish_request_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        VARCHAR(255) NOT NULL,
    workspace_id   UUID NOT NULL,
    conversation_thread_id UUID NOT NULL,
    tool_call_id   TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('initial_coverage','full_coverage','bullbear','event_note','earnings_preview',
                                                 'earnings_review','thesis_update','topic','conviction_call')),
    title          TEXT NOT NULL,
    slug           TEXT NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9-]{2,60}$'),
    draft_path     TEXT NOT NULL,
    attachments    JSONB NOT NULL DEFAULT '[]',   -- [{path, role:'pdf_source'|'model'|'appendix'|'facts', sha256}]
    bundle_sha256  VARCHAR(64) NOT NULL,          -- 草稿 + 附件的联合哈希；批准与执行时都要复核
    updates_thesis BOOLEAN NOT NULL DEFAULT FALSE,
    thesis_version_no INTEGER,                    -- 提交时的 thesis 版本（updates_thesis 时必填）
    status         TEXT NOT NULL CHECK (status IN ('auditing','audit_failed','reviewing','review_revise',
                     'review_unavailable','awaiting_pm','approved','publishing','published','rejected','superseded','expired')),
    audit          JSONB,                         -- PK.9 的结果
    review         JSONB,                         -- PK.10 的结果 {model, family, verdict, reasons[], usage}
    decided_by     VARCHAR(255), decided_at TIMESTAMPTZ, decision_message TEXT,
    publication_id UUID,
    error          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (conversation_thread_id, tool_call_id)  -- 工具在 resume 时从头重跑，靠它幂等
);
CREATE INDEX IF NOT EXISTS idx_dalton_pr_ws_status ON dalton_publish_requests (workspace_id, status);

CREATE TABLE IF NOT EXISTS dalton_publications (
    publication_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    publish_request_id UUID NOT NULL UNIQUE REFERENCES dalton_publish_requests(publish_request_id),  -- Dalton 表之间可以有外键
    user_id        VARCHAR(255) NOT NULL,
    workspace_id   UUID NOT NULL,
    ticker         TEXT NOT NULL,
    kind           TEXT NOT NULL,
    title          TEXT NOT NULL,
    published_date DATE NOT NULL,
    files          JSONB NOT NULL,   -- [{path:'published/2026-10-20-meta-q3-review.md', sha256, bytes, mime}]
    bundle_sha256  VARCHAR(64) NOT NULL,
    sources        JSONB NOT NULL,   -- 数字核验的匹配表快照（每个数字 → 来源 + 摘录 + sha）
    audit_summary  JSONB NOT NULL,   -- {figures: 142, matched: 142, exempt: 9}
    reviewer       JSONB NOT NULL,   -- {model, family, verdict:'pass', reasons[], reviewed_at}
    approver_user_id VARCHAR(255) NOT NULL,
    approved_at    TIMESTAMPTZ NOT NULL,
    thesis_version_no INTEGER,       -- 这次发布确立的 thesis 正式版本（updates_thesis 时）
    supersedes_publication_id UUID REFERENCES dalton_publications(publication_id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_dalton_pub_ws_date ON dalton_publications (workspace_id, published_date DESC);

CREATE TABLE IF NOT EXISTS dalton_publication_files (
    publication_id UUID NOT NULL REFERENCES dalton_publications(publication_id),
    path           TEXT NOT NULL,
    sha256         VARCHAR(64) NOT NULL,
    mime           TEXT NOT NULL,
    content        BYTEA NOT NULL,   -- 永久留档；published/ 被删改时 PK.5 只警告，需要时由 owner 按这份字节手工恢复；单文件上限 publish.max_file_bytes
    PRIMARY KEY (publication_id, path)
);
CREATE TRIGGER trg_dalton_pub_immutable BEFORE UPDATE OR DELETE ON dalton_publications
  FOR EACH ROW EXECUTE FUNCTION dalton_forbid_mutation();
CREATE TRIGGER trg_dalton_pubfiles_immutable BEFORE UPDATE OR DELETE ON dalton_publication_files
  FOR EACH ROW EXECUTE FUNCTION dalton_forbid_mutation();
```

文件字节存 `BYTEA`，不复用 `workspace_file_blobs` 对象存储：那套存储会把没有引用的 blob 在 7 天后回收（`sweep_blob_garbage`），不适合保存永久记录。

`publications.py` 提供：
- `get_or_create_request(thread_id, tool_call_id, **fields) -> (row, created)`：`INSERT … ON CONFLICT (conversation_thread_id, tool_call_id) DO UPDATE SET updated_at=dalton_publish_requests.updated_at RETURNING *`，写法同 `order_attempts.insert_attempt`；
- `transition(request_id, from_statuses, to, **cols) -> row | None`：带条件的 UPDATE，加 RETURNING；
- `consume_approval(request_id, bundle_sha256) -> row | None`：`UPDATE … SET status='publishing' WHERE id=%s AND status='approved' AND bundle_sha256=%s RETURNING *`，这是唯一的执行令牌；
- `insert_publication(...)`；`list_publications(workspace_id)`；`get_publication(id)`；`list_pending(user_id)`。

**验证方式**：升级、降级、再升级；手工对 `dalton_publications` 做 UPDATE，应被拒绝。

**验收标准**：三张表和触发器都在；`consume_approval` 在两个并发会话里只有一个返回行。

**风险与注意**：没有指向上游表的外键（D10），删除工作区不会碰上只追加触发器；正式发布记录永久保留，作为档案。

**预估工时**：0.75 人日。

---

### PK.9 数字核验引擎 + `check_draft`（草稿期反馈）

**目标**：从报告里抽出每一个数字，按"指标 + 期间 + 单位 + 数值"匹配到来源记录、模型单元格或知识层证据。匹配不上的列成清单。草稿期只出反馈和标注；发布时同一个引擎作为闸门。

**前置依赖**：PK.4（证据台账）；`models.json`；**P0D-P1 §P0-D.3b 的逐文档来源记录**（`research_doc` 类型，带 `dalton_source_url`、`dalton_as_of`，迁移 `d004_provenance_locator`）。它和本步一起在 P3 实施；没有它时 `prov:` 只能指到整次 MCP 调用（`mcp_tool` 记录），匹配仍然可做，但来源身份和 as-of 要从正文里读。

**改动/新增文件**：
- 新建 `src/dalton/publish/numbers.py`：抽取
- 新建 `src/dalton/publish/match.py`：匹配
- 新建 `src/dalton/publish/audit.py`：编排，输出 `AuditResult`
- 在 `src/dalton/tools/publish_tools.py` 中新增 `check_draft` 工具
- 新建 `plugins/dalton/skills/dalton-publish/SKILL.md`：写作约定，说明怎么让数字能被核验

**具体做法**：

**(1) 写作约定（降低匹配难度的主要手段）**。数字后面用 markdown 脚注标来源，读者看到的正文仍然干净：

```markdown
3Q26 广告收入 512 亿美元，同比 +21%[^e1]；我们的 FY26 EPS 预测 28.40 美元[^m1]，高于一致预期 27.10 美元[^c1]。

[^e1]: evidence:E-20261030-01
[^m1]: model:model/META-model-v13.xlsx#Model!H42
[^c1]: prov:3f6c…（Yahoo Finance via yfinance 一致预期，researchdata estimates，2026-10-28）
```

脚注目标有四种：
- `evidence:<E-id>`：知识层证据；
- `model:<path>#<Sheet>!<Cell>`：模型单元格；
- `prov:<provenance_record_id>`：本工作区的来源记录（优先指到 §P0-D.3b 的 `research_doc` 逐文档记录）；
- `pub:<publication_id>`：引用已发布成果。

表格可以在表头下一行或表尾写一个"来源"行，整张表共用来源。

**事实底表（`role:"facts"` 附件）**。PS 的 skill（如 `dalton-bullbear`）会产出 `facts.md`，其中有一个 `<!-- FACTS-LEDGER -->` 块：每行一个数字，列为 `id | metric | period | value | unit | ref`，`ref` 用上面四种脚注目标之一。正文可以用 `[^f:<id>]` 指向底表行。审计把底表当作索引：先按 `f:<id>` 或"指标 + 期间 + 数值"在底表里找到行，再**照常核验该行的 `ref`**（规则同下表 1–3）。底表本身不是来源，`ref` 核验不过的底表行等于没有来源。

**(2) 抽取（`numbers.py`，确定性逻辑）**：
- 用正则识别：数值（含千分位、小数、负号、括号负数）+ 可选的币种前缀（`$`、`US$`、`HK$`、`¥`、`RMB`）+ 量级（`k/mn/m/bn/b/万/亿/千亿`）+ 单位（`%`、`pp`、`bp`、`x`、`倍`、`美元/港元/元`）。
- 排除：日期和期间本身（`2026-10-19`、`FY2026`、`3Q26`、`10-29`）、章节编号、脚注号、列表序号、页码、代码块、markdown 链接 URL、出现在"免责声明"段的数字。
- 每个数字记录上下文：所在句子；在表格中的话，加上行表头和列表头；最近的期间词（同句优先，其次表头，再次段首）；最近的脚注。
- 在表格里，**指标 = 行表头，期间 = 列表头**，这是确定性的。
- 在正文里，"指标/期间"由一次结构化 LLM 调用标注：`LLMService.complete(slot="background", response_schema=FigureLabels)`。输入是按句分组的已抽出数字；**模型只能给已抽出的数字打标签，不能新增数字**。服务端检查返回的数字 id 集合和输入完全一致。标注出错只会让结果偏向"匹配不上"，这是保守方向。
- 输出 `Figure{id, raw, value, unit, scale, currency, metric, period, label_hint(estimate|consensus|guidance|reported|derived|unknown), footnote, location{line, table?, row?, col?}}`。

**(3) 匹配（`match.py`）**。按顺序试，命中即停：

| 次序 | 目标 | 规则 |
|---|---|---|
| 0 | 脚注 `f:<id>` 或底表命中 | 取 facts 附件 FACTS-LEDGER 的对应行，转而核验该行的 `ref`（按 1–3） |
| 1 | 脚注 `evidence:` | 读最新 thesis.json 的该证据，在 `figures[]` 里找 metric 相近、期间相同、单位换算后数值在舍入容差内的项 |
| 2 | 脚注 `model:` | 在沙箱里用 openpyxl `data_only=True` 读该单元格（先要求 `models.json` 里这一版 `recalculated=true`，否则先跑 xlsx skill 的 `recalc.py`），按数值和容差比较；期间取该列表头 |
| 3 | 脚注 `prov:` | 取 `provenance_result_bodies` 正文（`/provenance/{rid}/body?full=true` 的同一 DB 函数），在正文里找该数值的所有写法（`51,200`、`51.2bn`、`512 亿`），并要求同一窗口（±300 字符）出现指标关键词和期间 |
| 4 | 无脚注：自动搜索 | 依次搜：本 thesis 的 evidence.figures；当前模型版本的 key_outputs 和带 `Source:` 批注的输入单元格（xlsx skill 的约定，SKILL.md L131）；本工作区近 90 天线程的 provenance 正文（按 thread → workspace 关联：`conversation_threads.workspace_id`） |
| 5 | 派生数 | label_hint=derived（增速、利润率、倍数）时，必须能指到模型单元格（规则 2），或在同一段落里能用两个已匹配的数字重算出来（容差同下） |

容差：报告里的数字精确到最后一位有效数字时，允许 ±0.5 个末位单位；百分比允许 ±0.05pp；单位换算表放在 `match.py`（`亿=1e8`、`bn=1e9`、`万=1e4`、`bp=0.01pp`）。

期间必须相等；指标用别名表做模糊匹配（`revenue/营收/收入/营业收入`），别名表放在 `plugins/dalton/skills/dalton-publish/metric_aliases.json`，分析师可以追加。

特殊规则（来自蓝图 §3.1）：
- `label_hint=consensus` 的数字，**只能**匹配到 `prov:` 来源，且来源是一致预期类：`research_doc` 的 identity 为 `street_estimate`，或 `provider` 以 `publish.consensus_providers` 中的前缀开头；否则判为"无来源"。
- 目标价、IRR、三情景收益只能匹配模型单元格。
- 文件读取类 provenance 只存了 snippet（`middleware.py` L707-734）。这类来源要重读该工作区文件，并比对 `result_sha256`；sha 不一致时判为"来源文件已变化"，作为未匹配处理。

**(4) 豁免**：
- 引用块里管理层原话中的数字；
- 显式标注 `〔示意〕` 的举例数字；
- 结构性数字（"三个支柱"、"第 2 季度"）由抽取规则直接排除。

豁免数单独计数，写进结果。

**(5) 输出 `AuditResult`**：

```json
{"draft_sha256": "…", "figures": 142, "matched": 131, "exempt": 9, "unmatched": [
  {"id": "F-087", "raw": "27.10 美元", "line": 48, "metric": "FY26 EPS 一致预期", "period": "FY2026",
   "reason": "consensus_needs_provenance", "hint": "加脚注 prov:<record>，来源必须是一致预期数据接口"},
  {"id": "F-102", "raw": "+34%", "line": 77, "metric": "Reels 收入增速", "period": "3Q26",
   "reason": "no_source_found", "candidates": [{"kind":"prov","id":"…","value":33.6,"why":"数值差 0.4pp 超容差"}]}],
 "matches": [{"id": "F-001", "kind": "evidence", "ref": "E-20261030-01", "excerpt": "…", "source_sha256": "…"}]}
```

**(6) `check_draft(draft_path)` 工具**：
- 跑同一个审计；
- 写侧车文件 `drafts/<slug>.audit.md`（中文清单，按行号排序）；
- 写标注副本 `drafts/<slug>.annotated.md`，未匹配的数字改成 `**27.10 美元**⟨未核实⟩`；
- **不改原稿，不阻止任何事**；
- 返回一行摘要：`142 figures: 131 matched, 9 exempt, 2 unverified → drafts/x.audit.md`。

分析师写完草稿时由 skill 要求自检一次。草稿的读者（PM）打开 `.annotated.md` 就能看到哪些数字还没核实。

**验证方式**（真实数据）：
1. 用 META 工作区里一份真实草稿：先不加脚注，跑 `check_draft`，记录未匹配数；
2. 让分析师按清单补脚注，再跑一次，未匹配数应降到 0 或只剩真没来源的；
3. 故意把一个数改错 1 位（512 → 521），应被列为未匹配，`candidates` 里列出 512 的来源。

**验收标准**：
- 在一份 ≥ 100 个数字的真实报告上，人工抽查 20 个"已匹配"数字，误匹配 0 个；
- 人工抽查全部"未匹配"，其中确实无来源或写错的 ≥ 80%（其余是抽取或别名问题，记进别名表）；
- 一份报告的核验时间 < 60 秒，LLM 标注费用 < $0.05（`LLMService.complete` 的用量按 P0.8 计入日额度）。

**风险与注意**：
- 中文数字写法（"约五百亿"）第一版不识别，skill 里要求一律用阿拉伯数字；
- provenance 正文超过 64KiB 时被截断，或 sha 校验失败时被丢弃（`_verify_source_sha`）。这类数字只能靠证据或模型匹配，skill 里要说明：重要数字先登记进证据台账；
- 不要用 LLM 判断"是否匹配"，匹配必须是确定性的。

**预估工时**：4 人日（抽取 1.25、匹配 1.5、xlsx 读取 0.5、工具与侧车 0.75）。

---

### PK.10 独立审稿：`dalton-reviewer`（不同模型家族）

**目标**：由和作者不同家族的模型，独立判断论证质量和结论含义，给出 `pass` 或 `revise` 及理由。审稿缺席时，草稿照常可读，但不能发布。

**前置依赖**：PK.9（审稿输入包含核验结果）。

**改动/新增文件**：
- 新建 `plugins/dalton/agents/dalton-reviewer.md`（草稿期可自愿派出的子智能体；正文同时作为服务端审稿的 system prompt）
- 新建 `src/dalton/publish/review.py`

**具体做法**：

**草稿期审稿子智能体的模型不固定。** 主模型切换时（P0.6），`scripts/dalton/apply_role_models.py`（P0.6 新增步骤）用 `role_policy.family_of()` 从 `publish.reviewer_models` 里挑第一个与主模型家族不同、且支持工具的模型，改写这个文件的 `model:` 字段。frontmatter 里写的只是初始值。

**(1) `plugins/dalton/agents/dalton-reviewer.md`**（frontmatter 字段依据 `src/server/services/plugins/agents.py` 和 `subagents/merge.py`）：

```markdown
---
description: Independent reviewer for a Dalton draft. Judges argument, evidence and implications; read-only; verdict pass or revise with reasons.
model: ocb-openai-gpt-6.1-sol   # 初始值；由 PK.10 的 setup 步骤用 role_policy.family_of() 按当前主模型改写成不同家族的带工具模型
effort: high
tools: [web]
preferred_skills: [dalton-conventions, thesis-tracker]
available_to: [analyst]
---
你是基金的独立审稿人，不是作者，也不替作者改稿。你只读，不写任何文件，不调用任何会修改工作区的工具。

审稿对象：一份准备正式发布的研究成果，以及它的数字核验结果、当前 thesis、debate、管理层风格。

逐项判断，每项给"通过 / 需修改"和一句理由，引用原文行号：
1. 判断先行：结论是否在开头写清，是否回答了这类报告必须回答的问题（按 dalton-conventions 对应报告类型的问题清单）。
2. 证据链：关键结论是否有 1–2 级来源支撑；是否把新闻或专家当成了事实；同一份研报被多处转述是否只算一条来源链。
3. 分层：估计、假设、公司指引、一致预期是否分开标注；house case 和市场基准是否分开；一致预期是否被当成了我们的判断。
4. 反方：是否写了最强反驳并正面回应；改判条件是否具体到可验证。
5. 预期差：如果给出 conviction，是否说清"市场哪里错、为什么错、什么会让市场改变"；没有足够预期差时是否直说"暂无高 conviction"。
6. 与知识层一致：结论和 thesis 支柱状态、house view 是否一致；不一致时，报告是否明确提出修订并给出理由。
7. 含义：对仓位建议的推导是否合理；风险是否量化到情景。

数字是否有来源由核验系统负责，你不重复对账；但看到核验结果里"已匹配"的数字被错误解读（期间、口径、单位用错）时，必须指出。

输出严格 JSON：
{"verdict":"pass"|"revise","summary":"一句话","items":[{"check":1,"result":"pass"|"revise","reason":"…","lines":[12,13]}],"must_fix":["…"],"nice_to_have":["…"]}
有任何一项 must_fix，verdict 必须是 revise。
```

注意：
- `tools: [web]` 会带上 web 加 filesystem（`merge.py` 的 `tools_for_words`）。子智能体没有只读开关，草稿期的只读只靠 prompt；即使它写了文件，也只是被 PK.5 记录的一次直接修改。草稿期子智能体要用工具，所以 `model` 必须是能调工具的模型（不能是 CLI gateway 类 text-only 模型）；lead 换成 Gemini 家族时，在 Plugins → Subagents 里把它改成别的家族（P0.6 的角色检查行会提示）。
- 草稿期分析师可以 `Task(dalton-reviewer)` 拿反馈，但这份反馈**不作为发布凭据**。

**(2) 服务端审稿（权威，`review.py`）**：

```python
async def run_review(*, user_id: str, author_model: str, bundle: ReviewBundle) -> ReviewResult:
    model = pick_reviewer_model(author_model)          # 见 (3)；None → ReviewResult(status="unavailable", reason=...)
    system = load_agent_body("dalton-reviewer")        # 读 plugin_subagents 表或 bundled 文件，确保与 .md 同源
    user = render_review_input(bundle)                 # 报告全文（带行号）+ AuditResult.matches 摘要 + thesis.md + debate.md + management.md
    result, usage = await LLMService(agent_config=…).complete(
        user_id=user_id, system_prompt=system, user_prompt=user,
        response_schema=ReviewVerdict, request_model=model, slot="primary",
        reasoning_effort="high", return_token_usage=True)
    return ReviewResult(status="done", model=model, family=family_of(model), verdict=result, usage=usage)
```

- 输入超过审稿模型上下文的 60% 时，按顺序裁剪：management.md → debate.md 的已关闭条目 → 报告附录；
- `usage` 由 `LLMService.complete` 写入用量，按 P0.8 计入日额度（P0.8 第 4 节的"未预留 usage"一项）；
- 超时 `asyncio.wait_for(…, 300)`。超时或供应商错误时，按时间退避重试（30s、2min），10 分钟内仍失败则 `review_unavailable`。不设固定次数（蓝图 §3.2）。

**(3) 家族判定**：直接复用 P0.6 的 `src/dalton/llm/role_policy.py: family_of(model_key)`，不另写一套：
- `ocb-*` 键（provider `openclaw-broker`）读 P0.4 overlay 条目里的 `dalton.family`。它由同步脚本根据 broker 目录的上游 `provider` 和模型 id 生成，同一模型经不同网关接入仍算同一家族（例如 `ocb-claude-cli-gateway-claude-opus-5-5` 和原生 `claude-opus-5-5-oauth` 都是 `anthropic-claude`）；
- 原生键（`claude-oauth`、`codex-oauth`、`deepseek`、`z-ai`、`gemini` 等）用 P0.4 第 3 节同一张前缀表。

lead 模型可以切换（P0.6），所以作者家族每次按实际模型判定：取线程最近一次 run 的模型（run ledger，`conversation_responses` 中的模型字段【待验证 列名】）。`pick_reviewer_model` 按 `publish.reviewer_models` 的顺序（§0.5：`ocb-openai-gpt-6.1-sol`、`ocb-google-gemini-4-argon`、`ocb-claude-cli-gateway-claude-opus-5-5`、`claude-opus-5-5-oauth`），取第一个"家族 ≠ 作者家族、用户有可用凭证（`model_resolves`，`subagents/context.py` L62）、且在 overlay 里可见"的模型。服务端审稿不带工具，所以 text-only 模型可以入选。

**验证方式**：
1. 用默认 lead（`claude-opus-5-5-oauth`）写作，发布时应选中 `ocb-openai-gpt-6.1-sol`，`dalton_publish_requests.review` 里 `family='openai-gpt'`。再把 lead 切到 `ocb-xai-grok-4.7` 重复一次，仍选中第一个候选（家族不同）。
2. 把 `reviewer_models` 配成只有和作者同家族的模型，应得到 `review_unavailable`，草稿仍可在 FilePanel 打开。
3. 故意交一份没有反方论证的草稿，应得到 `revise`，`must_fix` 里点名第 4 项。

**验收标准**：三条全部复现；审稿结论里的行号能对应到原文。

**风险与注意**：
- 审稿模型的输出必须是结构化的。解析失败按供应商错误处理（退避），不要当成 pass；
- 不要让分析师有办法跳过服务端审稿。`publish_report` 不接受"已审稿"之类的入参。

**预估工时**：2 人日。

---

### PK.11 `publish_report` 工具：状态机、PM 审批、不可变写入

**目标**：一个原生直连工具串起核验、审稿、审批和写入。审批"批一次、执行一次"；写入不可变；全程可重入。

**前置依赖**：PK.8–PK.10；PK.4（更新 thesis 正式版本）。

**改动/新增文件**：
- 在 `src/dalton/tools/publish_tools.py` 中新增 `publish_report`
- 新建 `src/dalton/publish/service.py`、`src/dalton/publish/files.py`
- 前端：第一期零改动（D4）；可选的小补丁见 PK-UI.3

**具体做法**：

**(1) 签名**：

```python
@tool("publish_report")
async def publish_report(
    draft_path: str,                       # drafts/<slug>.md
    title: str,
    kind: Literal["initial_coverage","full_coverage","bullbear","event_note","earnings_preview","earnings_review",
                  "thesis_update","topic","conviction_call"],
    slug: str,
    attachments: list[dict] | None = None, # [{"path":"drafts/<slug>.html","role":"pdf_source"},{"path":"model/META-model-v13.xlsx","role":"model"},
                                           #  {"path":"drafts/<slug>.facts.md","role":"facts"}]   role ∈ pdf_source|model|appendix|facts
    updates_thesis: bool = False,          # 批准后把当前 thesis 设为正式版本
    request_id: str | None = None,         # 继续一个已在 awaiting_pm 的请求（无人值守 run 准备好、PM 在交互线程里批）
    *, config: RunnableConfig, tool_call_id: Annotated[str, InjectedToolCallId],
) -> str:
    """Publish a finished draft as the fund's official research: audits every figure, gets an independent review, then asks the PM to approve.

    Call only when the draft is final; while drafting use check_draft. Unmatched figures or a "revise" review come back as a list to fix — fix and call again.
    Returns: the published paths, or what blocks publishing and what to do next.
    """
```

**(2) 主流程（`service.py: run_publish`）**。工具在 resume 时会从头重跑，所以每一步都先看数据库状态：

```
row, created = get_or_create_request(thread_id, tool_call_id, ...)    # 或按 request_id 取，并校验同一 workspace
match row.status:
  'published'          → 返回已发布路径（幂等）
  'publishing'         → 跳到第 6 步继续写（崩溃恢复）
  'rejected'|'expired'|'superseded' → 返回原因，提示重新调用（新的 tool_call_id 会建新请求）
  'audit_failed'|'review_revise'|'review_unavailable' → 只有 bundle 变化后（新调用）才重跑；同一请求直接返回上次结果
1. 计算 bundle_sha256（读 draft 和附件内容）。新请求：status=auditing
2. 数字核验（PK.9）→ 有 unmatched：status=audit_failed，返回清单（同时写 drafts/<slug>.audit.md）
3. status=reviewing → run_review（PK.10）
     unavailable → status=review_unavailable，返回"草稿保留在 drafts/，审稿恢复后再调用"
     revise      → status=review_revise，返回 must_fix
     pass        → status=awaiting_pm
4. 审批：
   a. 判断无人值守：线程 origin.type ∈ {'automation','agent'}（threads_write.py 的约定）
      → 不发 interrupt（发了会让自动任务 run 记为 FAILED），向 agenda 追加 pm_handoffs
        {kind:'publish_approval', what: title, blocks: []}，返回"已就绪，等待 PM 审批，request_id=…"
   b. 交互线程：answer = interrupt({
        "kind": "publish_approval",
        "action_requests": [{
          "type": "publish_report", "name": "publish_report",
          "tool_call_id": tool_call_id, "attempt_id": str(row.id),
          "description": f"正式发布《{title}》",
          "args": {"公司": "META", "类型": "业绩点评", "草稿": draft_path,
                   "数字核验": "142 个数字：133 已匹配，9 豁免，0 未核实",
                   "独立审稿": "通过 · ocb-openai-gpt-6.1-sol（openai-gpt）· 2 条建议",
                   "发布为": "published/2026-10-30-meta-3q26-review.md / .pdf / .xlsx",
                   "thesis 正式版本": "v9 → v14" }}],
        "review_configs": [{"action_name": "publish_report", "allowed_decisions": ["approve","reject"]}]})
      decision = (answer.get("order_decisions") or {}).get(str(row.id)) or (answer.get("decisions") or [None])[0]
      缺失 → 视为拒绝（"没有裁决就是拒绝"，同 order_governance）
5. 审批人校验：resume 是线程所有者发的（thread 属于 user_id）；且 user_id ∈ `pm_user_ids`（`src/dalton/dalton.yaml`），否则拒绝。
   approve → transition(awaiting_pm → approved, decided_by, decided_at)
   reject  → transition(awaiting_pm → rejected, decision_message)，返回 PM 的理由
6. 执行令牌：consume_approval(request_id, bundle_sha256)
   拿不到行：bundle 已变化 → transition(approved → superseded)，返回"草稿在审批后被改动，请重新发布"
7. 写文件（files.py），全部写完才进入第 8 步：
   - published/<YYYY-MM-DD>-<slug>.md   草稿原文，脚注保留；在文首插入发布信息块（审批人、时间、审稿模型、publication_id）
   - published/<…>.pdf                  有 pdf_source(html) 附件时，用 pdf_render 渲染（经 /api/v1/wsfiles 路由）；
                                        没有附件或没装 pdf 依赖 → 跳过并在结果里说明
   - published/<…>.xlsx                 model 附件的冻结副本
   - 目标路径已存在且哈希不同 → 加后缀 -2、-3，绝不覆盖
8. 一个事务：insert_publication + 每个文件 insert dalton_publication_files（含字节）
            + transition(publishing → published, publication_id)
9. updates_thesis → knowledge service 以 source='publish' 写 thesis.official
   （= 批准时 thesis 最新版本的冻结副本，含 pillars），并把 pillars[].provenance 设为 approved；
   重渲染 published/INDEX.md、thesis.md、agent.md 托管块；
   如果议程里有对应的 pm_handoffs，标为 answered
10. 返回：Published META 3Q26 review: published/2026-10-30-meta-3q26-review.{md,pdf,xlsx} · thesis official v14 · publication 7c1e…
```

**(3) 无人值守准备、交互审批**。PM 在任意一个该工作区的交互线程里说"批准发布 3Q26 点评"。分析师调用 `publish_report(..., request_id=<id>)`：
- 工具复核 `bundle_sha256` 没变；
- 不重跑核验和审稿；
- 直接进入第 4b 步发卡。

审批 TTL 为 `approval_ttl_hours`（168h）。研究总监每天把超期的请求转成 `expired`。

**(4) 批一次、执行一次的保证**：
- `approved → publishing` 只有一个 UPDATE 能成功（第 6 步）；
- `bundle_sha256` 让"批的是 A、发的是 B"不可能发生；
- 第 7–8 步失败时状态停在 `publishing`，下一次调用从第 7 步继续（研究总监日报会列出停在 `publishing` 的请求）。文件写入按哈希判断是否已存在，可以重复执行。

**验证方式**（真实调用）：
1. 交互线程：完整发布一份事件笔记。卡片出现（ToolApprovalCard，参数表就是上面的 `args`）→ 点批准 → `published/` 出现 md（和 pdf）→ `dalton_publications` 有行 → `thesis.md` 显示正式版本。
2. 点拒绝并填理由，工具结果里能看到 PM 的理由；`status=rejected`。
3. 批准前在另一个标签页改草稿一个字，再批准，应得到 `superseded`。
4. 在 `publishing` 状态下杀掉后端 worker（`docker compose restart backend`），重新进线程调用一次，应完成且不重复写。
5. 自动任务 run 里调用（用一个 once 自动任务指示"准备发布……"），run 应正常结束（不是 FAILED），议程里出现 `pm_handoffs`。之后在交互线程里批准，完成发布。
6. 刷新页面：卡片能从历史回放（`fromHistoryEvent.ts` 的工具审批分支），状态正确。

**验收标准**：
- 六条全部复现；
- `published/` 下没有任何文件被覆盖过（对比 `dalton_publication_files` 的哈希）；
- `select count(*) from dalton_publications where reviewer->>'verdict' <> 'pass'` 为 0；
- 正式发布物里未核实的数字为 0（用 `check_draft` 对 `published/*.md` 复跑，结果为 0）。

**风险与注意**：
- `ToolApprovalCard` 的标题文案用 `toolArtifact.directTool.*` 的 i18n 键拼 vendor 和 tool。vendor 为空串时，可能显示成"批准 publish report"一类的生硬文案【待验证】，可以用 PK-UI.3 的可选补丁修饰。
- `HITLDecision` 没有 `extra="allow"`，所以只用 `type` 和 `message`，不要依赖其他字段（勘查发现 `overrides` 可能被丢弃）。
- 不要把 `publish_report` 放进 PTC 封装或 MCP。它必须是主智能体的直连工具：UI 能看到，中间件能看到（蓝图 §4.8、根 AGENTS.md 的 PTC 说明）。

**预估工时**：3.5 人日。

---

### PK.12 失败行为总表

**目标**：每种失败都有明确的、不靠固定重试次数的处理方式，而且不阻止研究继续。

**前置依赖**：PK.9–PK.11。

**改动/新增文件**：无新文件；写进 `publish/service.py` 和 `dalton-publish/SKILL.md`。

**具体做法**：

| 情形 | 状态 | 对草稿 | 返回给分析师 | 后续 |
|---|---|---|---|---|
| 有未核实数字 | `audit_failed` | 不动，可读 | 清单 + `.audit.md` 路径 | 补脚注或改数，再调用（新 tool_call_id） |
| 审稿 revise | `review_revise` | 不动 | must_fix + nice_to_have | 修改后再调用 |
| 没有不同家族的可用审稿模型 | `review_unavailable` | 不动，可读 | "无法发布：缺少独立审稿" | 议程追加 `pm_handoffs{kind:'data_access', what:'配置审稿模型凭证'}`；不后台重试 |
| 已发布文件被改动或删除 | `published`（不变） | — | — | PK.5 在 agent.md 托管块里警告；数据库字节是留档，不自动恢复 |
| 审稿供应商临时故障 | 仍是 `reviewing`，在工具内退避 | 不动 | 10 分钟内恢复则继续；否则 `review_unavailable` | 同上 |
| 无人值守 run | `awaiting_pm` | 不动 | "等待 PM 审批 request_id=…" | 研究总监的日报列出 |
| PM 拒绝 | `rejected` | 不动 | PM 的理由 | 分析师按理由修改 |
| 审批后草稿被改 | `superseded` | 不动 | 重新发布 | — |
| 审批超期 | `expired` | 不动 | — | 研究总监日报提醒 |
| 非 PM 的人点了批准 | 仍是 `awaiting_pm` | 不动 | "只有 PM 能批准" | — |
| 写文件中途崩溃 | `publishing` | 不动 | 下一次调用完成写入 | 研究总监日报列出 |
| PDF 渲染不可用 | `published`（无 pdf） | — | 说明缺 PDF | 装 `pdf` 依赖后可补渲染，生成新文件名，不覆盖 |

**验证方式**：PK.11 的验证已经覆盖前 9 行；PDF 一行通过卸载 playwright 验证。

**验收标准**：表中每一行都能在 `dalton_publish_requests` 里找到对应状态的真实记录（实施期留截图）。

**风险与注意**：任何失败都不能删除或改写 drafts/。

**预估工时**：0.5 人日（主要是 skill 文案和联调）。

---

### PK.13 只读 API（给研究总监、对账和以后的界面用）

**目标**：后端提供只读查询，不建新页面。研究总监和对账使用；以后 owner 要页面时直接复用。

**前置依赖**：PK.3、PK.8。

**改动/新增文件**：
- 在 P0.7 已建的 `src/dalton/app/router.py`（`APIRouter(prefix="/api/v1/dalton")`）里追加下表端点，写法参照 `src/server/app/orders.py`，使用 `CurrentUserId`、`handle_api_exceptions`
- 不改 `src/server/app/setup.py`：路由已经由 P0.7 的钩子 `DALTON-HOOK(router)` 注册

**具体做法**：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/knowledge/{workspace_id}/{doc}/versions?limit=&before=` | 版本列表（version_no、source、reason、created_at、thread_id） |
| GET | `/knowledge/{workspace_id}/{doc}/versions/{n}` | 某一版全文 |
| GET | `/knowledge/{workspace_id}/{doc}/diff?from=&to=` | JSON 路径级差异（复用 `scripts/dalton/kdiff.py` 的函数，搬到 `src/dalton/knowledge/service.py`） |
| POST | `/knowledge/{workspace_id}/{doc}/import` | PK.7，仅 `pm_user_ids`；P1.4 迁移脚本调用 |
| PUT | `/automations/{name}` | PK.15：研究总监（Home）给公司工作区建、改 `dalton-*` 自动任务的写入 helper（唯一的写端点之一） |
| GET | `/verdicts?workspace_id=&since=&until=` | 结论 |
| GET | `/publications?workspace_id=` | 发布列表 |
| GET | `/publications/{id}` | 详情（文件、来源表、审稿、审批人） |
| GET | `/publish-requests?status=awaiting_pm` | 待 PM 事项 |
| GET | `/coverage/board` | 研究总监工具的同一份数据（PK.19） |
| GET | `/autonomy/report?week=2026-W43` | 自主性周报（PK.20） |

全部按 `user_id` 隔离。路由里不写 SQL，SQL 都放在 `src/dalton/database/`（`src/server/AGENTS.md` 的分层规则）。

**验证方式**：`curl -s localhost:8000/api/v1/dalton/knowledge/<ws>/thesis/versions | jq`（oss 模式下自动以 `local-dev-user` 身份）。

**验收标准**：每个端点都返回真实数据；跨用户访问返回 404。

**风险与注意**：`tests/unit/server/test_import_layering.py` 会检查分层，`services` 不能 import `handlers`；`src/dalton/` 内部同样保持 app → service → database 的方向。

**预估工时**：1 人日。

---

## 3. 自主研究议程（PK.14 – PK.20）

分工（避免重复花钱）：

| 谁 | 管什么 | 怎么触发 |
|---|---|---|
| 每家公司的 **watch 自动任务**（cron 轮询 + 预检） | **事件**：新公告、财报窗口、价格异动、领先指标越线、PM 新问题 | 有信号才唤醒该公司的分析师 |
| PS 建的**常驻任务**：夜间 thesis 增量 `dalton-<t>-delta.json`、价格触发 `dalton-<t>-move-up/-move-down.json`、每周六财报日历 `dalton-earnings-calendar.json` | 一致预期变化、单日大幅涨跌、官方财报日期 | 同样先过预检（PK.16），没有变化不唤醒 |
| 分析师**自己写的 once 自动任务** | 分析师明确给自己定的"下次什么时候、为什么" | 到点后过预检（只查额度、退避、是否等 PM），然后唤醒 |
| **研究总监**（Home，Chief of Staff，每日 cron + 预检） | **议程驱动**：到期验证、高价值开放问题，在当日额度内排序派单；每日计划；覆盖总览 | 有可做的事才唤醒；空闲日 $0 |

### PK.14 议程的维护规矩（`dalton-agenda` skill）

**目标**：分析师每次工作结束都留下"结论 + 下一项验证 + 下次唤醒"，议程始终是可执行的。

**前置依赖**：PK.2.8、PK.4。

**改动/新增文件**：新建 `plugins/dalton/skills/dalton-agenda/SKILL.md`。

**具体做法**：skill 规定"收尾三步"。每次由唤醒、PM 提问或自发研究触发的工作，结束前都要做：

1. **写结论**：`knowledge_append_verdict`。必须给五词之一（`NO_CHANGE`、`THESIS_STRENGTHENED`、`THESIS_WEAKENED`、`THESIS_BROKEN`、`NEW_THESIS`；md 里显示为 无变化／强化／削弱／证伪／新论点）、`next_validation`（what + due）和 `progress`；`trigger.ref` 填唤醒原因里的信号 id（PK.16 注入）。
2. **更新议程**：放在同一次调用的 `agenda_ops` 里。
   - 关闭已完成的 `V-`；新的验证项写进 `next_validations`。
   - 新问题追加到 `open_questions`，附 value 和 urgency。
   - 需要 PM 决定的事写进 `pm_handoffs`，同时把被阻塞的问题设为 `waiting_pm`。
3. **给自己排期**（可选）：只有当下一次需要在某个确定时间做事、而 watch 自动任务和研究总监都覆盖不到时，才写一个 once 自动任务（PK.15）。写完把文件名填进 `agenda.next_wake.automation_file`。

skill 同时写明：
- 一次工作里没有看到新证据，也要写结论 `NO_CHANGE` + `progress=none`，退避依赖这个信号（PK.18）；
- "暂无高 conviction"是合法结论；
- 不要为了"有产出"而降低 `NEW_THESIS`、`THESIS_STRENGTHENED` 的门槛；
- **已获 owner 授权**：分析师可以自主新建、修改、停用本工作区自己的 `dalton-*` 自动任务文件，不需要先问用户（LangAlpha 默认提示要求确认，`components/user_profile.md.j2` L71-73；这条授权同时写进用户层记忆和工作区 agent.md 的工作约定）。

**验证方式**：在 META 工作区里手动说"检查一下昨晚的 8-K"。结束时工具卡里应依次出现 `knowledge_append_verdict`（带 agenda_ops）。agenda.md 刷新，verdicts.md 多一条。

**验收标准**：连续 5 次不同触发的工作，每次都有结论和下一项验证（查 `dalton_verdicts`）。

**风险与注意**：结论偷懒（全是 NO_CHANGE）是旧 Dalton 的老问题（472 次全无变化，蓝图 §2）。研究总监的周报要统计各家公司的五词分布，PM 抽查。

**预估工时**：0.5 人日。

---

### PK.15 自己给自己排期：自动任务文件约定

**目标**：分析师和研究总监用 LangAlpha 现成的自动任务文件给自己排期，不改文件格式；所有 Dalton 自动任务（包括 PS 建的常驻任务）用一套命名，预检据此识别。

**前置依赖**：无（文件格式已存在：`src/server/services/automations/file.py`，README 在 `src/ptc_agent/agent/backends/automations.py` L16 起）。

**改动/新增文件**：
- 写进 `dalton-agenda` 和 `dalton-research-director` 两个 skill
- 新建 `src/dalton/autonomy/schedule.py`：`upsert_dalton_automation(user_id, name, entry)`，校验后经现有自动任务文件服务写入【待验证 `file.py` 里可复用的校验和写入函数名】
- `src/dalton/app/router.py` 加 `PUT /api/v1/dalton/automations/{name}`（REST helper），`src/dalton/tools/director_tools.py` 加同源的 `dalton_schedule_automation` 工具（PK.19）
- 新建 `scripts/dalton/bootstrap_watch.py`：为覆盖名单批量生成 watch 文件，调用上面的 REST helper

**具体做法**：

命名约定。文件名是预检识别 Dalton 自动任务的唯一依据，所以不需要往文件里写 `metadata`（`_Entry` 设了 `extra="forbid"`，写不进去）。`<t>` 是 ticker 小写、`.` 换成 `-`（例如 `meta`、`brk-b`），以满足文件名正则 `[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.json`（`paths.py` L497）。

| 文件名 | 触发 | 工作区 | 建立者 | 用途 | 预检探针（PK.16） |
|---|---|---|---|---|---|
| `dalton-<t>-watch.json` | cron | 公司 | PK（`bootstrap_watch.py`） | 事件轮询 | filing、earnings、price、indicator、pm_question、internal |
| `dalton-<t>-delta.json` | cron（每晚） | 公司 | PS.11 | 夜间 thesis 增量 | consensus（经 researchdata 桥）、filing、internal、price |
| `dalton-<t>-move-up.json` / `dalton-<t>-move-down.json` | price | 公司 | PS.10 | 原生价格触发（`pct_change_above` / `pct_change_below`） | move（触发本身即信号）+ 额度、退避、去重 |
| `dalton-<t>-v<YYYYMMDD>-<slug>.json` | once | 公司 | 分析师自己 | 自己定的下一项验证 | agenda（对应 V-） |
| `dalton-<t>-earn-<period>-<k>.json` | once | 公司 | PS.7 的日历任务那一轮 | 财报链：`k` ∈ `tm3`、`t0`、`tp1`（T-3 前瞻、T0 快讯、T+1 深度点评） | earnings（只认 `confirmed` 日期） |
| `dalton-earnings-calendar.json` | cron（每周六，Asia/Shanghai） | home | PS.7 | 刷新覆盖公司的官方财报日期 | calendar（当天总是唤醒；无到期 Core 公司时跳过） |
| `dalton-director-daily.json` | cron | home | PK.19 | 研究总监日程 | director |
| `dalton-director-weekly.json` | cron（周会日待 owner 定） | home | PK.19 / PS.13 | 周会复盘（workflow `weekly-coverage-review`，PS.13） | director |

**谁怎么写**：
- 分析师写**本工作区**的 `dalton-<t>-*` 文件，直接用 Write（已获 owner 授权，不需要先问用户，PK.14）；
- 研究总监在 Home 里给**公司工作区**建、改自动任务时，一律调用 `dalton_schedule_automation`（等价于 `PUT /api/v1/dalton/automations/{name}`），**不跨工作区写文件**。helper 校验：名称以 `dalton-` 开头且符合上表；`workspace_id` 属于当前用户；`_Entry` 字段合法；
- `bootstrap_watch.py` 和 PS 的安装脚本也走这个 helper。

样例（字段以 `_EDITABLE` 为准，`file.py` L84）：

`dalton-meta-watch.json`

```json
{
  "name": "META 事件监控",
  "status": "active",
  "cron_expression": "*/30 6-20 * * 1-5",
  "timezone": "America/New_York",
  "instruction": "Dalton 事件唤醒：按 <dalton_wake> 里的原因处理，按 dalton-agenda 收尾三步结束。",
  "workspace_id": "<META 工作区 id>",
  "thread": "new"
}
```

`dalton-meta-v20261029-dap.json`

```json
{
  "name": "META 验证 V-014：3Q26 Family DAP",
  "status": "active",
  "next_run_at": "2026-10-30T07:30:00",
  "timezone": "America/New_York",
  "instruction": "Dalton 计划唤醒：验证 V-014（agenda.md），对照 P1 的阈值 T3，给出五词结论。",
  "workspace_id": "<META 工作区 id>",
  "thread": "new"
}
```

`dalton-<t>-delta.json`、`-move-up/-move-down.json`、`dalton-earnings-calendar.json` 的正文由 PS 定义（PS.7、PS.10、PS.11），本节只规定名称。

几条规矩：
- `thread: "new"`：每次唤醒一个新线程，上下文干净，认知在知识层里。Automations 的 Feed 页面就是唤醒历史。
- once 任务跑完后不能再改（`file.py` L452："Write a new file for another run."），所以每次排期都写**新文件名**。研究总监每天把 14 天前已完成的 `dalton-*-v*`、`dalton-*-earn-*` 文件设为 `"status": "deleted"` 清理掉。
- 不在 Dalton 的文件里设失败次数一类的上限；预检本身"永不抛异常"（PK.16），不会让任务因 Dalton 的代码进入失败。

**验证方式**：
1. 跑 `bootstrap_watch.py`，在 Automations → Manage 里看到 4 家公司的 watch 任务；
2. 让分析师"把 V-014 排到 10-30 早上 7:30"，它直接写文件（没有向用户确认的步骤），Manage 里出现 once 任务，`agenda.next_wake.automation_file` 填上了文件名；
3. 在 Home 让研究总监"给 AMZN 加一个 V-020 的验证唤醒"，工具卡是 `dalton_schedule_automation`，没有跨工作区的 Write。

**验收标准**：
- 所有 Dalton 自动任务都符合命名约定，预检能从文件名识别出工作区和探针；
- 文件保存时没有 `NOT SAVED`；
- once 任务到点后正常触发（见 PK.16）。

**风险与注意**：用户自动任务的数量上限【待验证】，27 家 Core 公司 × 3–4 个常驻任务 + once 任务，必要时由研究总监清理已完成的 once 文件。

**预估工时**：0.75 人日。

---

### PK.16 零成本预检（迁移 d006）

**目标**：自动任务唤醒分析师之前，先由一层不调用模型、不启动沙箱的检查判断"有没有值得做的事"。没有就跳过，成本 $0；有就唤醒，并把原因和信号注入这一轮。

**前置依赖**：PK.3（读议程）；PK.15；P0.8 的 `get_budget_today(user_id)["remaining_usd"]`；P0D-P1 的 researchdata 桥（后端直连）。

**改动/新增文件**：
- 新建 `src/dalton/migrations/versions/d006_autonomy.py`（`revision="d006"`、`down_revision="d005"`）
- 新建 `src/dalton/database/watch.py`、`src/dalton/database/plans.py`
- 新建 `src/dalton/autonomy/preflight.py`
- 新建 `src/dalton/autonomy/probes/{__init__,agenda,sec,earnings,price,consensus,internal,calendar,director}.py`
- 钩子 `DALTON-HOOK(preflight)`：`src/server/services/automation_executor.py`，`execute()` 在 L812 之前调用预检；`_Firing` 加 `wake_context` 字段；`_run_turn` 把它注入 `additional_context`（§5 #5）
- 钩子 `DALTON-HOOK(precheck-outcome)`：`src/server/services/automation_settlement.py`，新增 `Outcome.PRECHECK_SKIPPED` 及其 `_Policy`（§5 #6）
- 钩子 `DALTON-HOOK(precheck-skip-reason)`：`src/server/models/automation.py`，`SkipReason` 加 `"precheck"`（L591，§5 #7）
- 钩子 `DALTON-HOOK(precheck-hidden)`：`src/server/database/automation.py`，`not_a_server_skip` 把 `'precheck'` 加进隐藏列表（L56-62，§5 #8）
- 【待验证】补丁 `web/src/pages/Automations/utils/status.ts`：为 `precheck` 加文案"预检跳过"；Feed 默认隐藏这类条目

**具体做法**：

**(1) 迁移 d006**（不建指向上游表的外键）：

```sql
CREATE TABLE IF NOT EXISTS dalton_watch_state (          -- 每个探针的游标；真值在库里
    workspace_id UUID NOT NULL,
    probe        TEXT NOT NULL,                          -- 'sec' | 'price' | 'consensus' | 'internal' | 'backoff:<kind>' | ...（hkex/cninfo 在 P6）
    cursor       JSONB NOT NULL DEFAULT '{}',            -- 如 {"last_accession": "0001326801-26-000123"}
    last_checked_at TIMESTAMPTZ, last_error TEXT, error_since TIMESTAMPTZ,
    PRIMARY KEY (workspace_id, probe)
);
CREATE TABLE IF NOT EXISTS dalton_signals (              -- 预检发现的信号；被结论消费
    signal_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      VARCHAR(255) NOT NULL,
    workspace_id UUID NOT NULL,
    kind         TEXT NOT NULL,                          -- filing|earnings_window|price_move|consensus_change|indicator|pm_question|internal_material|due_validation|scheduled
    ref          TEXT NOT NULL,                          -- accession / 'FY26Q3:t0' / '2026-10-29:move' / 'V-014'
    payload      JSONB NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at TIMESTAMPTZ, delivered_execution_id UUID,
    consumed_at  TIMESTAMPTZ, consumed_by_verdict_id TEXT,
    expired_at   TIMESTAMPTZ,
    UNIQUE (workspace_id, kind, ref)
);
CREATE INDEX IF NOT EXISTS idx_dalton_signals_open ON dalton_signals (workspace_id) WHERE consumed_at IS NULL AND expired_at IS NULL;
CREATE TABLE IF NOT EXISTS dalton_preflight_log (
    automation_execution_id UUID PRIMARY KEY,
    automation_id UUID NOT NULL, user_id VARCHAR(255) NOT NULL, workspace_id UUID,
    decision     TEXT NOT NULL CHECK (decision IN ('wake','skip')),
    reason_code  TEXT NOT NULL,                          -- no_signal|budget|backoff|waiting_pm|disabled|probe_error|signal
    reasons      JSONB NOT NULL, signal_ids UUID[] NOT NULL DEFAULT '{}',
    duration_ms  INTEGER NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_dalton_pf_user_time ON dalton_preflight_log (user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS dalton_daily_plans (
    user_id VARCHAR(255) NOT NULL,
    plan_date DATE NOT NULL,
    plan JSONB NOT NULL,                 -- PK.19 的计划；每次修订覆盖 plan，旧版本进 revisions
    revisions JSONB NOT NULL DEFAULT '[]',
    budget_usd NUMERIC(10,2), planned_usd NUMERIC(10,2),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, plan_date)
);
```

**(2) 挂点**（`automation_executor.py` 的 `execute()`，在 L812 `transition_execution(pending→running)` 之前）：

```python
# DALTON-HOOK(preflight): zero-LLM wake/skip decision for dalton-* automations
from src.dalton.autonomy import preflight as dalton_preflight

decision = await dalton_preflight.decide(automation, execution_id)   # 非 Dalton 自动任务直接返回 None；内部 wait_for 限时，不抛异常
if decision is not None and decision.skip:
    await settle(automation, execution_id, Outcome.PRECHECK_SKIPPED,
                 workspace_id=str(automation.get("workspace_id") or ""),
                 skip_reason="precheck", excerpt=decision.summary[:300])
    return
...
firing = _Firing(automation=automation, wake_context=decision.context if decision else None)
```

`_run_turn` 里，在 `additional_context` 末尾追加：

```python
if firing.wake_context:
    additional_context = [*(additional_context or []),
                          {"type": "directive", "content": firing.wake_context}]
```

`directive` 类型已经用于投递提醒（`_run_turn` L715-721），沿用即可。

`automation_settlement.py` 的新策略：

```python
Outcome.PRECHECK_SKIPPED: _Policy(status="skipped", from_statuses=("pending",), metric="precheck_skipped", schedule="close"),
```

- 不记 strike，不发 webhook。
- `schedule="close"`：once 任务关闭；price 任务按现有逻辑关闭或重新布防（`_schedule_action` L277）；cron 任务的下一次运行时间在 claim 时已经算好（`automation_scheduler.py` L217-230）。【待验证】`schedule="close"` 对 cron 是否是空操作。

为什么挂在 `pending` 阶段：这时还没有走信用闸门（L857）、没有建线程（L861-867）、没有启动投递（L888）、没有 heartbeat 任务。预检跳过时，除了 `automation_executions` 一行之外没有任何副作用。

**(3) `decide()` 逻辑**（全部是 Postgres 查询加外部 HTTP，零 LLM）：

```
if not file_name.startswith("dalton-"): return None
kind = 由文件名推出（PK.15 的表）：watch | delta | move-up | move-down | v | earn | earnings-calendar | director-daily | director-weekly
       无法识别的 dalton-* 名称 → 按 watch 处理并记一条 warning 日志（不让任务失败）
ws   = automation.workspace_id（earnings-calendar、director-* 在 home）
ctx  = 读 latest_valid_version 的 company、agenda —— 公司类任务没有议程则 skip(disabled, "知识层未初始化")
1. 开关：autonomy.enabled、company.autonomy.enabled、coverage_stage != 'paused'  → 否则 skip(disabled)
2. 额度：get_budget_today(user)["remaining_usd"] < autonomy.min_wake_usd → skip(budget)
   （company.autonomy.max_usd_per_day 设了时，再比较该公司今日已花费，超出 → skip(budget)；都是美元，不计次数）
3. 收集信号（按 kind 选探针，asyncio.gather，每个 wait_for 8s；探针异常 → 写 watch_state.last_error，当作无信号）
     watch     : sec（按 agenda.watch.filings.forms）、earnings（读 agenda.watch.events）、price（excess vs benchmark）、
                 indicator（只限 probe.kind=price|fmp_metric）、pm_question（agenda.open_questions 中 origin=pm 且 status=open 且无 attempts）、
                 internal、再加上"已投递但未被消费且超过 24h"的旧信号（时间退避重投，见 PK.18）
     delta     : consensus（下面的一致预期探针）、sec、internal、price；四者都没有变化 → skip(no_signal)，即"无重要变化不推送"
     move-up / move-down : 价格触发本身就是信号，kind=price_move，ref = '<交易日>:price'（与 watch 的价格探针同 ref，去重）
     v / earn  : 文件对应的 V- 项或财报窗口，ref = 'V-014' / 'FY26Q3:t0'
     earnings-calendar   : calendar 探针（覆盖名单里有 Core 公司在未来 120 天内没有 confirmed 日期，或已有候选日期待确认）→ wake；否则 skip
     director-daily/weekly : 见 PK.19（director 探针）
   每个信号 upsert 进 dalton_signals（UNIQUE(workspace_id,kind,ref)，ON CONFLICT DO NOTHING），已消费或已过期的不再算
4. 过滤：
     - 被 agenda.pm_handoffs(status=waiting) 阻塞的问题和验证项，不算信号 → 全部被挡时 skip(waiting_pm)
     - 处于退避期的（PK.18）→ skip(backoff)
5. 有信号 → wake：把信号写 delivered_at、delivered_execution_id；生成 wake_context
   无信号 → skip(no_signal)
6. 写 dalton_preflight_log（decision、reason_code、reasons、signal_ids、duration_ms）
```

`wake_context` 的样子（就是本轮唤醒分析师的"原因"）：

```xml
<dalton_wake trigger="watch" execution="…" budget_left_usd="11.40">
  <signal id="5b1e…" kind="filing" ref="0001326801-26-000123">8-K（Item 2.02）2026-10-29 16:05 ET：3Q26 业绩新闻稿。</signal>
  <signal id="77ac…" kind="earnings_window" ref="FY26Q3:t0">财报 T0：按 agenda W2 计划先出快讯，再排 T+1 深度点评。</signal>
  <rules>结论的 trigger.ref 填上面的 signal id；收尾按 dalton-agenda 三步；本轮花费建议 ≤ $4。</rules>
</dalton_wake>
```

**(4) 探针接口**：

```python
class Probe(Protocol):
    name: str
    async def scan(self, *, user_id: str, workspace_id: str, company: dict, agenda: dict,
                   state: dict, now: datetime) -> ProbeResult: ...
@dataclass
class ProbeResult:
    signals: list[SignalDraft]           # kind, ref, payload, summary
    cursor: dict | None                  # 新游标；None = 不变
    error: str | None = None
```

| 探针 | 数据来源（后端可直达，不经沙箱） | 说明 |
|---|---|---|
| `agenda` | `dalton_knowledge_versions` 最新有效 agenda | 到期验证（研究总监用）、PM 问题、`kind=date` 的唤醒条件 |
| `sec` | `https://data.sec.gov/submissions/CIK##########.json`（httpx，User-Agent 用 P0D-P1 的 `SEC_USER_AGENT`） | 游标是最新 accessionNumber；按 `watch.filings.forms` 过滤 |
| `consensus` | 后端经 HTTP 直接调 researchdata 桥：`finance(route="estimates", ticker=T)`，URL 为 `DALTON_RESEARCHDATA_URL`（JSON-RPC `tools/call`，与沙箱 wrapper 同一协议；后端可达性由 P0-D.0 验证） | 与工作区最新的 `data/consensus/*.json` 快照（经备份镜像读取）比较 EPS、收入（0q、+1q、0y、+1y）和目标价；变化超过阈值（见下文的阈值文件）才算信号，ref = `<交易日>:consensus`；无论是否唤醒都留当晚快照（见下文）。零 LLM |
| `earnings` | agenda.watch.events 中 `type=earnings`、`date_status=confirmed` 的项 | **只认官方日期**（公司 IR、新闻稿、8-K）；T-3、T0、T+1 窗口。`estimated`（FMP 等日历的候选，显示为"待确认"）不触发财报链 |
| `calendar` | 各公司最新 agenda 的 `watch.events` | 只给 `dalton-earnings-calendar.json` 用：判断这周是否需要刷新官方日期 |
| `price` | 后端行情 `market_data_provider.get_snapshots`（`src/data_client/market_data_provider.py` L524）【待验证 实例获取方式】；默认数据源 yfinance | 个股对 `company.benchmarks[0]` 的超额涨跌幅，超过 `agenda.watch.price.excess_vs_benchmark_pct` 才算信号；ref 为 `<交易日>:price`（与 move 文件同 ref） |
| `indicator` | 同上，或 FMP 指标（可选，配置了 `FMP_API_KEY` 才启用） | 只处理 `probe.kind` 为 price 或 fmp_metric 的领先指标 |
| `internal` | researchdata 桥：`research(route="all", operation="search", query=…, start=<上次检查日>)`，只看"有没有新文档 id" | 按 `agenda.watch.internal.queries`；AlphaEngine 全文有配额，探针只用 search，不读全文；配额用到 80% 时写一条告警日志（不设闸门） |
| `hkex` / `cninfo` | P6 | 本阶段只做美股 |

一致预期探针的阈值不放在 agenda 里，和 PS.11 共用：默认值 `plugins/dalton/skills/dalton-thesis-delta/config/default-thresholds.json`（后端从仓库的 `plugins/` 读取），单个工作区可以用 `research/tracker/thresholds.json` 覆盖（从备份镜像读）。

**一致预期快照**：delta 预检每晚都要留一份快照，**跳过时也写**，写到 `data/consensus/<YYYY-MM-DD_HHMM>.json`（P0-D.6 的格式，`_meta.source='preflight'`）。预检不启动沙箱：沙箱在运行就经 `PTCSandbox.awrite_file_text` 直接写；沙箱停着就把快照正文暂存在 `dalton_watch_state(probe='consensus').cursor.pending_snapshot`，下一轮开始时由 PK.5 的 `apply_pending` 补写（文件名用取数时间，不用补写时间）。

**同一事件只唤醒一次**：自动任务文件和 watch 探针覆盖同一事件时，两边生成**相同的 (kind, ref)**，靠 `dalton_signals` 的唯一约束去重，后到的一方看到信号已投递就 skip(no_signal)。约定：价格类一律 `kind=price_move`、`ref='<交易日>:price'`（move-up、move-down、watch 的超额探针共用）；财报窗口 `kind=earnings_window`、`ref='FY26Q3:t0'`（earn once 文件和 watch 的 earnings 探针共用）；公告 `kind=filing`、`ref=<accession>`（watch 和 delta 共用）；一致预期 `kind=consensus_change`、`ref='<交易日>:consensus'`。

**(5) 多 worker 与成本**：
- 同一条自动任务的同一次 firing 只会被一个 worker 抢到（`FOR UPDATE SKIP LOCKED`）；
- 信号去重靠唯一约束；
- 预检里没有进程内状态；
- 跳过的成本：一行 `automation_executions`、一行 `dalton_preflight_log`、几次 HTTP（含 researchdata 桥的 estimates 调用，宿主机上跑 yfinance，不花模型钱）。按 27 家公司、每 30 分钟轮询、每天 15 小时估算，大约 800 行一天，加上每晚 27 次 delta 预检，可以接受。如果数据量成为问题，再考虑 PK.17 的方案 B。

**验证方式**（真实调用）：
1. 对 META 的 watch 任务点 Manage 里的"立即运行"（`POST /automations/{id}/trigger`）。在没有新公告的时段，执行记录应为 skipped（预检），没有新线程，`dalton_preflight_log.reason_code='no_signal'`，预算模块当日花费不变。
2. 手动把 `dalton_watch_state(META,'sec').cursor.last_accession` 改成上一份 filing 的编号，再触发一次，应唤醒；新线程第一条上下文里有 `<dalton_wake>`；分析师结论的 `trigger.ref` 等于信号 id；`dalton_signals.consumed_at` 有值。
3. 把预算剩余调到低于 `min_wake_usd`，再触发，应得到 `skip(budget)`。
3b. 对 META 的 `dalton-meta-delta.json` 点"立即运行"：一致预期与最新快照相同时 `skip(no_signal)`；手工把最新快照的 FY EPS 改小 2% 再触发，应唤醒，`<dalton_wake>` 里有 `consensus_change` 信号。
4. 在议程里把一个问题设为 waiting_pm，并让唯一的信号指向它，应得到 `skip(waiting_pm)`。
5. 在 `--workers 2` 下重复第 1、2 条各 3 次。

**验收标准**：
- 五条全部复现；
- 跳过时没有建线程、没有 LLM 花费、沙箱没有被启动（computer 状态不变）；
- 单次预检的 p95 < 5 秒（取 `dalton_preflight_log.duration_ms`）。

**风险与注意**：
- **补丁触及上游的运行生命周期文件**（executor、settlement）。改动要尽量小：一个函数调用、一个字段、一条策略。合并上游时重点核对 `execute()` 的开头。
- 预检必须"永不抛异常"。内部异常一律转成 `skip(probe_error)` 并记日志，不能让自动任务因为 Dalton 的代码进入 FAILED。
- `transition_execution` 之前没有 heartbeat 任务。预检限时 20 秒，远小于 `ABANDONED_AFTER_SECONDS=300`，不会被清扫任务误判为已遗弃。

**预估工时**：4 人日（迁移 0.25、预检框架 1.25、sec/earnings/price/consensus/agenda 探针 1.75、钩子和联调 0.75）。

---

### PK.17 事件触发方案的选择（结论：轮询 cron + 预检）

**目标**：把"为什么不扩展 `TriggerType`"写成定论，避免实施时反复。

**前置依赖**：PK.16。

**改动/新增文件**：无。

**具体做法（比较）**：

| 方案 | 改动面 | 多 worker | 成本 | 结论 |
|---|---|---|---|---|
| A. 轮询 cron + 预检（本章） | 新模块 + executor、settlement 的小补丁 | 沿用调度器的行锁 | 空跑 $0，只多一些执行记录 | **采用** |
| B. 新建后台服务 `DaltonWatchService`（像 `PriceMonitorService`），用 advisory lock 选主轮询，有信号再经 `lifecycle.trigger_automation` 触发公司的自动任务 | 新后台服务 + lifespan 注册 | 需要选主锁和续期 | 空跑没有执行记录 | 留作优化，执行记录过多时再做 |
| C. 扩展 `TriggerType` 加 `event` | 迁移改 CHECK（002 的约束）、`models/automation.py`、`file.py` 的触发推断、调度器、前端 `TriggerSection` 和 `CronScheduleBuilder` | 要新写触发源 | 同 B | **不采用**：改动大，和上游冲突多，收益和 B 相同 |

**验收标准**：lead 确认采用 A。

**预估工时**：0。

---

### PK.18 退避：只按时间，不按次数

**目标**：同样的证据、同样的方法反复没有进展时，自动拉长下一次尝试的间隔；有新证据时立即恢复。不设"最多 N 次"。

**前置依赖**：PK.4（attempts）、PK.16。

**改动/新增文件**：新建 `src/dalton/autonomy/backoff.py`，由 `knowledge_append_verdict` 和预检调用。

**具体做法**：

**(1) 证据指纹**：`evidence_fp = sha256(sorted(set(本次 run 的 provenance identifier)) + method)[:16]`。来源记录取自本线程本轮的 `provenance_records`（`get_provenance_for_thread`，`src/server/database/provenance.py` L408）。

**(2) 退避键**：
- 议程问题：`Q-id`；
- 事件类：`(workspace, trigger_kind)`，例如价格异动；
- 写进 agenda 的 `stalled_since` 和 `next_attempt_after`（问题），或 `dalton_watch_state(probe='backoff:<kind>')`（事件类）。

**(3) 规则**：

```
记录一次尝试（progress, evidence_fp）：
  progress ∈ {some, resolved}                    → stalled_since = null；next_attempt_after = null
  progress = none 且 (上次也是 none 且 evidence_fp 相同)
                                                 → stalled_since = stalled_since or 上次时间
                                                   wait = clamp(max(base_hours, 0.5 × (now − stalled_since)), ≤ max_days)
                                                   next_attempt_after = now + wait
  progress = none 但 evidence_fp 不同（换了来源或方法） → 不退避（这是有效的新尝试）
预检：
  信号只关联处于退避期的问题 / 事件类 → 跳过；但如果信号本身是"新证据"（新 filing、新 internal 材料、
  新的领先指标读数），清除对应键的退避，直接唤醒
信号重投：已投递、未被结论消费的信号，下一次可重投时间 = delivered_at + max(24h, 0.5 × (now − first_seen_at))；
  first_seen_at 超过 7 天 → expired（研究总监日报列出）
```

效果：卡住 1 天后间隔约 1 天，卡住 10 天后约 5 天，上限 30 天。全程不计次数（蓝图 §3.2）。

**验证方式**：
1. 对同一个问题用同样的来源做几次无进展尝试（可以用测试脚本直接调 service，并伪造时间），`next_attempt_after` 应随卡住的时长拉长；
2. 新增一份 filing 信号后，预检直接唤醒。

**验收标准**：间隔按公式增长；有新证据时立即清零。

**风险与注意**：`evidence_fp` 只看来源 identifier。分析师对同一来源换了分析方法时，`method` 字段要写清楚，否则会被误判为重复。退避只看时间，`attempts[]` 只是历史记录，不从它的条数推出任何上限。

**预估工时**：1 人日。

---

### PK.19 研究总监：Chief of Staff 的每日调度

**目标**：Home 里的 Chief of Staff 每天读所有议程，在当日剩余额度内按价值和紧迫度排序，派给各公司的分析师，并留下当日计划和覆盖总览。

**前置依赖**：PK.3、PK.15（`dalton_schedule_automation`）、PK.16、PK.18；P0.8 的 `get_budget_today`、`get_run_charge_usd`；现有 `delegate_to_analyst`（`src/tools/secretary/chief_of_staff.py` L86）；owner 已授权的派单自动批准（见下文第 (4) 点）。

**改动/新增文件**：
- 新建 `src/dalton/tools/director_tools.py`：`dalton_coverage_board`、`dalton_record_plan`、`dalton_agenda_note`、`dalton_schedule_automation`（PK.15），只给 `chief_of_staff` 角色
- 新建 `src/dalton/autonomy/board.py`
- 新建 `src/dalton/autonomy/probes/director.py`：研究总监自动任务的预检
- 新建 `plugins/dalton/skills/dalton-research-director/SKILL.md`，以及 `scripts/render_coverage.py`（只用标准库，把看板 JSON 渲染成 HTML，样式沿用 html-report skill）
- 新建 Home 下的自动任务文件 `dalton-director-daily.json`

**具体做法**：

**(1) 自动任务**：

```json
{
  "name": "Dalton 研究总监 · 每日排程",
  "cron_expression": "30 7 * * 1-5",
  "timezone": "Asia/Shanghai",
  "instruction": "运行 dalton-research-director：读看板，按价值与紧迫度在今日额度内排程并派单，记录计划，刷新覆盖总览。",
  "workspace_id": "home",
  "thread": "new"
}
```

时区定为 `Asia/Shanghai`（已决定）；美股公司的 watch 任务按 `America/New_York`。周会任务 `dalton-director-weekly.json` 的星期几由 owner 定（§6.3）。

**(2) 研究总监的预检**（`probes/director.py`，零成本）：满足下面任一条才唤醒，否则 skip(no_signal)，空闲日 $0：
- 有公司存在到期或逾期的 `V-`；
- 有 value=high 的开放问题，且不在退避期、不在等 PM；
- 有超过 24 小时未消费的信号；
- 有接近 TTL 的 `awaiting_pm` 发布请求，或停在 `publishing` 的请求；
- 有财报日期仍为"待确认"、且距候选日期不到 14 天的 Core 公司（提醒核对官方日期）。

**(3) `dalton_coverage_board()`**：从 Postgres 读，返回 JSON，不碰沙箱：

```json
{"date": "2026-10-19", "tz": "Asia/Shanghai",
 "budget": {"cap_usd": "<owner 定>", "spent_usd": 2.1, "remaining_usd": 27.9, "reserve_for_events_usd": 6.0},
 "companies": [{
   "workspace_id": "…", "ticker": "META", "tier": "core", "one_liner": "…",
   "aggregate_status": "intact", "house_view": {"stance": "long", "conviction": "medium"},
   "official_version": 9, "last_verdict": {"id": "VD-20261017-01", "word": "NO_CHANGE", "at": "…"},
   "due_validations": [{"id": "V-014", "due": "2026-10-29", "what": "…", "overdue_days": 0}],
   "open_questions": [{"id": "Q-007", "value": "high", "urgency": "this_week", "backoff_until": null, "waiting_pm": false}],
   "open_signals": [{"id": "…", "kind": "filing", "age_h": 3, "delivered": true}],
   "next_events": [{"type": "earnings", "date": "2026-10-29", "date_status": "confirmed"}],
   "waiting_pm": [{"id": "H2", "what": "…", "since": "…"}],
   "publish_awaiting_pm": [{"request_id": "…", "title": "…", "expires_in_h": 40}],
   "last_research_at": "…", "spend_7d_usd": 9.3}],
 "candidates": [
   {"rank": 1, "ticker": "META", "task": "验证 V-014 前置：3Q26 preview", "kind": "earnings_preview",
    "score": 9.0, "est_usd": 3.5, "why": "T-3 + 高价值 + 本周唯一窗口"}],
 "est_cost_by_kind": {"earnings_preview": 3.5, "event_note": 1.2, "topic": 6.0}}
```

`candidates` 的确定性排序：
- `score = value(high=3, medium=2, low=1) × urgency(逾期=3, 今日或T-3=2.5, 本周=2, 本季=1, 以后=0.5) × staleness`。`staleness` 为 1 加上"距上次研究的天数 / 14"，上限 2。
- 排除处于退避期和等待 PM 的项；
- 按 score 降序贪心，直到累计 `est_usd` 达到 `remaining − reserve`；
- `budget` 各字段直接取自 `get_budget_today`（P0.8）；`next_events` 里 `estimated` 的日期显示为"待确认"；
- `est_cost_by_kind` 取该类任务最近 30 天实际花费的中位数（`get_run_charge_usd`，P0.8），没有历史时用配置的默认值。

研究总监可以基于判断调整顺序，但不能超出额度。超出时工具 `dalton_record_plan` 拒绝保存。

**(4) 派单**：对每个入选项调用现有的 `delegate_to_analyst(question=…, workspace_id=…, report_back=True)`。`question` 要写清任务、相关议程 id 和建议花费，例如：

```
研究总监派单（2026-10-19，计划项 #1）：做 3Q26 业绩前瞻（skill earnings-preview + Dalton 方法）。
关联：V-014、D1、P1/T3。建议花费 ≤ $3.5。收尾按 dalton-agenda 三步，结论 trigger.kind=director，ref=plan:2026-10-19#1。
```

注意：
- **派单自动批准（owner 已授权，必做配置）**：无人值守 run 里，`delegate_to_analyst` 默认会弹确认卡（`dispatch.py` L218），卡片在自动任务 run 中出现会导致 FAILED（`automation_settlement.py` 约 L202-235）。所以上线前在设置 → 自动批准里打开"派单自动批准"（`other_preference.auto_approve_handoffs`，`StandingApprovalMiddleware`；界面是 `web/src/pages/Settings/panels/AutoApproveSettings.tsx`）。只放开派单，不放开建工作区、删除等。
- 并发不另设 Dalton 的个数上限：由现有 `reserve()` 的 Redis 槽位决定（`handoff/reserve.py` L825），拿不到槽位的项留在当日计划的"稍后"列表，等 report-back 回来再派；总量由当日美元额度约束。report-back 会以系统轮次回到 Home 线程（`report_back/handoff/executor.py` L471）。

**(5) `dalton_record_plan(plan)`**：
- upsert `dalton_daily_plans`：同一天重复调用时，旧 plan 移入 `revisions`；
- 写 `Home/dalton/plans/<date>.md`（中文：今日额度、入选、未入选及原因、等待 PM 事项）；
- 研究总监随后运行 `scripts/render_coverage.py board.json > dalton/coverage.html`，刷新覆盖总览。HTML 由确定性脚本生成，不用模型写 HTML，省 token，格式也稳定。

**(6) `dalton_agenda_note(workspace_id, question, value, urgency, why)`**：PM 在 Home 里调整优先级时（例如"这周重点看 AMZN 的 AWS 利润率"），研究总监用这个工具直接往该公司议程追加 `origin=pm` 的问题。走 knowledge service，`source='server'`，reason 写"PM 经研究总监追加"。不用为了改议程专门唤醒分析师花钱。

**(7) 财报日期与每周附加任务**：
- **财报日期不归研究总监刷新**：由 PS.7 的 `dalton-earnings-calendar.json`（每周六 09:00，Asia/Shanghai）负责。规则两边一致：只有公司官方来源（IR 网站、新闻稿、8-K）给出的日期才写成 `date_status: confirmed`；FMP 等日历的日期只作候选，写成 `estimated`，在 agenda.md、看板和 coverage.html 里显示为"待确认"，不触发财报链；确认后由那一轮写 T-3、T0、T+1 的 once 文件（`dalton-<t>-earn-<period>-<k>.json`）。研究总监只在日报里列出"待确认且临近"的公司；
- 每天清理 14 天前已完成的 once 文件；
- 把 `scope=fund` 的 lessons 汇总进用户层记忆（PK.6）；
- 列出超过 7 天未消费、已过期的信号和超期的发布请求。

**验证方式**（真实调用）：
1. 先确认派单自动批准已打开。手动触发 `dalton-director-daily`。Home 新线程里依次看到 `dalton_coverage_board` → 若干 `delegate_to_analyst`（没有确认卡）→ `dalton_record_plan` → Bash 运行 render 脚本。各公司工作区出现派单线程，完成后在 Home 收到 report-back。
2. 把所有公司的议程清空（没有到期项、没有信号），再触发，应被预检跳过，花费 $0。
3. 把当日额度设得很低，看板的 `candidates` 应只剩一项，计划里写明其余项"超出额度"。

**验收标准**：
- `Home/dalton/plans/<date>.md` 和 `dalton/coverage.html` 存在，FilePanel 能打开（HTML 用 `HtmlViewer`）；
- 当日计划的 `planned_usd` ≤ `remaining − reserve`；
- 派出的每个任务最终都在对应公司留下 `trigger.kind=director` 的结论。

**风险与注意**：
- 研究总监本身也花钱（读看板、写派单）。目标是每天 ≤ $0.5，`dalton_coverage_board` 的输出要尽量紧凑；
- 不要让研究总监自己做研究。Chief of Staff 角色本来就没有 equity-analyst 子智能体（根 AGENTS.md），skill 里再写一遍。

**预估工时**：3 人日。

---

### PK.20 交给 PM 的事项不在后台空转 + 一周自主运行验收

**目标**：凡是等 PM 决定的事，后台一律不再为它花钱；用一周不派任务的真实运行证明"在自主研究"。

**前置依赖**：PK.14–PK.19；发布治理（`awaiting_pm`）。

**改动/新增文件**：
- 新建 `src/dalton/autonomy/report.py`，以及 PK.13 中 `GET /api/v1/dalton/autonomy/report`
- 新建 `scripts/dalton/autonomy_week_report.py`：调用该接口，输出 markdown 写到 Home 的 `dalton/reports/<week>.md`

**具体做法**：

"不空转"由四处共同保证：
1. **议程**：`pm_handoffs(status=waiting)` 所阻塞的 Q 和 V 不算信号（PK.16 第 4 步），也不进研究总监的候选（PK.19）；
2. **发布**：`awaiting_pm` 的请求不会被后台重跑核验和审稿（PK.11 的状态机）；
3. **派单**：研究总监不对处于 `waiting_pm` 的问题派单；
4. **解除**：PM 在任何线程里回答后，分析师用 `knowledge_update` 把对应 handoff 标为 `answered`，问题恢复 `open`；发布审批则由 `publish_report` 自动标记。

周报指标（SQL 放在 `src/dalton/database/plans.py`）：

```sql
-- 每家公司本周"由事件或议程触发、且有下一项验证"的结论数
SELECT w.workspace_id, w.name,
       COUNT(v.*) FILTER (WHERE v.trigger_kind <> 'manual' AND v.origin_type IN ('automation','agent')
                           AND v.next_due IS NOT NULL) AS autonomous_verdicts,
       jsonb_object_agg(COALESCE(v.word,'-'), 1) AS words_seen
FROM workspaces w LEFT JOIN dalton_verdicts v
  ON v.workspace_id = w.workspace_id AND v.verdict_at >= %(week_start)s AND v.verdict_at < %(week_end)s
WHERE w.user_id = %(user_id)s AND w.workspace_id IN (SELECT workspace_id FROM dalton_knowledge_versions WHERE doc_key='company')
GROUP BY w.workspace_id, w.name;
-- 每日花费 vs 额度：取自 P0.8 的 dalton_budget_reservations 与 get_budget_today；空闲日 = 当日 dalton_preflight_log 中没有任何 wake
```

**验证方式**：P5 的正式验收周。
- 周一 08:00 起不派任何任务，只允许 PM 回答分析师和研究总监主动提出的问题；
- 周日运行 `autonomy_week_report.py`。

**验收标准**（lead 验收时逐条检查）：
1. 每家覆盖公司 `autonomous_verdicts ≥ 1`，并且每条都有 `next_validation`（看周报表格）；
2. 每一天的花费 ≤ 当日额度（`GET /api/v1/dalton/budget/today` 或预算卡截图；超额不超过 P0.8 第 5 节的上界）；
3. 空闲日（研究总监和所有 watch 都被预检跳过的日子）的模型花费 ≤ $0.05；
4. 没有一条 `waiting_pm` 的事项在等待期间产生过新的 run（SQL：`dalton_preflight_log` 里 wake 的 `signal_ids`，不关联任何 waiting 的项）；
5. 抽查 5 条结论：五词结论和理由站得住，证据可点开（结论 → 证据 id → thesis.md 的证据台账 → 来源）；
6. 五词分布不是 100% NO_CHANGE；如果是，周报里要有解释（例如"本周确实无事"），并附预检日志佐证。

**风险与注意**：一周只覆盖 4 家公司时，第 1 条很容易满足。扩展到 Core 27 家时，主要瓶颈在额度，研究总监的排序质量需要 PM 抽评。

**预估工时**：1 人日，外加 1 周真实运行。

---

## 4. 界面（PK-UI.1 – PK-UI.6）

原则（owner 2026-10-18 澄清）：**直接复用 LangAlpha 现有界面，前几个阶段不新建页面。**
- 实时过程本来就看得到；
- 公司知识层是普通工作区文件，FilePanel 原生就能预览（md、html、pdf、xlsx）；
- 一家覆盖公司就是一个工作区，原生的工作区列表就是覆盖名单；
- 跨公司总览是研究总监每天在 Home 重新生成的 HTML 文件；
- 风格遵守 `DESIGN.md`（Quiet Workspace）。

### PK-UI.1 原生就有的可见性（盘点，无需开发）

| 想看什么 | 在哪里看 | 组件或路径 |
|---|---|---|
| Dalton 现在在做什么、每一步工具调用 | 线程的对话视图：步骤和工具卡，`knowledge_update`、`publish_report` 的参数可见 | `web/src/pages/ChatAgent/`（`components/messageList/*`） |
| 子智能体并行取证 | 子智能体卡片，可点开看过程 | `web/src/pages/ChatAgent/components/PTCAgentCard.tsx` |
| 这个回答用了哪些来源 | FilePanel 的 sources 标签：本轮和全线程的来源，可打开原文或文件 | `components/SourcesPanel.tsx`（挂在 `filePanel/ActiveTabBody.tsx` L18/L157） |
| 公司认知（thesis、debate 等） | FilePanel 文件树：`thesis.md`、`debate.md`、`drivers.md`、`management.md`、`agenda.md`、`verdicts.md` | `components/filePanel/FileViewer.tsx`（markdown 分支） |
| 财务模型 | FilePanel 打开 `model/*.xlsx`：ExcelViewer 显示公式和数值，支持 `#Sheet!A1` 定位 | `components/viewers/ExcelViewer` |
| 已发布成果 | `published/INDEX.md` → 各篇 md 或 pdf | 同上（PdfViewer） |
| 历史版本 | `history/<doc>/` 下按日期排列的 JSON 副本 | 同上（JSON 只有语法高亮） |
| 自主运行记录 | Automations → Feed（每次唤醒，含跳过的）/ Manage（所有 `dalton-*` 任务） | `web/src/pages/Automations/components/{FeedView,ManageView}.tsx` |
| 研究总监、跨公司问答 | 侧栏 "All workspaces"（Home），Chief of Staff 线程；`Home/dalton/coverage.html`、`dalton/plans/` | Home 工作区 |
| 覆盖名单 | 侧栏工作区列表、工作区总览（`sidebar.workspaceGallery`） | `components/Sidebar/AppSidebar.tsx` |

**验收**：PK-UI.6 的浏览器走查。

### PK-UI.2 隐藏用不上的入口：交易、下单、行情、券商

**目标**：PM 打开应用只看到研究相关的入口。

**前置依赖**：无。

**改动/新增文件**：
- 补丁 `web/src/components/nav/navItems.ts`：`NavItem.requires` 从 `'orders'` 扩展为 `'orders' | 'market'`；`/market` 加 `requires: 'market'`
- 补丁 `web/src/components/nav/useNavItems.ts`：`'market'` 读构建变量 `VITE_DALTON_MODE`（新增，默认不设）；设了就隐藏
- 补丁 `web/src/components/Main/Main.tsx` L99：`VITE_DALTON_MODE` 时兜底路由改为 `/chat`（落到 Home）
- `/dashboard` 在 Dalton 模式下也隐藏（已决定：仪表盘以行情、自选股、新闻为主）：`NavItem.requires` 再加 `'dashboard'`，同样读 `VITE_DALTON_MODE`
- 配置：`market_watch` 功能已经是 opt-in（`src/config/features.py` L63-80），保持关闭即可；`/orders` 在没有连接券商时已经自动隐藏（`useNavItems.ts` 的 `useOrdersVisible`），不改
- 【待验证】设置页里的 `TradingPermissionSection.tsx`、`ConnectedAccounts.tsx` 是否需要在 Dalton 模式下隐藏；如需隐藏，同样用 `VITE_DALTON_MODE` 判断
- `web/src/components/nav/__tests__/useNavItems.test.tsx` 补一条 market 用例

**具体做法**：
- 只做"不画入口"，路由仍然可以通过 URL 访问（`navItems.ts` 注释里写明的约定：门槛不是路由守卫）；
- 后端不需要改。

**验证方式**：`VITE_DALTON_MODE=1 pnpm dev`，侧栏和移动端底部标签栏都没有行情、下单和仪表盘。

**验收标准**：
- 三处导航（rail、移动端标签栏、账户菜单）一致；
- `pnpm typecheck`、`pnpm test` 通过。

**风险与注意**：这是构建期开关，改了要重新构建前端。

**预估工时**：0.5 人日。

### PK-UI.3 发布审批卡

**目标**：PM 在线程里看到一张清楚的审批卡，批准或拒绝，并且可以写理由。

**前置依赖**：PK.11。

**改动/新增文件**：
- 第一期：**零改动**。interrupt 带 `attempt_id` 后，前端判为工具审批（`session/interrupts/toolApprovalCard.ts` L19-32），渲染 `ToolApprovalCard`：参数表（`ArgsTable`）显示 `args` 里的中文键值对，有批准、拒绝、拒绝理由；裁决按 attempt 回传 `order_decisions`（`answerBoard.ts` L44-52）。历史回放走同样的分支（`fromHistoryEvent.ts`）。
- 第二期（可选，小补丁）：在 `web/src/pages/ChatAgent/components/ToolApprovalCard.tsx` 中，参照 `data.order → OrderApprovalCard` 的分流方式，加 `data.toolName === 'publish_report'` → 新组件 `components/dalton/PublishApprovalCard.tsx`。卡片按"发布凭据"的样子画：标题、公司、核验结果、审稿模型与结论、将写入的文件、thesis 版本变化。i18n 键 `dalton.publish.*` 同时加进 `locales/zh-CN.json` 和 `locales/en-US.json`（`locales/__tests__/keys.test.ts` 要求两边一致）。

**验证方式**：PK.11 的验证 1、2、6。

**验收标准**：
- 卡片标题和参数都是中文，能看懂这次批准会产生什么文件；
- 刷新页面后卡片状态正确（待批、已批、已拒）；
- 已批的卡片收起成 `SettledStep` 状态行。

**风险与注意**：
- 不要让 `args` 里出现 `order` 键，否则会被分流到下单卡片；
- 第一期的标题文案可能拼成"批准 publish report"一类的生硬说法【待验证】，介意的话直接做第二期补丁（约 0.5 人日）。

**预估工时**：第一期 0；第二期 0.5 人日。

### PK-UI.4 中文

**目标**：界面和交付默认中文。

**具体做法**：
- 界面语言：`locale` cookie 设为 `zh-CN`（设置 → 偏好），LangAlpha 已经有完整的中文语言包（`web/src/locales/zh-CN.json`）；
- Dalton 自己产出的文字（渲染出的 md、工具返回、卡片参数）都由本章代码直接写中文；
- 术语保留英文，如 thesis、debate、conviction、house view。

**验收标准**：PK-UI.6 走查中没有未翻译的键名（例如显示成 `sidebar.xxx`）。

**预估工时**：0（文案已含在各步骤里）。

### PK-UI.5 跨公司总览与已发布成果的阅读体验（文件即界面）

**目标**：不建页面，也能一眼看到"覆盖了谁、各自什么状态、今天做了什么、有什么等我批"。

**前置依赖**：PK.19、PK.11。

**具体做法**：
- `Home/dalton/coverage.html`（研究总监每天由 `render_coverage.py` 生成，在 FilePanel 里由 `HtmlViewer` 打开）。内容：
  - 每家公司一行：一句话判断、thesis 状态（confirm / warn / break 用文字加颜色点，配色取自 DESIGN.md 的状态词汇）、最近结论和日期、下一个催化剂、最近发布；
  - 页面顶部：今日研究活动、花费 / 额度；
  - "等你处理"：待批发布（附线程链接）、等待 PM 的事项。
  - 链接用工作区相对路径（`../META/thesis.md`）。【待验证】HtmlViewer 里相对链接能否跳转到兄弟工作区文件（`agentPaths.ts` 的 `siblingWorkspacePath` 规则）；跳不了就退回成"文件路径文本加线程 id"。
- `published/<date>-<slug>.md` 文首自动插入发布信息块：审批人、批准时间、审稿模型与结论、publication_id、核验摘要（"142 个数字全部有来源"）。脚注就是来源；PDF 是同一内容的版式版。
- `published/INDEX.md`：按日期倒序列出全部发布，含类型、标题、thesis 版本。

**验收标准**：PK-UI.6 第 4、5 条。

**预估工时**：0.5 人日（`render_coverage.py` 和信息块模板）。

### PK-UI.6 浏览器验收清单（lead 走查用，真实数据截图）

1. 打开应用：侧栏只有对话、自动化、插件，没有行情、下单和仪表盘；界面是中文。
2. 侧栏工作区列表就是覆盖名单（`META`、`AMZN` 等，名字就是 ticker），描述里有公司名、Core 或 Watch 和一句话判断。
3. 进入 META：FilePanel 文件树和 PK.1 一致。依次打开 thesis.md、debate.md、drivers.md、management.md、agenda.md、verdicts.md，都是可读的中文表格；`model/` 下的 xlsx 能看到公式。
4. 打开 Home 的 `dalton/coverage.html`：4 家公司的状态、最近结论、下一个催化剂、今日花费和额度、待批事项都有，和数据库一致（抽查 2 家）。
5. 打开一篇 `published/*.md`：文首有审批人、审稿模型和结论；点脚注能对上证据；同目录有 pdf。
6. Automations → Feed：能看到 watch 任务的执行记录，跳过的显示"预检跳过"；Manage 里所有 `dalton-*` 任务都在。
7. 打开一次唤醒线程：第一条上下文能看到 `<dalton_wake>` 的原因；过程中有工具卡和子智能体卡；结尾有 `knowledge_append_verdict`。
8. 在一个交互线程里完成一次发布：审批卡 → 批准 → `published/` 出现文件 → thesis.md 的"正式版本"更新。
9. 打开 `history/thesis/`，能看到按日期排列的版本副本；在 FilePanel 里手改一次 `debate.md` 后，下一轮能在 `history/debate/` 找到 `-edited.md` 副本，agent.md 托管块有提示行。

### 以后再做（仅在 owner 要求时）

- 可点击的版本对比页（数据由 PK.13 的 `/knowledge/.../diff` 提供；可以做成 FilePanel 的一个新标签：扩展 `filePanel/useFileTabs.ts` 的 `TabBody` 联合类型，再给 `ActiveTabBody.tsx` 加一个 case，编译器会强制补全）。
- `thesis.json` 专用查看器（在 `FileViewer.tsx` 代码回退分支之前加一个按文件名判断的分支，并同步修改 `fileMeta.ts` 的 `viewerFor`）。
- 覆盖总览页 `/coverage`（在 `components/Main/Main.tsx` 的路由表中新增），读 `/api/v1/dalton/coverage/board`。
- conviction call 卡片。

---

## 5. 上游文件补丁清单（合并上游时重点核对）

每处都是钩子级改动，带 `# DALTON-HOOK(<id>)`（TS 用 `// DALTON-HOOK(<id>)`）标记。P0 的 `scripts/dalton/check_hooks.sh` 校验的是 **P0 附录 A 与本表的并集**：多出或缺少都失败退出。路由注册（`DALTON-HOOK(router)`）和配置读取（`src/dalton/settings.py`）已由 P0 提供，本章不再改 `setup.py` 和 `src/config/settings.py`。

| # | 钩子 id | 文件 | 改动 | 所属步骤 |
|---|---|---|---|---|
| 1 | `dalton-tools` | `src/ptc_agent/agent/agent.py`（约 L1000） | `tools = [*tools, *build_dalton_tools(role, filesystem_backend, enabled=…)]`，约 2 行 | PK.4 / PK.11 / PK.19 |
| 2 | `knowledge-reconcile-mw` | `src/ptc_agent/agent/agent.py`（主智能体专属中间件） | 插入 `DaltonReconcileMiddleware`（abefore_agent，调用 `apply_pending`），约 2 行 | PK.5 |
| 3 | `knowledge-reconcile` | `src/server/handlers/chat/ptc_run.py`（L926 之后） | `asyncio.create_task(detect_direct_edits(...))`，约 3 行 | PK.5 |
| 4 | `preflight` | `src/server/services/automation_executor.py`（`execute()` L812 之前；`_Firing`；`_run_turn` L715 附近） | 预检调用、`wake_context` 字段、注入 directive，约 10 行 | PK.16 |
| 5 | `precheck-outcome` | `src/server/services/automation_settlement.py`（`Outcome`、`_POLICIES`） | `PRECHECK_SKIPPED`，约 2 行 | PK.16 |
| 6 | `precheck-skip-reason` | `src/server/models/automation.py`（L591） | `SkipReason` 加 `"precheck"`，1 行 | PK.16 |
| 7 | `precheck-hidden` | `src/server/database/automation.py`（L56-62） | 隐藏 `precheck` 跳过，1 行 | PK.16 |
| 8 | `dalton-nav` | `web/src/components/nav/{navItems,useNavItems}.ts`、`components/Main/Main.tsx` | `VITE_DALTON_MODE` 隐藏行情、仪表盘 | PK-UI.2 |
| 9 | `publish-card`（可选） | `web/src/pages/ChatAgent/components/ToolApprovalCard.tsx` | 分流到 `PublishApprovalCard` | PK-UI.3 |
| 10 | `precheck-label`（可选） | `web/src/pages/Automations/utils/status.ts` | `precheck` 文案 | PK.16 |

与 v0.1 相比，删掉了拦截写入的 `DaltonKnowledgeGuardMiddleware` 钩子、`setup.py` 路由钩子和 `src/config/settings.py` 访问器钩子。其余全部是新文件。

## 6. 工时汇总、风险、开放问题

### 6.1 工时（人日）

| 模块 | 步骤 | 工时 |
|---|---|---|
| 知识层 | PK.1 0.5 · PK.2 1.25 · PK.3 0.75 · PK.4 2.5 · PK.5 1 · PK.6 1 · PK.7 1 | 8 |
| 发布治理 | PK.8 0.75 · PK.9 4 · PK.10 2 · PK.11 3.5 · PK.12 0.5 · PK.13 1 | 11.75 |
| 自主议程 | PK.14 0.5 · PK.15 0.75 · PK.16 4 · PK.17 0 · PK.18 1 · PK.19 3 · PK.20 1 | 10.25 |
| 界面 | PK-UI.2 0.5 · PK-UI.3 0–0.5 · PK-UI.5 0.5 | 1–1.5 |
| 合计 | | **约 31 人日**（v0.1 约 44），另加 1 周自主运行验收 |

减少的主要来源：去掉写入拦截和 DRIFT 还原（PK.5 −2）；不变量只留三条（PK.4 −1.5）；PK.7 只提供接口、提取归 P1.4（−1）；不建外键和级联处理（PK.3、PK.8 −0.5）；去掉单独的 director 派单上限、周一日历刷新（PK.19 −1）；其余各步随之收缩。

建议顺序：
1. PK.1–PK.6（P1 入职之前要有）；
2. PK.7（P1，与 P0D-P1 的 P1.4 联调）；
3. PK.8–PK.12（P3，与 P0D-P1 §P0-D.3b 的逐文档来源记录同期）；
4. PK.14–PK.16、PK.18（P4）；
5. PK.19、PK.20（P5）；
6. PK.13 和 PK-UI 穿插进行。

### 6.2 主要风险

| 风险 | 缓解 |
|---|---|
| 对 executor、settlement 打补丁，和上游的运行生命周期改动冲突 | 补丁压到最小（§5 的 #4–#7），每次合并上游后跑 PK.16 的验证 1、2 |
| 数字核验误报太多，分析师疲于补脚注 | 第一版以"脚注约定 + 证据台账 + facts 底表"为主，自动搜索为辅；别名表可追加；PK.9 的验收要求在真实报告上测误报率；草稿期从不阻止 |
| 放开直接修改后，知识文件结构被写坏 | 版本表照常记录；渲染、预检、研究总监只读最新一份 `schema_valid=true` 的版本；托管块和工具 note 提示分析师合入 |
| 审稿模型凭证缺失，导致长期无法发布 | `review_unavailable` 立即写入 pm_handoffs，研究总监日报列出；`reviewer_models` 配多个不同家族的候选 |
| 结论偷懒（全是 NO_CHANGE） | 周报统计五词分布，PM 抽评；skill 里写明判断标准 |
| 研究总监派单开了自动批准，权限放宽 | 只放开派单（`auto_approve_handoffs`），不放开建工作区、删除等 |
| 知识层版本表增长 | TOAST 压缩；单文档超过 1MiB 时警告，二期做证据滚动归档 |

### 6.3 开放问题（只剩 owner 的 6 项，其余均已决定）

| # | 事项 | 当前默认 |
|---|---|---|
| Q1 | 日预算金额（P0.8 的 `daily_cap_usd`） | 待 owner 填；时区已定 `Asia/Shanghai`，与旧 Dalton 分开 |
| Q2 | STRL 的官方定义 | PS 暂用 M/D/E/C/F/S 替代口径 |
| Q3 | P2 样板公司 | 建议 DASH，备选 AFRM |
| Q4 | 仓位建议的百分比和门槛收益率 | 门槛收益率默认 15% |
| Q5 | 每周复盘日（`dalton-director-weekly.json` 的星期几） | 待 owner 定 |
| Q6 | 净现金口径确认 | 采用 SKILL.md 版本 |

已决定、不再讨论的事项（供实施者查阅）：时区 `Asia/Shanghai`；派单自动批准打开；分析师可自主读写 `dalton-*` 自动任务；布局按 PK.1；工作区名 = ticker；有正式发布的工作区可以删除，Dalton 记录作为档案保留；Dalton 模式隐藏仪表盘；审稿家族用 P0.6 的 `family_of`；预算接口用 P0.8 的 `get_budget_today` / `get_run_charge_usd`；迁移走 Dalton 独立链 `d003`/`d005`/`d006`。
