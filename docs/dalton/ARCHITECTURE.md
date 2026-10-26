# Dalton 技术文档

本文描述 Dalton **当前**的实现，面向接手开发和运维的工程师。各阶段的设计和验收历史见 `docs/dalton/dev-guide/`。那些文件按阶段编写，部分内容已经被后来的实现取代；如果和本文冲突，以本文和代码为准。

## 1. 定位与边界

Dalton 是构建在开源项目 LangAlpha 之上的基金研究分析师。LangAlpha 提供：智能体运行时（主智能体、子智能体、沙箱代码执行）、对话和流式传输、工作区和文件、自动任务调度，以及 Web 界面。Dalton 在此基础上增加了以下能力：

- 基金方法论、23 个研究 skill 与 6 个多步 workflow（带完成契约门禁 `dalton_check_done` 与断点续跑）；
- 每位用户独立的覆盖名单与动态筛选规则组（由 Chief of Staff 管理），以及每用户独立的财报日历；
- 每家公司的结构化知识层，带乐观锁版本号、JSON Schema 校验与直接编辑调和；
- 正式发布治理：数字核验、两轮语言审查（只标不改）、异族模型独立审稿、PM 审批，以及发布时由智能体渲染 HTML；
- 自主研究：预检与探针唤醒、股价异动按交易日单向触发、研究总监排期，以及自动化结果经飞书卡片 2.0 / Discord 私信推送；
- 数据接入：researchdata 桥接（MCP 令牌与管理令牌分离）、公司 Wiki 入库与飞书文档读写、SEC XBRL、`yfinance` 优先与 FMP 共享额度熔断、盘前盘后行情标注、网页、X、团队对话归档；
- 模型接入与计费：订阅制 OAuth、原生 GLM、原生订阅模型巡检/同步/退役、OpenClaw broker，以及每日现金额度；
- 团队模式与安全边界：邮箱白名单与 PM 单次邀请链接注册（用户名 + 密码）、三层身份（`admin` / `internal` / `external`）与外部成员完全隔离、共享模型凭据、按通道（Web UI 与 Chief of Staff）配置的工作区共享权限；个人工作区内的全局搜索（⌘K）、输入框草稿持久化、文件与 Memo 面板批量上传、后台子代理并发上限与排队；隐藏交易与券商连接功能（保留仪表盘「持仓」视图），后端拒绝券商开关与交易权限变更且不向智能体绑定任何下单工具。

**代码归属。** Dalton 自有代码只放在 `scripts/dalton/dalton_paths.txt` 列出的路径下，主要有：

- `src/dalton/`、`plugins/dalton/`、`plugins/dalton_data/`、`workflows/`、`scripts/dalton/`、`config/dalton/`、`deploy/dalton/`、`docs/dalton/`；
- `tests/*/dalton/`；
- `config.dalton.yaml`、`agent_config.dalton.yaml`、`docker-compose.dalton.yml`；
- 少量 `web/src/...dalton...` 文件。

除此以外都是上游 LangAlpha 的代码，只允许在「挂钩点」修改，见第 11 节。

## 2. 总体结构

```
宿主机（macOS）
  ├─ OpenClaw：模型 broker（ocb-*）、researchdata MCP、xreach、web_search / x_search 网关、飞书与 Discord 发送通道
  ├─ researchdata 桥接  127.0.0.1:8960   scripts/dalton/researchdata_bridge.py（launchd: com.dalton.researchdata-bridge）
  │    ├─ MCP 接口：POST /mcp（_is_valid_mcp_token：校验 DALTON_BRIDGE_TOKEN 或 DALTON_BRIDGE_ADMIN_TOKEN）
  │    └─ 管理接口：POST /embed、POST /wiki/ingest、POST /feishu/*、GET|POST /jobs/conversation-archive*、GET /jobs/ops、POST /notify（_require_admin_token：仅校验 DALTON_BRIDGE_ADMIN_TOKEN）
  ├─ web_search 桥接     127.0.0.1:8961   （launchd: com.dalton.web-search-bridge）
  └─ 对话归档（每天 21:15） conversation_archive.py run（launchd: com.dalton.conversation-archive）

Docker（colima，数据盘在 EveSSD）
  ├─ backend   FastAPI + LangGraph，127.0.0.1:8000；挂载 src、scripts、plugins、mcp_servers、migrations、libs/market-protocol、workflows、config/dalton、var/dalton
  ├─ postgres  dalton-postgres:18-pgvector
  ├─ redis
  ├─ frontend  127.0.0.1:5173（开发 Vite 服务）
  ├─ edge      Caddy 127.0.0.1:8088（deploy/dalton/Caddyfile）：仅将 /api/*、/ws/* 反代到 backend:8000；/health、/docs、/redoc、/metrics 等后端根路径返回 404；其余 GET/HEAD 请求由 /srv（挂载生产包 web/dist）静态提供并做 SPA 回退；公网经 Tailscale Funnel :8443 转发到 edge :8088
  └─ sandbox   智能体执行代码的容器（所有工作区按用户共用各自的 "computer"）
```

## 3. 智能体与角色

- **角色**由上游 `turn_route`（`src/server/services/turn_runtime.py`）按工作区决定：
  - Home 工作区是 **Chief of Staff**，兼任研究总监；
  - 公司工作区是 **analyst**。
- **工具绑定**（`src/dalton/tools/__init__.py` 的 `build_dalton_tools`，钩子 `dalton-tools`）：

  | 角色 | 绑定的工具 |
  |---|---|
  | Chief of Staff（Home） | `dalton_coverage`（管理每用户覆盖名单与动态规则组）、`dalton_coverage_board`、`dalton_record_plan`、`dalton_agenda_note`、`dalton_watch_event`、`dalton_resolve_signals`、`dalton_schedule_automation`、`dalton_coverage_tracking`、`dalton_system_jobs`、`conversation_search`、`check_language`（`allow_home_workspace=True`，省略工作区或传 `"home"` 时由 `resolve_caller_home_workspace_id` 解析到调用方 Home 工作区）、`dalton_check_done`（限于 `weekly-coverage-review` 与 `dalton-earnings-calendar`）、`knowledge_update` 与 `knowledge_append_verdict`（跨工作区向 `cos_access='rw'` 的同事工作区写入知识层）、`ingest_company_wiki`（`allow_home_workspace=True`）、`feishu_read`、`feishu_list_wiki`、`feishu_create_doc`、`feishu_append`，以及上游协调工具 `delegate_to_analyst`、`manage_workspaces`、`manage_threads`、`agent_output` |
  | 公司分析师（公司工作区） | `knowledge_update`、`knowledge_append_verdict`、`dalton_check_done`、`check_draft`、`check_language`、`publish_report`、`ingest_company_wiki`、`feishu_read`、`feishu_list_wiki`、`feishu_create_doc`、`feishu_append`、`dalton_schedule_automation`、`dalton_coverage_tracking`、`dalton_system_jobs`（范围锁定在当前公司工作区）、`conversation_search` |
  | 子智能体（`general-purpose`） | 在上游工具外额外绑定 `check_language`（`src/ptc_agent/agent/agent.py`，钩子 `dalton-tools`） |

- **禁用下单与券商连接**（`DALTON-HOOK(hide-trading)`）：
  - 前端隐藏交易模式开关与券商连接入口，保留仪表盘「持仓」页签；
  - `GET /api/v1/mcp/brokerages` 固定返回空列表，`PATCH /api/v1/mcp/brokerages/{name}/enabled` 返回 HTTP 403（`src/server/app/mcp_brokerages.py`）；`GET /api/v1/users/me/trading-permission` 固定返回 `no_trading`，`PUT /api/v1/users/me/trading-permission` 返回 HTTP 403（`src/server/app/trading_permission.py`）；
  - `bindable_direct_tools`（`src/ptc_agent/agent/middleware/direct_mcp.py`）与 `_ORDER_TOOLS`（`src/server/services/tool_binding.py`）按 `_DEFAULT_ORDER_TOOL_NAMES`（`trading_order_place`、`trading_order_replace`、`trading_order_cancel`、`place_equity_order` 等）过滤全部下单与撤单工具，确保下单工具不会绑定到智能体。
- **每用户覆盖名单与动态规则组**（`src/dalton/coverage/models.py`、`src/dalton/coverage/service.py`、`src/dalton/tools/coverage_tools.py`，迁移 `src/dalton/migrations/versions/d015_user_coverage.py`）：
  - 数据存放在 `dalton_coverage` 和 `dalton_coverage_groups`（`group_id`、`user_id`、`label`、`criteria`、`default_tier`、`auto_refresh`、`last_resolved_at`）。`dalton_coverage` 表主键为 `(user_id, ticker)`，数据库列包含专用字段 `ticker`、`name`、`tier`、`market`、`peer_group`、`peers`、`aliases`、`earnings`（`DEDICATED_COMPANY_KEYS`）、来源与分组列 `source`、`group_id`、`added_at`、`updated_at`，以及 JSONB 扩展列 `extra`；渲染到 `Home/dalton/coverage.yaml` 的其余字段（`sheet_section`、`etfs`、`workspace`、`coverage_stage`、`note`）以及规则组归属状态（`group_ids`、`group_stale_ids`）均存放在 `extra` 中，由 `company_to_row` 与 `row_to_company` 无损转换。
  - 单股校验与补全（`validate_and_enrich_ticker`、`make_new_company_dict`）：优先通过 `yfinance`（`_lookup_via_yfinance`）核验股票代码并补全公司名、市场、行业（写入 `peer_group`）、交易所与市值，再用 FMP profile（`_lookup_via_fmp_profile`）补充缺失字段（新建条目的 `peers` 与 `etfs` 均初始化为空列表 `[]`）；数据源不可用时抛 `MarketDataUnavailable`，查无此股时放入 `not_found` 且拒绝入库。
  - 动态规则组（`preview_coverage_group`、`add_coverage_group`、`remove_coverage_group`、`resolve_coverage_group`）：用 `yfinance.screener`（`EquityQuery`）按市值、交易所、行业等条件筛选并过滤非主板股票（`_filter_mainboard_screener_rows`），失败时回退 FMP screener。Chief of Staff 先调 `preview_group` 将候选列表展示给用户确认，确认后再调 `add_group` 落库。`calendar` 预检时通过 `refresh_due_coverage_groups_for_preflight` 自动重算距上次解析超过 85 天（`GROUP_REFRESH_INTERVAL_DAYS = 85`，即按季度）且 `auto_refresh=True` 的规则组（零 token）；重算时不再满足条件的既有标的不会自动删除，而是将该 `group_id` 记入 `extra.group_stale_ids`（汇总时标为 `group_stale=True`）。
  - 每次变更覆盖名单后，调用 `sync_user_coverage_to_home` 将当前用户名单渲染为沙箱内的 `Home/dalton/coverage.yaml`，并在名单非空时通过 `ensure_user_earnings_calendar_automation` 幂等创建该用户的 `dalton-earnings-calendar` 自动任务。修改公司 `tier`（`Core` / `Watch`）时，通过 `set_tier_and_resync`（`src/dalton/autonomy/tracking.py`）同步重写该公司的 `dalton-<t>-delta` 排期。
- **新用户默认与防重复建工作区**：
  - 新用户首次创建时（`create_user` in `src/server/database/user.py`），默认时区设为 `Asia/Hong_Kong`（`DEFAULT_USER_TIMEZONE`），`other_preference` 默认写入 `{"auto_approve_handoffs": True}`（`DEFAULT_OTHER_PREFERENCE`，仅在 `INSERT` 时写入，不覆盖已有用户的偏好）；`users.locale` 为空时由 `with_default_locale`（`src/dalton/user_locale.py`）默认填充 `zh-CN`。
  - 智能体（Chief of Staff）经审批路径创建工作区时，`duplicate_workspace` 与 `is_duplicate_name`（均位于 `src/tools/secretary/approvals.py`，调用 `src/server/database/workspace.py` 的 `get_live_workspace_names`）做大小写无关重名检查，并对形如股票代码的名称通过 `normalize_symbol`（`src/server/models/user.py`）做代码归一比对（`700.HK` 与 `0700.HK`、`600519.SS` 与 `600519.SH` 视为同一代码）。发现同名工作区或数据库检查失败时，`_standing_create_withheld` 扣留自动批准并在审批卡片与拒绝提示中返回已有 `workspace_id` 供直接复用。用户在侧栏手动新建工作区不走这条路径，只做文件夹名规范化与大小写折叠（`src/server/database/workspace_names.py`），不做股票代码归一。
- **各角色用哪个模型**：在 `agent_config.dalton.yaml`、`src/dalton/dalton.yaml` 和子代理定义（`plugins/dalton/agents/*.md`）里配置，见第 8 节。
- **Skill**：共 **23** 个，在 `plugins/dalton/skills/`（含 `dalton-language`），通过 `src/ptc_agent/agent/middleware/skills/dalton_registry.py` 合并进上游 registry。
- **Workflow**：共 **6** 个，在 `workflows/<name>/workflow.js`：`initial-coverage`、`full-coverage`、`bullbear-doc`、`earnings-deep-update`、`weekly-coverage-review`、`topic-research`，由对应 skill 通过 `RunWorkflow` 启动。
- **完成契约与断点续跑**（`src/dalton/workflows/contracts.py`、`src/dalton/workflows/journal.py`、`src/dalton/workflows/as_of.py`、`src/ptc_agent/agent/middleware/background_subagent/workflow/tool.py`、`src/ptc_agent/agent/middleware/background_subagent/workflow/driver.py`）：
  - 6 个 workflow 与 8 个多步 skill（`dalton-call`、`dalton-earnings-calendar`、`dalton-earnings-flash`、`dalton-earnings-preview`、`dalton-filing-review`、`dalton-publish`、`dalton-stock-move`、`dalton-thesis-delta`）各自声明 `contract.json`（按 `src/dalton/workflows/contract.schema.json` 校验）。智能体在收尾前必须调用 `dalton_check_done` 校验必需文件、知识层更新、本轮 `verdict`、契约声明的校验命令（`_check_stages` 执行 `validators`，如 `audit.py --strict`）、语言审查（`closing.language_review`）及推送稿（`closing.push_draft`）。
  - 仅对美股（`market` 为 `"US"` 或未传）的 `initial-coverage`、`full-coverage`、`bullbear-doc` 三个 workflow（`US_AS_OF_WORKFLOWS`），当 `as_of` 参数未传或传 `"latest"` 时，由 `resolve_launch_as_of` / `apply_as_of_default` / `last_completed_trading_day`（`src/dalton/workflows/as_of.py`）按 `XNYS` 交易日历的实际收盘时间（`calendar.regular_close`，含提前收盘日与节假日）解析为最近一个已收盘的美股交易日；续跑时优先通过 `adopt_resumed_as_of` 继承原任务运行目录 `<source_dir>/args.json` 中记录的 `as_of`。
  - workflow 每次运行的目录为 `.agents/threads/<short_thread_id>/workflows/<run_task_id>/`（`workflow/driver.py` 的 `run_dir`），各派发步骤的日志保存在运行目录的 `journal/<label>.json`（`src/dalton/workflows/journal.py` 的 `WorkflowJournal`，通过 `write_progress` 在工作区写入进度快照）。阻断写法因 workflow 而异：以 `full-coverage` 为例，`branchArtifacts`、`probeBranchFiles`（派发只读 `dalton-evidence` 子代理核对分支产物文件存在、非空白且达到最小字节数）与 `gateBlock` 在并行取证分支失败或缺产物时返回 `ok: false`、列出 `blocked` / `incomplete` 并附 `resume_hint`，阻断后续阶段；建模步骤失败则不阻断，继续写 memo 并在相应章节写明模型缺口。`initial-coverage`、`bullbear-doc`、`topic-research`、`earnings-deep-update` 用各自内联的 `blocked` 返回阻断；`weekly-coverage-review` 探测失败时不阻断：单家公司文件缺失时跳过该公司的 call，核验子任务整体失败时本轮不派任何 call 并标 `unverified_by_probe`；两种情况都继续生成周会稿，最终返回 `incomplete`。续跑时需向 `RunWorkflow` 显式传入原任务的 `resume` 参数（`workflow/tool.py` 的 `create_run_workflow_tool`，经 `resolve_resume_source` 与 `claim_resume` 校验并认领源运行目录），驱动层通过 `WorkflowJournal.find_replayable` 与 `digest_paths` 复用 `journal/<label>.json` 中 `status="ok"` 且磁盘产物摘要未变的已完成步骤，只重跑失败或未完成的部分。可续跑的 workflow 失败（`RESUMABLE` 或 workflow `worker_lost`）会在已结算任务文本、缺失回复提示（`src/ptc_agent/agent/middleware/background_subagent/tools.py`）和回报消息（`src/server/services/report_back/subagent.py`）中明确指示模型：用户说「继续」时必须调用 `RunWorkflow(workflow=<same>, resume=<task id>)`（规则亦写入 `plugins/dalton/skills/dalton-conventions/SKILL.md`），绝不能用手动 `Task` 重新派发；省略 `resume` 参数则始终从头启动新运行（钩子 `workflow-resume`）。
- **交付物格式**（`src/ptc_agent/agent/prompts/templates/components/user_profile.md.j2` 钩子 `html-md-canonical`）：提示词要求智能体将日常研究笔记与报告草稿保持为 Markdown（`.md`），不在起草或日常研究阶段生成 HTML；仅当用户的输出格式偏好为 HTML（`agent_preference.output_format == "html"`，且为主智能体）时，在 `publish_report` 接受定稿（到达 `awaiting_pm` 或 `published`）后，由智能体按提示词在草稿同目录渲染一份同名 `drafts/<slug>.html`（并非发布接口自动生成）。

### 3.1 子代理并发上限、可续跑交接与沙箱环境

- **后台子代理并发上限与 FIFO 排队**（`src/dalton/subagent_slots.py`、`config.dalton.yaml`，钩子 `subagent-caps`）：
  - 每一轮后台子代理运行（无论是模型直接派发的 `Task`，还是 `RunWorkflow` 的子步骤）均计入两道跨 worker 并发上限（`get_subagent_limits` in `src/dalton/subagent_slots.py`）：
    - **单对话同时运行最多 8 个**（`subagent_limits.max_running_per_thread = 8`）；
    - **全后端同时运行最多 24 个**（`subagent_limits.max_running_global = 24`）。
    - `RunWorkflow` 驱动自身的 `max_concurrent_children`（单次运行 8 个子任务、单线程 2 个运行）仍叠加生效。
  - **Redis ZSET 租约**：槽位保存在每线程 ZSET 与全局 ZSET 中（score 为过期时间戳），持有方每 `RENEW_INTERVAL_S = 30s` 续租一次，租期 `LEASE_TTL_S = 90s`（worker 崩溃后槽位在 90 秒内自动释放）。`ACQUIRE_SLOT_LUA` 在单次原子调用内清理过期成员、检查双上限并写入两个集合，或将超限任务按单调序号留在线程的 FIFO 排队队列中。
  - **超限排队与计时起点**：超出任一上限的派发**自动排队而不失败**。`Task` 工具立即返回排队状态、当前在跑数与队列位置，并提示模型切勿重复派发；子代理图通过 `wait_for_subagent_slot` 每 1–2 秒带抖动轮询等待，**子代理自身的超时计时（`time_limit` / `child_timeout`）在真正拿到槽位后才开始计时**。workflow 子任务排队期间，父任务通过 `registry.note_alive` 持续发送心跳，避免被孤儿回收器误收；另一 worker 上的 `TaskOutput` 通过 `resolve_task_queued_status`（`QUERY_QUEUE_LUA`）从 Redis 读取排队状态。
  - **续租恢复与全路径释放**：若续租时发现租约已消失（例如 Redis 短暂不可用超过 90 秒或事件循环卡顿），以 `reregister` 模式强制把在跑任务写回两个集合（即使暂时超出上限），确保计数真实；Redis 不可用期间放行的任务（fail-open，首次获取时记一条 `WARNING`，故障期间续租静默）也会持续续租并在 Redis 恢复后自动补登记。完成、失败、排队或运行中取消、异常及发布前中止等所有退出路径均幂等调用 `RELEASE_SLOT_LUA` 释放槽位（发起获取前先标记可能已注册，取消时清理后再重抛 `CancelledError`）。
- **可续跑中断保留 Chief of Staff 交接对**（`src/server/services/report_back/handoff/pointer.py`、`src/server/services/report_back/handoff/executor.py`、`src/server/services/report_back/handoff/reserve.py`、`src/server/services/report_back/steer.py`、`src/server/services/report_back/notify_turn.py`，钩子 `handoff-keep-on-resumable`）：
  - 当 Chief of Staff 派给公司分析师的运行因系统可续跑的原因中断（`worker_lost` / `RESUMABLE`，由 `notify_turn.py` 的 `is_resumable_failure` 与 `executor.py` 的 `is_resumable_analyst_failure` 判定，排除用户主动取消）时，失败回报（outbox 路径 `executor.py` 与 steer 路径 `steer.py`）在重新入队、停泊或 POST 之前，先调用 `mark_awaiting_resume`（`AWAITING_RESUME_LUA` in `pointer.py`）按 `dispatch_gen` 原子地在 Redis `ptc_origin` 上写入 `awaiting_resume` 标记（记录分析师运行 ID、`marked_at`、`dispatch_gen`），并在发往 Home 的中断通知中追加「运行被中断，系统会续跑，完成后会再回报。」。
  - Home 汇总轮结束触发 teardown 时，Lua 脚本在原子删除步骤中对照 Redis `TIME` 检查该标记（24 小时过期，对齐 `PTC_ORIGIN_TTL`）并拒绝删除交接对，同时保留未结束汇总轮的指针（`release_pointer_when_kept=False`）。拒绝删除后，Python 侧通过 `still_resumable_or_revoke` 查询 Postgres 确认该运行仍处于可续跑失败状态；若已不再可续跑（或后续终态运行非可续跑，先调 `clear_awaiting_resume`），则按条件撤销标记并重新执行 teardown。孤儿回收（`reserve.py`）使用同一检查，新预留或回滚会保留有效标记而不覆盖；teardown 前读取 `ptc_origin` 与运行指针使用 `get_strict`，Redis 读错误时将任务退回 outbox 重试而非误判为无标记；已删除的派发线程仍无条件丢弃（`ignore_awaiting_resume=True`）。待续跑任务完成后，沿原交接对正常去重、租约与代数检查路径向 Home 回报结果。
- **子代理工作区根路径与沙箱包安装**：
  - 子代理系统提示词的所有构建出口（默认、`custom_prompt`、`custom_prompt_template`）统一经 `_workspace_root_line`（`src/ptc_agent/agent/subagents/compiler.py`，钩子 `subagent-workspace-root`）注入工作区绝对根路径。
  - 沙箱内 `uv pip install` 统一传入 `--system`（`src/ptc_agent/core/sandbox/mcp_setup.py`、`src/server/app/workspace_sandbox.py`，钩子 `sandbox-pip-system`），与 `Dockerfile.sandbox` 保持一致；自动装包前由 `_filter_local_packages`（`mcp_setup.py`，由 `src/ptc_agent/core/sandbox/execution.py` 调用）剔除与本地工作区目录或模块同名的包（若本地名称探测失败，则跳过该批安装并记告警）。

## 4. 知识层

每个公司工作区有 8 份结构化文档，存在 `data/<doc>.json`：`company`、`thesis`、`debate`、`drivers`、`management`、`lessons`、`agenda`、`models`。其中 6 份会渲染成根目录的 `.md` 文件；根目录的结论记录文件由数据库渲染生成。路径定义在 `src/dalton/knowledge/paths.py`，schema 在 `src/dalton/knowledge/schemas/`。

- **写入**（`src/dalton/tools/knowledge_tools.py`、`src/dalton/knowledge/service.py`）：
  - `knowledge_update`（调用 `apply_update`）：带版本号的乐观锁，结构化 ops，按 JSON Schema 校验。失败时返回信息里带有期望的条目结构。
  - `knowledge_append_verdict`（调用 `append_verdict`）：记录五词结论之一（`NO_CHANGE` / `THESIS_STRENGTHENED` / `THESIS_WEAKENED` / `THESIS_BROKEN` / `NEW_THESIS`），同时更新议程，并关闭对应的自主信号。
- **硬性规则**（`src/dalton/knowledge/validate.py`）：
  - `thesis` 的 `evidence` 只能追加；
  - `thesis.official` 只能通过发布来写；
  - `house_view` 的目标价必须指向 `models` 文档里登记过的模型单元格。
- **版本**：每个版本留一份快照 `history/<doc>/<UTC>-vNNNN.json`。数据库表 `dalton_knowledge_versions` 不允许删除，也不允许改内容；`dalton_verdicts` 不允许修改。
- **手动修改**（`src/dalton/knowledge/reconcile.py`，钩子 `knowledge-reconcile`）：
  - 改了渲染出来的 `.md`：修改稿存入 `history/`，记一条 `source=direct_edit` 的版本和 `rendered_md_edited` 警告，Markdown 从最近一次有效的 JSON 重新生成；分析师需要用 `knowledge_update` 把修改合入；
  - 改了 `data/*.json`：按 schema 校验，通过的作为新版本，不通过的回退并记警告。

## 5. 发布治理

`publish_report`（`src/dalton/tools/publish_tools.py`，仅 `admin` 与 `internal` 成员可用，`external` 成员调用在入口处直接拒绝；日常起草、`check_draft` 与 `check_language` 不受限）对应的后端状态机 `run_publish` 在 `src/dalton/publish/service.py`，状态保存在 `dalton_publish_requests` 表（`run_publish` 本身执行数字核验、独立审稿、PM 审批、消费批准与落盘，不包含语言审查与 HTML 渲染；语言审查是 skill/workflow 的流程要求，由 `dalton_check_done` 契约门禁核对）：

1. **数字核验**（`src/dalton/publish/audit.py`、`src/dalton/publish/numbers.py`、`src/dalton/publish/match.py`）：
   - 先做确定性匹配（`match_figures`），对照知识层（`thesis` 证据、`drivers` 历史读数）、`models` 登记的 Excel 模型单元格、来源记录和事实底表（`data/fact_base.json`、`bg_data/<branch>.json`、`lenses_work/<lens>.json`）。
   - **引用定位**（`parse_footnotes`、`parse_facts_ledger` in `src/dalton/publish/numbers.py`）：识别数字附近的 Markdown 脚注 `[^id]` 以及正文内联引用 `[引用N]` / `[回报引用N]`（`_FOOTNOTE_REF_RE`、`_CITATION_REF_RE`），对照文末引用定义或来源表把对应证据源排在匹配首位。
   - **异形同值数字的来源片段定位**（`_extract_hit_window` in `src/dalton/publish/match.py`）：当草稿数字原文（如 `9.14 亿美元`）未直接出现在来源中、回退到数值相等的来源数字（如 `$914 million`）构造供 LLM 复判的上下文窗口时，按提取器相同的分行规则 `body.splitlines(keepends=True)` 定位来源数字所在行行首，跳过同行前序豁免数字（引用或 `[假设]` 单元格）取第 n 次出现（行内查找未命中时回退全文首次出现）。**已知限制**：提取器会遮罩行内代码与链接，而 `body.find` 不会；若同一行更早位置（距真实数字超过一个窗口长度）的行内代码或链接中恰好出现相同数字文本，片段中心可能落在遮罩副本上（该片段仅作 LLM 复判参考上下文，不直接决定结论）。
   - **标准库读模型与未重算模型处理**（`read_model_cell`、`_decode_cell_element`、`count_unrecalculated_formulas`、`_PctNumberFormatUnit` in `src/dalton/publish/match.py`，`_check_unrecalculated_models` in `src/dalton/publish/service.py`）：使用标准库 `zipfile` + `xml.etree.ElementTree` 直接解析 `.xlsx` 内部 XML，不经 `openpyxl` 加载整表。只有当单元格 `number_format` 显式为百分比格式（`_PctNumberFormatUnit`）时才设 `stored_as_ratio=True` 允许 `×100` 折算。数字核验读取模型单元格时，若公式缺少缓存计算值 `<v>`（`_decode_cell_element` 返回错误）或 `models.json` 中 `recalculated` 为 `false`，引用该单元格的数字在 `match_figures` / `aaudit_draft`（`src/dalton/publish/audit.py`）中直接判为 `unverified`（`lock_unverified=True`）；同时 `run_check_draft` 与 `run_publish` 通过 `_check_unrecalculated_models`（调用 `count_unrecalculated_formulas`）扫描草稿引用的 `model:` `.xlsx` 与 `role="model"` 附件，记入 `AuditResult.unrecalculated_model_warnings`，并在 `run_publish` 中将状态置为 `audit_failed` 阻断报告发布（知识层写入本身不检查 `.xlsx` 公式缓存）。
   - **不猜单位、币种与比率**（`values_within_tolerance`、`match_figures`、`aaudit_draft`）：确定性匹配和 LLM 复判引用校验均要求证据原文显式支持目标数字的量级词（亿/万/billion/million 等）与币种，禁止将无单位裸数字跨量级放行（如 `×1000`）或猜测币种。同一指标在同句或相邻句并列双币种（如 `$113.8B（约 ¥8,100 亿）`）时，折算侧标为 `uncertain` 交 PM 复核，不计入 `unverified`。
   - **缓存复用与超时控制**：`check_draft` 在生成的 `<slug>.audit.md` 尾部写入由 `DALTON_AUDIT_CACHE_KEY`（`get_dalton_audit_cache_hmac_key`）HMAC-SHA256 签名的缓存块（`_sign_audit_cache_payload`、`render_audit_md`）；若草稿、模型与知识层哈希未变，`publish_report` 直接通过 `try_load_reusable_audit_from_md` 复用核验结果。沙箱文件读取放入 `asyncio.to_thread`，单次模型调用超时由 `dalton_audit_timeout_s` 控制，整次核验总时长由 `audit_total_timeout_s`（默认 1800 秒）兜底。存在 `unverified` 或 `contradicted` 数字时状态为 `audit_failed`；`uncertain` 交 PM 判断。
2. **独立审稿**：审稿人从 `dalton_reviewer_models`（`src/dalton/llm/model_prefs.py` / `src/dalton/dalton.yaml`）里挑，必须和作者属于不同的模型家族（`src/dalton/publish/review.py`），按 7 项检查。审稿意见为「需修改」时，状态是 `review_revise`。
3. **PM 审批**：
   - 交互式线程里弹出审批卡片（`PublishApprovalCard.tsx`，钩子 `publish-card`）；
   - 自动任务里不打断，状态停在 `awaiting_pm`，并在议程里加一项待审批；
   - 只有 `DALTON_PM_USER_IDS` 里的用户能批准，而且发布请求必须属于当前用户。
4. **一次批准对应一次发布**：批准绑定正文加附件组成的发布包 sha256（`compute_bundle_sha256`）；在消费批准前发布包一旦变化，状态变成 `superseded`。
5. **写入**（`write_published_bundle` in `src/dalton/publish/files.py`）：
   - 生成 `published/<日期>-<slug>.md` 及可选的 `.xlsx`（按约定不覆盖，更正时发布新版本）；
   - `dalton_publications` 和 `dalton_publication_files` 两张表不允许修改；
   - 如果设置了 `updates_thesis`，同时写入 `thesis.official`。

`check_draft`（`run_check_draft`）只做数字核验、不拦截：在输入文件旁边生成 `<slug>.audit.md` 和 `<slug>.annotated.md`，原稿不动。

**语言审查（skill/workflow 约定与 `dalton_check_done` 契约门禁）**（`src/dalton/publish/language.py`、`src/dalton/tools/check_done_tools.py`、`plugins/dalton/skills/dalton-language/SKILL.md`）：

- **只标不改，分段审查**（`run_check_language`、`split_sections`）：调用语言审查模型（`resolve_dalton_language_model`，留空时回退 `dalton_audit_model`）按 `plugins/dalton/skills/dalton-language/SKILL.md` 的规则对章节分段挑错（问题类型 `ISSUE_TYPES`：`filler_opening`、`meta_transitional`、`redundancy`、`wordiness`、`credentialing`、`negation_defensive`、`translationese`）。每条问题的 `quote` 必须逐字命中原稿，生成 `<draft>.language.md` 清单与 `<draft>.language.json` 多轮记录（`language_sidecar_paths`），原稿不改动；模型调用失败时不写侧车、不算通过。
- **两轮门禁与适用范围**：`run_publish` 本身不执行语言审查；语言审查由 skill 与 workflow 要求执行，并在收尾时由 `dalton_check_done` 的契约检查（`_check_language_review`、`_check_language_record`、`_check_push_draft`）核对：
  - 留在工作区给人读的正式产物（`drafts/<slug>.md`、`research/<name>.md`，由 `is_reviewable_deliverable` 统一判定，排除机器文件、`bg_data/`、`data/`、`lenses_work/`、`model_dump/`、知识层渲染文件、`research/topics/conversations/` 与核验侧车）以及 6 类会触发私信推送的任务推送稿（`research/push/<slug>.md`），都必须完成第 1 轮审查（`round=1`）、按清单改稿、再跑第 2 轮复审（`round=2`）。
  - workflow 中由 `general-purpose` 子智能体派发执行；`dalton_check_done` 对契约声明产物、journal 派发回报文件与运行时间窗内新写或修改的可审 `.md` 取并集，逐个核对 `<path>.language.json` 含第 1、2 轮记录且最后一条第 2 轮的 `draft_sha256` 与当前文件哈希一致。推送任务的最终回复必须与通过第 2 轮语言审查的 `research/push/<slug>.md` 一致。

## 6. 自主研究

### 6.1 预检与探针

上游自动任务执行器在启动沙箱前，会对 `dalton-*` 任务调用 `preflight.decide()`（`src/dalton/autonomy/preflight.py`）：

- 没有信号：任务以 `PRECHECK_SKIPPED` 结束，不启动沙箱；
- 有信号：注入 `<dalton_wake>` 指令（外部标题等不可信文本先经 `_sanitize_untrusted_wake_field` 清洗），唤醒分析师，并要求收尾调用 `knowledge_append_verdict` 与 `dalton_check_done`。
- `dalton-data-health`（每天 07:00）：在 `preflight.decide()` 内直接调用 `run_all_health_checks`（超时 `data_health_timeout_s = 600s`），更新 `dalton_data_source_health` 后以 `skip` 结算，零 token。

**探针**（`src/dalton/autonomy/probes/`）：

| 探针 | 适用范围 | 检查什么 |
|---|---|---|
| `sec` | 公司 `watch` | SEC EDGAR 新申报（CIK 按 `watch_state` → `company` 知识文档 → 覆盖名单/内置表 `BUILTIN_TICKER_CIKS` → SEC 官方映射表解析） |
| `cnhk` | 公司 `watch` | 巨潮 A 股与纯港股公告（`finance(route="cn-hk", operation="filings")`），超上限窗口自动二分递归抓取（`_fetch_window_filings_recursive`），标题繁简归一后按关键词分为 `P1`（业绩/回购/并购等）和 `P2`（`classify_cnhk_priority`） |
| `agenda` | 公司 `watch` | 议程里到期的验证点、催化剂和行动项 |
| `earnings` | 公司 `watch` | 按自然日判断是否处于财报前 1–3 天、当天或后 1 天 |
| `calendar` | Home `dalton-earnings-calendar` | 财报日历周更唤醒，并顺带调用 `refresh_due_coverage_groups_for_preflight` 刷新超 85 天的动态规则组 |
| `director` | Home `dalton-director-*` | 研究总监每日派活与周度复盘唤醒 |

- 公司盘中 `watch` 预检只跑 `COMPANY_PROBES = ("sec", "cnhk", "agenda", "earnings")` 四个探针，**不再在 `watch` 里扫描内部研究库**。
- 探针失败后各自按时间退避，间隔依次为 2 分钟、10 分钟、30 分钟、2 小时。
- 信号存在 `dalton_signals` 表去重，7 天过期；每次决策记入 `dalton_preflight_log`。
- SEC `10-K`/`10-Q`/`20-F`/`8-K Item 2.02`、巨潮 `P1`、`agenda`、`earnings`、`calendar`、`director`、`schedule`、`price` 直接唤醒（`is_direct_wake_signal`）；其余 SEC 表单和巨潮 `P2` 合并调一次 `assist_json(task="preflight.triage")` 分诊。

### 6.2 标准任务、内部来源日扫与股价异动规则

排期定义在 `src/dalton/autonomy/schedule.py`，股价异动判定在 `src/dalton/autonomy/price_trigger.py`（迁移 `d013_price_move_firings.py`）：

- **每家公司的标准任务**（`build_company_watch_specs`、`enable_tracking`）：
  - `dalton-<t>-watch`：核心标的周一至周五 06:00–20:30 每 30 分钟一次（美股用 `America/New_York`，A 股和港股用 `Asia/Shanghai`）；观察名单周一至周五 07:30 一次，且不在盘中扫描内部来源。
  - `dalton-<t>-delta`（`resolve_delta_schedule`）：六个内部研究来源（`company-wiki`、`guidepoint`、`funda-ai`、`sales-notes`、`tmtb`、`alphaengine`）、一致预期快照与新公告统一由 `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py` 每天扫一次，时区固定为 `Asia/Shanghai`：`Core` 标的为周二至周六 11:00（`DELTA_CRON_CORE = "0 11 * * 2-6"`，对应美股周一至周五收盘后），`Watch` 标的为周六 11:00（`DELTA_CRON_WATCH = "0 11 * * 6"`）。
  - `dalton-<t>-move-up` / `dalton-<t>-move-down`（股价较前收盘涨跌达 ±4%）：
    - **只在常规交易时段触发**：`price_trigger.py` 通过 `trading_session_date` 与 `extract_today_regular_snapshot` 校验当前处于交易所常规交易时段（`MarketPhase.REGULAR`），盘前、盘后、休市及未完成跨日滚动的报价一律拒绝；当某美股代码下仅有 Dalton 异动任务时，非常规交易时段直接跳过轮询（`should_poll_us_symbol`）。基准前收盘价单独存在 `service._dalton_move_refs`（`record_snapshot_reference`），避免盘前盘后价格污染 `previous_close`。
    - **每个交易日每个方向最多触发一次**：命中阈值时先查 Redis 键 `dalton:price_trigger:<user_id>:<symbol>:<direction>:<trading_date>`，再在单个 Postgres 事务（`claim_dalton_price_move_firing`）中写入 `dalton_price_move_firings`（唯一约束 `(user_id, symbol, direction, trading_date)`）并认领任务执行。
    - **先看新闻和 X，再查公告与卖方库**（`plugins/dalton/skills/dalton-stock-move/SKILL.md`）：被唤醒后第 1 步固定先搜今天和昨天的新闻（`web_search`）和 X（`x_search` / `x-timeline`）弄清当天催化，再查公告、卖方与专家库全文及 `move_quant.py` 去 beta 分解；若判定为纯大盘或板块共振（`NO_CHANGE`），不写推送稿、不发私信。
- **财报一次性任务**（由财报日历排出）：`dalton-<t>-earn-<p>-tm3`（提前 3 个工作日 08:00）、`-t0`（发布时间 + 30 分钟）、`-tp1`（下一个工作日 20:00）。

### 6.3 跟踪管理与自动任务总表

- **单公司跟踪管理**（`src/dalton/autonomy/tracking.py`）：汇总工作区下 `watch`、`delta`、`move-up`、`move-down` 及 `earn-*` 任务的 `enabled` / `disabled` / `incomplete` 状态，支持按公司一键启停（通过 `metadata.paused_by_user` 区分用户主动暂停与待补建任务）及档位切换重排期（`set_tier_and_resync`）。主题工作区（无股票代码）调用 `GET /api/v1/dalton/workspaces/{id}/tracking` 返回 HTTP 200 `{"status": "not_applicable"}`（写接口仍返回 HTTP 400，`src/dalton/autonomy/tracking.py`、`src/dalton/app/router.py`），前端跟踪开关（`web/src/pages/ChatAgent/components/dalton/WorkspaceTrackingToggle.tsx`、`web/src/pages/ChatAgent/components/dalton/useWorkspaceTracking.ts`）自动隐藏并停止轮询，自动任务总表将其视为无股票代码。
- **四域自动任务总表**（`src/dalton/autonomy/system_jobs.py`）：将四个域统一暴露给 REST 接口（`/api/v1/dalton/system-jobs*`、`/api/v1/dalton/workspaces/{id}/tracking*`）、前端「设置 → 模型」中的自动任务卡片（`web/src/pages/Settings/panels/dalton/DaltonSystemJobsCard.tsx`）以及工具 `dalton_coverage_tracking` 和 `dalton_system_jobs`：
  - `company`：单公司跟踪任务（`watch`、`delta`、`move-up`、`move-down`、`earn-*`）；
  - `home`（`HOME_JOB_TITLES`）：`dalton-earnings-calendar`（财报日历扫描）、`dalton-director-daily`（研究总监每日晨会）、`dalton-director-weekly`（研究总监每周复盘）、`dalton-data-health`（数据地图每日体检）；
  - `host`（`_fetch_host_conversation_archive_job`）：宿主机 `conversation-archive`（团队对话归档，经桥接层 `/jobs/conversation-archive*` 查询、开关与立即运行）；
  - `ops`（`DEFAULT_OPS_JOBS`、`_fetch_ops_jobs`）：宿主机 launchd 运维任务 `ops-db-backup`（数据库备份）、`ops-backend-watchdog`（后端看门狗），经桥接层 `GET /jobs/ops` 只读查询。桥接层 `handle_get_ops_jobs` 实时读取宿主机状态：`launchctl print` 判断是否加载，备份日志最后一行给出上次运行与结果，看门狗失败计数文件给出健康状态，下次运行由各 plist 的 `StartCalendarInterval` / `StartInterval` 按宿主机时区计算。设置页里 `host` 与 `ops` 两组只在「高级」模式显示。

### 6.4 研究总监与每用户财报日历

- **研究总监**：`dalton_coverage_board` 给候选研究任务打分（`src/dalton/autonomy/board.py`），依据是问题价值、紧急度和陈旧程度；`dalton_record_plan` 拒绝超出当天预算的计划并写入 `dalton/plans/<日期>.md`；`dalton_watch_event` 登记跟踪事件并立即创建一次性复核任务；`dalton_resolve_signals` 批量关闭已处理的信号。
- **每用户财报日历**（`plugins/dalton/skills/dalton-earnings-calendar/scripts/calendar_state.py`、`src/dalton/coverage/service.py`，迁移 `d015_user_coverage.py`）：
  - 每位有覆盖名单的用户在自己的 Home 工作区拥有独立的 `dalton-earnings-calendar`（周六 09:00 `Asia/Shanghai`），普通用户也可在设置页或通过 `dalton_system_jobs` 开关和立即运行自己的财报日历任务；
  - `calendar_state.py publish` 校验沙箱内的 `Home/dalton/earnings-calendar.json`（上限 1 MB）后调用 `PUT /api/v1/dalton/earnings/calendar/mirror`（`publish_user_earnings_calendar_mirror`）写入 `dalton_earnings_calendar_mirror` 表；前端通过 `GET /api/v1/dalton/earnings/calendar`（`query_user_earnings_calendar`）展示当前用户自己的财报日历。

### 6.5 自动化结果私信推送

自动任务结束结算时，`src/server/services/automation_settlement.py`（钩子 `dalton-notify`）调用 `on_automation_settled`（`src/dalton/notify/dispatch.py`，迁移 `d014_notify_deliveries.py`）：

- **推送范围**（`classify_automation_for_notify`）：仅以下 **4 类（6 种）任务**会推送私信：
  1. 财报快讯与深度更新：`earnings_t0`（`dalton-<t>-earn-<p>-t0`）、`earnings_tp1`（`dalton-<t>-earn-<p>-tp1`）；
  2. 重大股价异动：`price_move`（`dalton-<t>-move-up`、`dalton-<t>-move-down`，要求本轮 `verdict` 不为 `NO_CHANGE`）；
  3. 财报 T-3 前瞻：`earnings_tm3`（`dalton-<t>-earn-<p>-tm3`）；
  4. 每周复盘与财报日历周报：`director_weekly`（`dalton-director-weekly`）、`earnings_calendar`（`dalton-earnings-calendar`）。
  - `watch`、`delta`、`director-daily`、`data-health` 永不推送。
- **按用户、按任务开关**（`src/dalton/llm/model_prefs.py`、`src/dalton/notify/dispatch.py` 的 `resolve_automation_notify_state`）：
  - 存放在当前用户自己的 `user_preferences.model_preference` 中（按用户隔离，不回退到团队凭据持有人）：总开关 `dalton_notify_enabled`（默认 `False`）、接收目标 `dalton_notify_targets`（格式 `feishu:user:ou_<id>` 或 `discord:user:<digits>`）、细粒度开关 `dalton_notify_overrides`。
  - 单任务开关优先级：任务精确名 → 小写任务名 → `category:<类别>` → `<类别>` → 默认开启（`True`）。总开关和接收账号在前端「设置 → 模型」（`web/src/pages/Settings/panels/ModelTab.tsx` / `web/src/pages/Settings/panels/dalton/DaltonSection.tsx` / `DaltonNotifyTargetsCard.tsx`）设置；单个任务的推送开关在自动任务列表里（`web/src/pages/Automations/components/AutomationList.tsx` / `web/src/pages/Settings/panels/dalton/AutomationNotifyToggle.tsx`）逐项切换；类别覆盖（`category:<类别>` / `<类别>`）只能通过偏好设置写入，无前端界面。
- **推送稿语言审查与卡片组装**（`build_feishu_notify_card`、`format_notify_message`、`scripts/dalton/notify_preview.py`）：
  - 6 种推送任务在收尾前必须把推送正文写入 `research/push/<slug>.md`，完成两轮 `check_language` 并通过 `dalton_check_done` 的 `closing.push_draft` 校验。
  - 飞书使用 Card Schema 2.0：按五词结论映射表头配色（`VERDICT_TEMPLATE_MAP`）和业务标签（`CATEGORY_BUSINESS_TAG_MAP`），提取「结论 / 要点 / 下一步验证 / 来源」四个区块（`extract_card_sections`），按块截断（`_truncate_section_body`：单块上限 320 字、全卡正文上限 3000 字，整块保留 Markdown 表格与列表或在句末标点截断，并通过 `_strip_non_http_markdown_links` 剥离沙箱内部路径链接），底部附「在 Dalton 中打开」按钮（链接为 `{DALTON_FRONTEND_URL}/chat/t/{thread_id}`）。Discord 发送纯文本私信。
  - 预览工具 `scripts/dalton/notify_preview.py` 优先经 `CheckpointHistoryReader`（`read_run_answer`）从 LangGraph checkpointer 读取完整回复，仅在不可用时回退到 `automation_executions.result_excerpt`（打印截断告警并通过 `_keep_excerpt_last_line` 丢弃未闭合尾行），再调 `feishu_send_card.py --dry-run` 校验卡片结构。
- **投递账本与重试**：`dalton_notify_deliveries` 表以 `(execution_id, channel, target)` 唯一键做幂等认领（`claim_target_delivery`、`mark_target_delivery`），后端通过宿主机桥接层 `POST /notify`（携带 `DALTON_BRIDGE_ADMIN_TOKEN`）发送。`preflight.decide()` 期间顺带调用 `retry_failed_notify_deliveries`，对 24 小时内失败或 `pending` 超 10 分钟的记录最多重试 3 次（重试前重新核对用户当前开关和目标）。

## 7. 预算与计费

- 只有一个约束：**每人每日的现金额度**，外加可选的单任务上限。不限调用次数，也不限重试次数（`src/dalton/budget/`）。
  - 本地闸门要在 `HOST_MODE=oss` 且 `DALTON_BUDGET_GATE=1` 时才生效（`src/dalton/budget/gate.py`）。当前部署两者都已设置。
  - 默认每天 100 美元，按 `Asia/Shanghai` 的自然日计算。
  - 剩余不足 0.25 美元时拒绝新请求，返回 HTTP 429。
  - 额度按每块 1 美元预留，租期 300 秒。
- **只有现金花费计入额度。** `*-oauth` 模型和标记为 `subscription` 的模型只记名义成本。Coding Plan 的 GLM 配了价格为 0 的价格卡，这样每条计费路径算出来的现金都是 0。
- 预检发现额度用完时，跳过本次唤醒；一次性任务顺延一天。
- 相关代码：钩子 `budget-gate`、`budget-admission`；接口 `/api/v1/dalton/budget*`；设置页的 `DaltonBudgetCard`。

## 8. 模型接入

模型目录由四部分合并而成：上游 manifest、`src/dalton/llm/native_overlay.json`、运行时同步覆盖层 `var/dalton/catalog/native_sync_overlay.json`、`src/dalton/llm/openclaw_overlay.json`（`src/dalton/llm/overlay.py`、`src/dalton/llm/native_catalog.py`，钩子 `llm-overlay`）。`dalton_meta` 字段标明每个模型的家族和计费方式。各个角色默认用哪个模型，由 `agent_config.dalton.yaml`、`src/dalton/dalton.yaml` 和代理定义决定。

| 接入方式 | 供应商 | 说明 |
|---|---|---|
| 订阅 OAuth（上游） | `codex-oauth`、`claude-oauth` | |
| 订阅 OAuth（Dalton） | `antigravity-oauth` | Google 账号；Gemini，以及经它转接的 Claude、GPT-OSS |
| 订阅 OAuth（Dalton） | `muse-oauth` | 登录后向 `/muse-code/key` 换发模型 API key；会话头和 Muse Code CLI 1.4.12 一致 |
| 订阅 OAuth（Dalton） | `xai-oauth` | SuperGrok：`auth.x.ai` 设备码登录，带 Redis 锁的刷新，经 `cli-chat-proxy.grok.com` 调用。`x-grok-client-version` 由 `DALTON_XAI_GROK_CLIENT_VERSION` 指定（默认 `1.0.67`），过旧会收到 426 |
| 订阅 API key | `z-ai-cn-coding` | 智谱 GLM Coding Plan，Anthropic 协议；模型 `glm-5.3-plan`、`glm-5.3-flash-plan`，默认思考强度 low。只认自己名下的 key，不会回落到按量计费的父供应商。频率限制较严，批量调用时要低并发并退避 |
| OpenClaw broker | `openclaw-broker` | `ocb-*`，`http://{HOST_IP}:18789/dalton-broker/v1`，用 `scripts/dalton/sync_openclaw_models.py` 同步 |
| 现金 | `deepseek`、`xai`（API key）等 | |

**角色策略**（`src/dalton/llm/role_policy.py`）：

- contributor 变体在所有角色里都禁用；
- 主角色和子代理必须支持 tool calling；
- 审稿人必须和作者属于不同家族；
- 可以通过 `models.disabled_providers` 整体关掉某个供应商。

**团队凭据**（`src/dalton/credentials.py`）：同时满足四个条件时，经 `credential_user_id` 解析的用户模型凭据会统一落到第一位 PM 名下：Dalton 启用、使用 Dalton 的 Supabase 登录模式、`credentials.sharing: team`、设置了 `DALTON_PM_USER_IDS`。宿主机上的数据服务密钥不在这个机制内。

**Dalton 模型路由设置**（`src/dalton/llm/model_prefs.py`）：数字核验模型与备选及超时（`dalton_audit_model`、`dalton_audit_fallback_models`、`dalton_audit_timeout_s`）、语言审查模型（`dalton_language_model`，留空时回退 `dalton_audit_model`）、辅助任务模型与备选（`dalton_assist_model`、`dalton_assist_fallback_models`）、审稿模型候选（`dalton_reviewer_models`）、对话归档抽卡模型（`dalton_extract_model`）统一在前端「设置 → 模型」页面的「路由（Routing）→ Dalton」区域修改，存入 `user_preferences.model_preference`。读取时经 `dalton_model_pref(user_id, key)` 按以下顺序生效：**当前用户自己的设置 → 团队凭证持有人（`src/dalton/credentials.py` 解析出的第一位 PM）的设置 → `src/dalton/dalton.yaml` 配置文件默认值**；留空即回退到下一层，源码中不硬编码具体模型名。

**原生订阅模型巡检、同步与退役**（`src/dalton/app/router.py`、`src/dalton/services/native_admin.py`、`src/dalton/llm/native_catalog.py`、`src/dalton/services/model_references.py`，迁移 `d012_native_model_sync.py`）：

- PM 可在设置页或通过 `GET /api/v1/dalton/native-models/status`（`get_native_status`）及 `POST /api/v1/dalton/native-models/{check,sync,retire,restore}`（端点在 `src/dalton/app/router.py`，如 `check_native_models_endpoint`；服务层为 `src/dalton/services/native_admin.py` 的 `run_native_check`、`run_native_sync`、`run_native_retire`、`run_native_restore`）查询状态、巡检六个原生订阅供应商的上游模型列表，勾选新增模型写入 `var/dalton/catalog/native_sync_overlay.json`，或将已下线模型标记退役 / 恢复。
- 退役前通过 `src/dalton/services/native_admin.py` 的 `_collect_reference_map`（汇总 `src/dalton/services/model_references.py` 的各处收集函数）检查 `agent_config`、`dalton.yaml`、`user_preferences`、自定义与插件子代理以及自动任务；若仍有引用则拒绝退役（HTTP 409）。每次巡检和变更通过 `create_native_sync_run` / `finish_native_sync_run`（`src/dalton/database/native_sync.py`）记入数据库。

**模型容错、槽位降级与 Anthropic 系统消息位置兼容**：

- **模型回退原因日志与事件透传**（`src/ptc_agent/agent/middleware/model_resilience.py`，钩子 `fallback-reason-log`）：主模型调用失败触发 fallback 时，记录 `WARNING` 日志（含源模型、目标模型、HTTP 状态码、异常类型及脱敏截断后的错误文本），并将 `error_type` 透传至流式 fallback 事件（`src/server/services/runs/sse_producer.py`、`src/server/services/history/projector.py`）。
- **无客户端槽位降级检查范围**（`demote_clientless` in `src/server/services/llm/subagents.py`、`src/server/services/llm/config.py`）：仅检查当前轮次角色在主槽位上的模型固定配置，Home 不再对仅限 analyst 的固定配置告警，后台槽位调用不再产生告警。
- **Anthropic 系统消息位置兼容**（`src/llms/operator_channel.py` 的 `_placeable` 与 `_as_reminder_turn`，回移植上游 #596，钩子 `upstream-backport-596`）：当上一轮在模型回复前被中断、导致运行时上下文 `system` 条目紧邻下一条 `user` 消息（违反 Anthropic Messages API 要求 `system` 必须位于 `assistant` 消息之前或数组末尾的规则）时，无法按 `system` 角色放置的条目自动包装为 `user` 轮内的 `<system-reminder>` 块发送，其余条目保持 `system` 角色，使已中断线程在下一轮自动恢复而不再返回 HTTP 400 触发模型回退。

## 9. 数据接入

**researchdata 桥接与双令牌隔离**（`scripts/dalton/researchdata_bridge.py`、`src/dalton/settings.py`）在宿主机上运行，只用 Python 标准库：

- 通过 stdio 包装 OpenClaw 的 researchdata MCP 服务，使用**两个独立令牌**隔离沙箱数据调用与宿主机管理操作：
  - **MCP 令牌（`DALTON_BRIDGE_TOKEN`）**：仅能访问 `POST /mcp`（通过 `Authorization: Bearer` 或 `X-Dalton-Bridge-Token` 请求头传递，由 `_is_valid_mcp_token` 校验；旧 `/mcp/<token>` 路径仅作兼容保留）。该令牌传给后端和沙箱供 `execute_code` 调用 `catalog`、`research`、`finance`、`macro`、`community`；拿它访问任何管理接口都会被 `_require_admin_token` 拒绝并返回 HTTP 403。
  - **管理令牌（`DALTON_BRIDGE_ADMIN_TOKEN`）**：除同样能访问 `POST /mcp`（`_is_valid_mcp_token` 同时接受两种令牌）外，可访问 `POST /embed`（向量化）、`POST /wiki/ingest`（公司 Wiki 入库）、`POST /feishu/*`（飞书文档读写与知识空间查询）、`GET /jobs/conversation-archive`、`POST /jobs/conversation-archive/{enable,disable,run}`（宿主机对话归档开关与触发）、`GET /jobs/ops`（宿主机 launchd 任务状态）以及 `POST /notify`（飞书卡片 2.0 与 Discord 私信发送）。未配置时管理接口 fail-closed 返回 HTTP 401。该令牌只配置在宿主机桥接层与后端容器内，**管理令牌不得进入沙箱**：`src/ptc_agent/core/sandbox/ptc_sandbox.py`（`_build_sandbox_env_vars`）以及 `sanitize_sandbox_env_vars` / `sanitize_mcp_client_config_server`（`src/dalton/settings.py`）会剥离任何包含管理令牌的环境变量或 MCP 配置项。
  - `GET /healthz` 无需鉴权。
- 以下路线由桥接层自己执行：
  - `xueqiu`、`employee`、`last30days`；
  - `x-timeline`：只读地调用 OpenClaw 已登录的 `xreach`，只放行 `tweets`、`tweet`、`thread`、`search`，并会转交代理。
- 参数处理：
  - `offset` 和 `max_chars` 会被截到网关允许的范围；
  - 传错位置的 document id 会被映射或给出提示；
  - `transcript` 路线的主题词查询会被丢弃，只用 ticker 去查。

**行情与财务数据：yfinance 优先、FMP 按需与共享额度熔断**：

- **`yfinance` 优先与 FMP 兜底**（`FallbackFinancialSource` in `src/dalton/market_data_fallback.py`，钩子 `market-data-fallback`）：公司概况、实时报价、利润表、现金流量表、关键指标、财务比率、股价表现、分析师目标价与评级、历史 EPS、股票筛选与搜索（`_YFINANCE_FIRST`）一律**优先调用 `yfinance`**，仅在抛错、返回空或价格为 0 时回退 FMP；**只在 `yfinance` 不提供的接口上直连 FMP**（`_FMP_ONLY`：分部收入 `get_revenue_by_segment` 与指定历史日期的板块表现 `get_sector_performance(target_date=...)`）。后台新闻轮询（`src/server/services/news_refresh_service.py`）从各订阅源链中移除 FMP（若某订阅源移除后无剩余供应商则跳过并记 `INFO` 日志），不再消耗 FMP 额度；用户主动发起的新闻查询仍可使用 FMP（`config.dalton.yaml` 中新闻源顺序为 `ginlix-data`、`yfinance`、`fmp`）。
- **共享 FMP 额度熔断**（`src/data_client/fmp/fmp_client.py`，钩子 `market-data-fallback`）：任一 worker 收到 FMP HTTP 429 或 HTTP 200 `"Limit Reach"` 正文时，`trip_quota_marker` 在 Redis 写入 `fmp:quota_exhausted`，过期时间对齐到下一个美东 15:00（`next_quota_reset`）或响应头 `x-ratelimit-reset` / `retry-after`；熔断生效期间所有 worker 的 `FMPClient._make_request` 直接抛 `FmpQuotaExhausted` 短路，不再发网络请求（HTTP 402 套餐限制只记单次失败，不触发全员熔断）。
- **每进程 yfinance SQLite 缓存隔离**（`configure_process_cache` in `src/dalton/yfinance_cache.py`，钩子 `yfinance-cache`）：将 `yfinance` 的时区、cookie 和 ISIN SQLite 缓存目录设为 `/tmp/langalpha-yfinance/<pid>` 并在 `fork()` 后重置连接单例，消除多进程并发写 WAL 导致的 `database is locked`。
- **盘前盘后与休市报价标注及历史收盘回退**（`src/dalton/extended_hours.py`、`src/data_client/yfinance/data_source.py`）：美股在盘前、盘后及休市窗口（按 `XNYS` 交易日历判定）从 `ticker.info` 补齐盘前盘后价与涨跌幅（`enrich_yfinance_snapshot_extended_hours`），并通过 `format_quote_change_parts` / `snapshot_quote_time_labels` 区分常规收盘（标注 `close MM-DD` 或 `last close MM-DD`）与盘前盘后（标注 `vs prev close $X` 与 `@ HH:MM ET`），防止把上一交易日收盘标成 `today`。当周末或节假日 `yfinance` 的 `fast_info` 抛出 `KeyError`（如 `'currentTradingPeriod'`）时，报价自动回退到 `history(period="5d", auto_adjust=False)`（`_snapshot_from_history` in `src/data_client/yfinance/data_source.py`），按交易所日历（无法解析日历时以美东 16:00 兜底）仅取已收盘场次的 K 线，状态标为 `closed` 并由 `extended_hours.py` 使用快照的 `regular_trading_date` 标注收盘日期（`as_of` 不超过当前时间）。

**数据地图**：静态说明来自 `src/dalton/data_map/sources.yaml`，实际的路由结合运行时 catalog，可用性取自体检记录；没有体检记录的显示「未体检」（`src/dalton/data_map/builder.py`）。

- 每天 07:00 由 `dalton-data-health` 在预检内体检一次（`src/dalton/data_map/health.py`），结果写进 `dalton_data_source_health`，再渲染进提示词。
- 资料库探测「最新一条」的方式：
  - `tmtb`、`funda-ai`、`company-wiki` 用按时间排序的 `list`；
  - `alphaengine`、`guidepoint`、`sales-notes` 用 30 天窗口搜索；`transcript` 用 120 天窗口，因为电话会按季度开；
  - `all` 取它实际查询的六个来源中最新的日期；
  - `last30days` 调用成本高，不做自动探测，状态固定为 `unprobed`。
- 港股公告走 `cn-hk` 路线的 `filings` 操作（巨潮的 `hke` 栏目），覆盖率大约 95%；仅英文版的公告和股东大会投票结果，退回 `site:hkexnews.hk` 搜索。

**其他数据入口**：

| 入口 | 说明 |
|---|---|
| `dalton_sec` | stdio MCP；XBRL 期间归一，可选按原始披露或按最新重述取数 |
| 行情 | `get_quote` 等，盘前盘后与休市日期标注在 `src/dalton/extended_hours.py`，财务回退在 `src/dalton/market_data_fallback.py` |
| 网页搜索 | `search_api: openclaw`，经 `web_search` 桥接（`127.0.0.1:8961`） |
| 网页读取 | `fetch_chain: ["inhouse", "firecrawl"]`：自家抓取失败的 URL 才交给 Firecrawl（`FIRECRAWL_API_KEY`，和 OpenClaw 共用账号） |
| `x_search` | 经桥接层转到 OpenClaw 网关 |

### 9.1 公司 Wiki 入库

管理员（`admin`）与内部成员（`internal`）可从 Chief of Staff（Home）或公司工作区（analyst）向 `company-wiki` 归档材料；外部成员（`external`）在工具入口即被拒绝，包含 `confirm=False` 预览在内均不调用桥接层（`src/dalton/tools/wiki_tools.py`、`src/dalton/database/wiki_ingests.py`、`scripts/dalton/researchdata_bridge.py`，迁移 `d017_wiki_ingests.py`，钩子 `wiki-ingest`）：

- **工具与入库规范**：`ingest_company_wiki` 同时绑定在两个角色上，仅在用户明确要求归档时调用。规则定义在 `plugins/dalton/skills/dalton-conventions/references/wiki-ingest.md`：卖方材料一律归入 `sales-notes`，严禁写入 `company-wiki`；遵循目标与 slug/别名规范及允许的 `content_type`；正式写入前先执行 dry-run 预览。
- **限流与状态账本**：工具从调用方自己的沙箱读取文件发往宿主机桥接层。每位用户每小时最多入库 `WIKI_INGEST_RATE_LIMIT_PER_HOUR`（30）次（`check_and_increment_wiki_ingest_rate_limit`，经单条原子 Redis Lua `INCR` + `EXPIRE` 计数，Redis 异常时拒绝入库）。调用桥接层前先在 `dalton_wiki_ingests` 表写入 `pending` 行，完成后转为 `done` 或 `failed`（`WHERE status IS DISTINCT FROM 'done'`，`done` 永不被覆盖；若桥接层返回同一 `request_id` 正在执行，该行保持 `pending` 并提示调用方重试）。
- **桥接层校验、文件锁与幂等去重**（`POST /wiki/ingest`，受 `DALTON_BRIDGE_ADMIN_TOKEN` 保护）：
  - 校验目标与 slug、`content_type`、文件大小（单文件上限 20 MB）、扩展名，并对文件做全字节密钥扫描；
  - 所有可能在 CLI 启动前失败的准备工作（解析 CLI、调用 `_build_wiki_subprocess_env` 构造环境）均在获取第一把锁之前完成；随后按固定顺序在稳定锁文件上获取内核 `fcntl.flock` 锁：`request_id` → 内容 SHA-256 → 目标槽位（`finally` 中释放，进程异常退出时由内核自动释放，无需陈旧锁抢占）；
  - CLI 成功后，先将结果完整写入独立幂等文件、`fsync` 并原子替换落盘，再写审计日志（损坏的幂等文件永不覆盖）；相同 `request_id` 的重试直接重放该结果并补齐缺失审计记录，不再重复运行 CLI；
  - 去重同时比对幂等记录与审计日志中的 SHA-256 以及目标文件的哈希（在事件循环外计算），PDF、图片等二进制文件同样按全字节 SHA-256 去重。
- **已知限制**：沙箱内检查与读取之间理论上可将文件替换为符号链接；由于读取发生在调用方自己的沙箱内、只能指向同一沙箱内的文件（同一 computer 上的工作区设计上互信且无法访问宿主机文件），未在沙箱内逐级加 `O_NOFOLLOW`。

### 9.2 飞书文档

智能体可读写飞书（Lark）文档，并按三层身份实施权限隔离（`src/dalton/access_tiers.py`、`src/dalton/tools/feishu_tools.py`、`src/dalton/feishu.py`、`src/dalton/database/feishu.py`、`scripts/dalton/researchdata_bridge.py`，迁移 `d019_feishu_read_scopes.py`，钩子 `feishu-docs`）：

- **三层身份与读写范围**（`get_user_tier` in `src/dalton/access_tiers.py`，由名单固定、无 UI 修改入口，任何查询失败均 fail-closed 降为 `external`）：

  | 操作 | Admin（`DALTON_PM_USER_IDS`） | Internal（邮箱在 `DALTON_ALLOWED_EMAILS`） | External（其余用户，含邀请链接注册成员） |
  |---|---|---|---|
  | 读取文档、列出知识空间 | ✅ | ✅ | 仅限管理员加入白名单的知识空间（wiki space） |
  | 新建文档 / 文末追加 | ✅ | ✅ | ❌（服务端直接拒绝，不调用桥接层） |

  - 若管理员将受邀成员的邮箱加入 `DALTON_ALLOWED_EMAILS`，该成员即晋升为 `internal`。
- **工具与触发约定**（`src/dalton/tools/feishu_tools.py`）：`feishu_read`、`feishu_list_wiki`、`feishu_create_doc`、`feishu_append` 同时绑定在 Chief of Staff 与公司分析师上。按 `plugins/dalton/skills/dalton-conventions/SKILL.md` §6 约定，交付物默认留在工作区内，**只有在用户明确要求时才新建或追加飞书文档**（Chief of Staff 派活给分析师且用户要求写飞书时，派活指令必须显式写明）；用户给出链接或要求读取时调用读取工具。
- **宿主机凭据隔离与桥接路由**（`FeishuClient` in `scripts/dalton/researchdata_bridge.py`）：飞书调用全部在宿主机桥接层完成，凭据取自 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 或只读查询 OpenClaw 的飞书通道账号，Docker 后端与沙箱均不持有 app secret。五条 `/feishu/*` 路由（`/feishu/doc/read`、`/feishu/wiki/nodes`、`/feishu/wiki/node`、`/feishu/doc/create`、`/feishu/doc/append`）均受 `DALTON_BRIDGE_ADMIN_TOKEN` 保护，沙箱的 MCP 令牌无法访问；上游错误文本截断至 200 字符且不含请求体或请求头。
- **写入、块清洗与自动授权**：
  - 写入正文时先将 Markdown 转为块并插入 `children_id` 与 `descendants`，按 OpenClaw `cleanBlocksForDescendant` 规则清洗块结构（`_sanitize_feishu_block`：移除 `parent_id`、规范化表格单元格子块、重建 `table.property`）。第一期仅支持新建与文末追加，不支持覆盖、删除、移动或修改权限。若文档创建成功但正文写入失败，桥接层返回 HTTP 502 及 `document_id` 与 `url`，工具提示「文档已创建但正文没写进去，请到 `<url>` 手动处理」。
  - 新建文档后，桥接层按 OpenClaw 规则调用 `FeishuClient.grant_chat_full_access` 自动将该文档 `full_access` 共享给团队跟踪群（环境变量 `FEISHU_DEFAULT_GRANT_CHAT_ID`，默认为 `oc_397207208fb52a0b09969794ef3cd421`，设为空值可关闭群共享，修改需重启桥接层），随后再共享给调用方的真实邮箱（跳过邀请注册的合成域名邮箱）；单步共享失败仅附加告警，响应仍为 HTTP 200 并保留文档。
- **外部成员读取校验与白名单管理**：
  - 链接或裸 token 经 `resolve_feishu_node_for_check` 一次性解析（支持 wiki URL、docx URL 或裸 token，先按 wiki 节点解析，再按 docx 反查），权限检查与实际读取复用同一解析结果。快捷方式节点（`shortcut`）要求其 `space_id` 与 `origin_space_id` 均在白名单中；`feishu_list_wiki` 使用相同规则过滤。不属于任何知识空间的云盘文档对外部成员一律拒绝。
  - 白名单存于 `dalton_feishu_read_scopes` 表，管理员通过 `GET` / `PUT /api/v1/dalton/feishu/read-scopes` 与 `GET /api/v1/dalton/feishu/spaces`（`src/dalton/app/router.py`、`src/dalton/database/feishu.py`，整表替换以 `pg_advisory_xact_lock` 串行化，`space_id` 去除首尾空白后限制为 `[A-Za-z0-9_-]` 且最多 200 条，否则返回 HTTP 422）或在设置页 `FeishuReadScopesCard.tsx`（`web/src/pages/Settings/panels/dalton/FeishuReadScopesCard.tsx`）维护。

## 10. 团队对话归档、团队与权限及全局搜索

### 10.1 团队对话归档

**导出**：`scripts/dalton/conversations/export_openclaw.py` 只读取 OpenClaw 的 SQLite（使用 backup API）。

- 只保留真人会话，丢弃 cron、heartbeat、子代理和测试会话；
- 只取文本，并做脱敏。

**处理流程**：`scripts/dalton/conversation_archive.py` 提供 `run` 和 `backfill` 两个命令；当更换抽卡模型或提示词版本需要全量重抽时，使用 `scripts/dalton/reextract_conversations.py`（正常流程 `plan` → `extract` → `apply` → `render`：新卡先暂存到运行目录的 `STAGED_FILE`，`apply` 在单事务内原子替换旧卡并保留完整快照；出错时用 `rollback` 删除本轮新卡、恢复旧卡，之后再跑一次 `render`）。

- 分段：间隔超过 30 分钟就切开，每段最多 6000 字、25 个事件；
- 每段抽取成原子化的**卡片**（`src/dalton/conversations/extractor.py`），每张卡片包含类型、claim、原话引用、数字、ticker、entities、置信度；
- 程序做确定性兜底校验：原话子串核对（`verify_verbatim_quote`）、数字与期间/币种核对（`normalize_number_token` 对超过安全整数转换上限的超大整数、`Infinity` / `NaN` 及触发 `ValueError` / `OverflowError` 的 token 返回 `None`，按非数字处理）、专名与禁用结论词检查、未上市私企分流至 `entities`、券商渠道词过滤，以及结合每用户覆盖名单别名表（`get_user_aliases_sync` + `SUPPLEMENTAL_TICKER_ALIASES`）的股票代码归一与双上市回填；
- **按会话隔离阻断与确定性失败分类**（`scripts/dalton/conversation_archive.py`、`is_deterministic_extraction_error` in `src/dalton/conversations/extractor.py`）：未完成前缀阻断（`blocked_by_incomplete_prefix`）按 `(agent, session_id)` 隔离，单个失败片段仅阻断同一会话的后续片段，其他会话已完成的片段照常刷盘并写入 `processed`（跨会话 supersede 在入库事务内按双向时间戳比对）。解析/校验错误及代码异常（`ValueError`、`TypeError`、`KeyError`、`NameError`、`ImportError` 及 `Unicode*Error` 家族）归为确定性失败，主模型（含 OAuth 主模型）失败后仅在备选模型上重试一次，若仍失败则直接结束为 `failed`（不写 `processed` 行、不再退避重试）；包装的 `PoolTimeout` 等瞬态错误保留重试路径（**已知限制**：`LLMService.complete` 内部抛出的代码异常被 `complete.py` 包装为 `stream_cut`，仍按瞬态错误处理）；
- **每小时预算**：`src/dalton/dalton.yaml` 中的 `conversations.hourly_token_budget` 设为 `5000000`（5M tokens/小时，参见 `docs/dalton/dev-guide/CONVERSATION-ARCHIVE.md`）；
- 经宿主机桥接层 `POST /embed`（携带 `DALTON_BRIDGE_ADMIN_TOKEN`）向量化后存入 `dalton_conversation_cards`（pgvector），用 `content_hash` 去重。

**使用**：管理员与内部成员的分析师和 Chief of Staff 都可以用 `conversation_search` 检索卡片（`src/dalton/tools/conversation_tools.py`；外部成员调用会被直接拒绝，提示改用全局搜索检索自己的对话）。检索方式是向量、trigram 和 RRF 混合排序，时间衰减的半衰期参数为 180 天，权重最低不低于 0.7。卡片还会按天渲染成文件：只涉及一家公司、并且这家公司已有工作区的卡片，写到该工作区的 `conversations/<日期>.md`；跨公司主题的卡片，以及还没有工作区的公司的卡片，写到 Home 的 `research/topics/conversations/<日期>.md`（`src/dalton/conversations/render.py`）。

**调度**：每天 21:15（宿主机本地时间）由 launchd 运行仓库脚本 `scripts/dalton/launchd/archive_daily.sh`（`scripts/dalton/install_conversation_archive.sh` 安装）。脚本先读设置页开关的状态文件，关闭时跳过；退出码 3（有段落未完成）时等待后重试一次，重试前再读一次开关。整次运行的输出（含 stderr）写在一个 `=== <时间> run` 运行头下，桥接层 `/jobs/conversation-archive` 据此只返回最后一次运行的日志，设置页只在失败时显示。

### 10.2 团队与权限（邀请注册、三层身份、工作区共享与外部成员隔离）

- **PM 单次邀请链接注册**（`src/dalton/invites.py`、`src/dalton/database/members.py`、`web/src/pages/Settings/panels/dalton/DaltonInvitesCard.tsx`、`web/src/lib/dalton/InviteRedeemPage.tsx`，迁移 `d016_members_and_invites.py`，钩子 `invite-signup`）：
  - **签发与撤销**：PM（`DALTON_PM_USER_IDS`）在设置页 `DaltonInvitesCard.tsx`（仅当 `can_invite` 为 `true` 时显示，接口 `POST` / `GET /api/v1/dalton/invites`、`DELETE /api/v1/dalton/invites/{invite_id}`）创建、列出和撤销邀请。邀请链接格式为 `{DALTON_FRONTEND_URL}/invite#{token}`，有效期 7 天，单次有效。令牌置于 URL fragment 中以免进入访问日志；`InviteRedeemPage.tsx` 读取令牌后立即清空 fragment，并以 JSON 请求体调用 `POST /api/v1/dalton/invites/check` 与 `POST /api/v1/dalton/invites/redeem`；数据库 `dalton_invites` 仅保存令牌的 SHA-256 摘要并按常量时间比对。
  - **三步兑换与重试保护**（`redeem_invite_token` in `src/dalton/invites.py`）：① 锁定邀请行并校验处于 `pending`、用户名未被占用且不与 `dalton_members` 或 `DALTON_ALLOWED_EMAILS` 冲突，随后记录 `redeeming_username`；② 通过 Supabase Admin API（需在 `.env` 配置 `SUPABASE_SECRET_KEY`，缺失时返回 HTTP 503「邀请注册未配置」；Supabase 公开注册保持关闭，见 `docs/dalton/dev-guide/AUTH-DEPLOY.md` §6）创建用户，邮箱设为 `{username}@{DALTON_INVITE_EMAIL_DOMAIN}`，`email_confirm=True`，并在 `app_metadata.dalton_invite_id` 记录创建该账号的邀请 ID（超时后重试若发现 Supabase 用户已存在，仅当其 `app_metadata.dalton_invite_id` 等于当前邀请时才重置密码，其余既有账号一律返回 HTTP 409，防止并发或重试接管无关账号）；③ 再次锁定邀请行，插入 `dalton_members` 记录并将邀请标为已使用（记录 Supabase 用户 ID）。
  - **成员判定、登录页与防滥用**：团队成员定义为 `DALTON_ALLOWED_EMAILS` 或 `dalton_members` 中的任一用户（`is_team_member` in `src/dalton/invites.py`，供 `src/dalton/auth.py` 的 `verify_dalton_token` 及 `src/dalton/sharing.py` 使用；进程内仅缓存肯定结果，新成员在所有 worker 上立即可见；查询失败拒绝访问）。Dalton 模式下登录框（`web/src/pages/Login/LoginPage.tsx`）接受「邮箱或用户名」，不含 `@` 的输入自动转为 `{username}@{VITE_DALTON_INVITE_EMAIL_DOMAIN}`（非 Dalton 模式保持不变）。公开的 `check` 与 `redeem` 接口按客户端 IP 在 Redis 限流（仅当直接对端属于 `DALTON_TRUSTED_PROXIES` 时采信 `X-Forwarded-For`，IPv4 映射 IPv6 对端先归一化；Redis 故障时返回 HTTP 503）。
- **三层身份与调用方 / 所有者分类**（`get_user_tier`、`classify_owner_tier` in `src/dalton/access_tiers.py`）：
  - **调用方身份（`get_user_tier`）**：由名单固定，无 UI 修改入口；任何查询失败均 fail-closed 解析为 `external`，PM 检查失败继续走邮箱检查而绝不授予 `admin`：
    - `admin`：`user_id` 在 `DALTON_PM_USER_IDS` 中；
    - `internal`：邮箱在 `DALTON_ALLOWED_EMAILS` 中（若管理员将邀请注册成员的邮箱加入 `DALTON_ALLOWED_EMAILS`，即晋升为 `internal`）；
    - `external`：其余所有用户（包含经邀请链接注册、不在公司邮箱白名单中的成员）。
  - **资源所有者身份（`classify_owner_tier`）**：严格返回 `admin` / `internal` / `external` / `unknown`。对调用方降级为 `external` 是收紧权限，但对资源所有者若同样降级为 `external`，会把内部成员的 `hidden` 工作区误开放给管理员只读查看，或使邀请注册的 PM 丢失管理员身份；因此凡是名单检查抛错、找不到用户行或邮箱为空，一律返回 `unknown`，调用方遇到 `owner_tier == "unknown"` 时直接隐藏该工作区（返回 HTTP 404 或在列表中跳过），绝不套用任一隔离规则。列表查询中由 `_classify_owner_row_tier`（`src/dalton/sharing.py`）对连接查询出的 `owner_email` 按同一规则分类。
- **按工作区、按通道的团队共享设置与外部成员完全隔离**（`src/dalton/sharing.py`、`src/dalton/app/router.py`、`web/src/pages/ChatAgent/components/dalton/WorkspaceSharingModal.tsx`、`web/src/pages/ChatAgent/components/dalton/TeamSharing.tsx`，迁移 `d018_workspace_sharing.py`，钩子 `workspace-sharing-settings`）：
  - 每个公司工作区在 `dalton_workspace_sharing` 表中有两项按通道划分的共享设置（在内部成员与管理员之间生效），各有三档：

    | 通道设置 | 可选值 | 默认值（表中无记录） |
    |---|---|---|
    | 同事 **Web 界面**（`web_access`） | `rw` / `ro` / `hidden` | `hidden` |
    | 同事 **Chief of Staff**（`cos_access`） | `rw` / `ro` / `hidden` | `ro` |

  - **Home 工作区永不共享**（无论设置如何）。全部权限校验均在服务端执行；仅内部/管理员工作区的所有者或 PM（`DALTON_PM_USER_IDS`）可修改共享设置（`can_edit_workspace_sharing`，接口 `GET` / `PUT /api/v1/dalton/workspaces/{id}/sharing`，部分更新由 `upsert_workspace_sharing` 原子写入）。
  - **统一权限校验与外部成员隔离**（`require_workspace_access` 与 `require_thread_access` in `src/dalton/sharing.py`）：
    - 所有者本人始终拥有其工作区（含 Home）的完整读写权限，无论自身身份层为何。
    - **外部成员隔离在 Home / `deleted` / `unbound` 状态检查、所有者成员资格检查及共享表读取之前执行**（防止通过 403 与 404 状态码差异泄露工作区存在性）：
      - 调用方为 `external`（`get_user_tier(uid) == "external"`）：访问任何他人工作区、线程、文件或知识层文档，在 `web` 与 `cos` 两条通道上一律返回 HTTP 404（无视所有者的共享设置）；
      - 所有者为 `unknown`（`classify_owner_tier(owner_id) == "unknown"`）：对所有非所有者一律返回 HTTP 404；
      - 所有者为 `external`（`owner_tier == "external"`）：对 `internal` 调用方一律返回 HTTP 404；仅 `admin` 调用方只读可见（`need="read"` 置为 `ro`，但 Home 及 `unbound` / `deleted` 工作区对 `admin` 同样返回 HTTP 404；`need="write"` 返回 HTTP 403 `EXTERNAL_OWNED_ADMIN_WRITE_FORBIDDEN_DETAIL`）。外部成员工作区的共享设置不生效且不可修改：`GET /api/v1/dalton/workspaces/{workspace_id}/sharing` 返回 `owner_external=True`、`can_edit=False`，`PUT /api/v1/dalton/workspaces/{workspace_id}/sharing` 对外部所有者本人及 PM 均返回 HTTP 403；前端 `WorkspaceSharingModal.tsx`（通过 `createPortal` 挂载到 `document.body` 并复用 `ModalShell` 渲染）在 `owner_external` 时隐藏选项与保存按钮并展示只读说明。管理员同样不能修改外部成员工作区的自动跟踪设置（`src/dalton/app/router.py` 的 `_resolve_tracking_workspace_owner` 在 `owner_tier == "external"` 时返回 `can_edit=False`，写请求返回 HTTP 403）。
    - **内部成员与管理员之间的共享语义**（所有者为 `internal` / `admin` 且调用方为 `internal` / `admin`）：`hidden` 返回 HTTP 404（不泄露存在性），`ro` 在写入时返回 HTTP 403，非团队成员返回 HTTP 403：
      - **Web `rw`**：同事可在该工作区内新建、编辑、上传和删除文件（写、上传、删除在解析符号链接后二次校验路径）；删除或重命名工作区、修改配置、启停 computer、修改自动跟踪或共享设置仍仅限所有者（共享设置允许 PM）。前端 `WorkspaceSharingModal.tsx` 提供双通道三档选择器，`TeamSharing.tsx` 的 `useTeamReadOnly` 为三态；`rw` 同事仍不展示所有者专属的 Memo、分享、预览与自动发送操作。同事读取文件时，运行中的工作区直读沙箱实时文件，已停止的工作区从数据库镜像读取且绝不自动启动沙箱（`src/server/app/workspace_files/crud.py`）。
      - **Chief of Staff `ro` / `rw`**：`ro` 与 `rw` 工作区会出现在 `manage_workspaces(list)` 中并标明所有者与档位（`list_colleague_cos_workspaces`）；`ro` 支持 `agent_output`、`manage_threads(list)`、文件读取与知识层读取；`rw` 额外允许知识层写入（结论保留真实作者）与文件写入（在 `ro` 上尝试写入会返回明确的只读提示）。
  - **两个列表函数与前端 `caller_tier`**：
    - `list_team_workspaces`（Web 侧栏团队工作区列表）与 `list_colleague_cos_workspaces`（Chief of Staff 的同事工作区列表，均位于 `src/dalton/sharing.py`）：当调用方 `get_user_tier(uid) == "external"` 时直接返回空列表 `[]`；遍历行时经 `_classify_owner_row_tier` 分类所有者，`owner_tier == "unknown"` 的行跳过，`owner_tier == "external"` 的工作区对 `internal` 调用方跳过、仅对 `admin` 调用方以只读（`web_access="ro"`、`cos_access="ro"`，忽略表中的 `hidden` 或 `rw`）返回。
    - `GET /api/v1/dalton/workspaces/team`（`list_team_workspaces_endpoint` in `src/dalton/app/router.py`）返回 `{"members": members, "caller_tier": caller_tier}`；前端 `TeamWorkspacesSection`（`web/src/pages/ChatAgent/components/dalton/TeamSharing.tsx`）仅当 `data?.caller_tier` 确认为 `'internal'` 或 `'admin'` 时才渲染侧栏「团队」分组，外部成员以及查询加载中/失败时均隐藏该分组。
  - **外部成员对三个工具的拒绝**：外部成员在自己工作区内一切照常（全部 skill 与数据源可用），仅以下三个工具在入口处按 `get_user_tier(user_id) == "external"` 直接拒绝，不触碰任何数据或桥接层（`plugins/dalton/skills/dalton-conventions/SKILL.md` 与 `src/dalton/tools/knowledge_tools.py` 同步写明该边界）：
    1. `ingest_company_wiki`（`src/dalton/tools/wiki_tools.py`）：拒绝归档进公司 Wiki（含 `confirm=False` 预览，返回 `EXTERNAL_WIKI_INGEST_FORBIDDEN_MESSAGE`）；
    2. `conversation_search`（`src/dalton/tools/conversation_tools.py`）：拒绝检索团队对话归档（返回 `EXTERNAL_CONVERSATION_SEARCH_FORBIDDEN_MESSAGE`，提示用全局搜索检索自己的对话）；
    3. `publish_report`（`src/dalton/tools/publish_tools.py`）：拒绝发布正式报告（返回 `EXTERNAL_PUBLISH_FORBIDDEN_MESSAGE`；起草、`check_draft` 与 `check_language` 仍可用）。
  - **已知限制**：
    1. **同事在他人工作区发起运行留到下一期**：`rw` 同事在他人工作区发消息，或 Chief of Staff 向同事工作区的分析师派活，均返回「该功能下一期开放」（`src/server/services/turn_runtime.py`、`src/server/app/threads/messaging.py`、`src/tools/secretary/dispatch.py`；将运行计费归属发起人同时在所有者沙箱内执行留待第二期）；
    2. 若数据库记录某工作区为 `running` 但其沙箱实际已停止，同事的实时读取会走通用会话获取路径并将沙箱拉起（仅在状态不一致时发生，起自愈作用，不授予额外权限）。

### 10.3 全局搜索与前端交互增强

- **全局搜索（⌘K / Ctrl+K）**（`src/dalton/database/search.py`、`src/dalton/app/router.py`、`web/src/components/dalton/globalSearchHost.tsx`、`web/src/components/dalton/GlobalSearchModal.tsx`，钩子 `global-search`、`query-keys`）：
  - 在任意页面可通过侧栏品牌标旁按钮（`web/src/components/Sidebar/AppSidebar.tsx`）、移动端底栏（`web/src/components/BottomTabBar/BottomTabBar.tsx`）或快捷键 **⌘K / Ctrl+K** 打开居中搜索弹窗（由 `web/src/App.tsx` 单例挂载的 `GlobalSearchHost` 托管，桌面与移动端布局切换后仍保持可用）。
  - **仅搜索当前用户自己拥有的工作区**：所有查询过滤 `w.user_id = caller AND w.status != 'deleted'`（对话同时过滤 `t.archived_at IS NULL`）并在 Python 层经 `_row_owned_and_active` 二次校验；Memo 仅读取 `store.prefix = '<user_id>.memos'`；**排除同事共享给你的工作区**。
  - **后端查询与来源**（`GET /api/v1/dalton/search?q=&limit=`，少于 2 个字符返回 HTTP 422，2 个汉字即可）：所有查询参数化、经 `escape_like` 转义 `%` / `_` / `\`（`ILIKE … ESCAPE '\'`），事务内执行 `SET LOCAL statement_timeout = '5s'`。结果分**对话（conversations）**与**文档（documents）**两节，均按最新优先排序，服务端截取命中处前后约 60 字符摘要并返回按码点（code point）计算的 `highlights` 偏移：
    - **对话来源**：线程标题、`conversation_queries.content`（用户消息）及 `thread_transcript_files` 中 `user` / `assistant` 的 `text`（工具调用、工具结果与原始 JSON 绝不进入摘要；transcript 先按原词及 JSON 转义形式预筛，再按每批 200 个、最多 2000 个文件读取并在 Python 中解析、按线程去重与时间排序，每个线程仅展示最新一条命中）。
    - **文档来源**：Memo 正文、文件名与描述，以及 `workspace_files` 的文件名、路径与内联存储的 `content_text`。**已知限制**：开启对象存储时，文件正文不在数据库中，工作区文件仅匹配文件名和路径。
  - **前端导航**：点击对话跳转 `/chat/t/:threadId`；点击工作区文件或 Memo 跳转 `/chat/:workspaceId` 并在路由 state 中携带 `openFilePath`，由当前可见的 `ThreadGallery`（`web/src/pages/ChatAgent/components/ThreadGallery.tsx`）按 `location.key` 消费一次、打开文件面板并清空 state。
- **输入框草稿持久化**（`web/src/lib/dalton/chatDrafts.ts`、`web/src/components/ui/chat-input.tsx`，钩子 `composer-drafts`）：
  - 各处输入框的未发送文本按用户与会话保存在浏览器 `localStorage` 的 `dalton.chatDraft.v1:<user id>:<draft key>` 下（`buildDraftStorageKey`，涵盖线程 `thread:<id>`、工作区新对话 `ws:<id>:new`、画廊 `ws:<id>:gallery` / `home:gallery`、行情页 `market:main` / `market:panel:<threadId>` / `market:panel:<workspaceId>:<SYMBOL>`、仪表盘 `dashboard:new`）：仅保存 `{text, savedAt}`（不含附件），300 ms 防抖并在卸载或切换 key 时刷盘，超过 100 KB 不保存并删除同 key 旧草稿，7 天过期（`pruneExpired`），读取时经 Zod（`StoredChatDraftSchema`）校验，`localStorage` 不可用或已满时静默降级。
  - **仅在发送成功时删除**：`ChatInput.onSend` 支持返回 `false` 或 Promise；返回 `false` 或 reject 时（如 `MarketView` 或仪表盘 `useChatInput.handleSend` 未解析出工作区或发送抛错），原文本连同发送等待期间新输入的文本一并恢复并保存（`settleSendResult`）。发出新对话后、线程 ID 落定前继续输入的文本会从已实际发送的 `ws:<id>:new` 迁移到新线程 key；行情面板显式点「新对话」时清空对应草稿。
- **文件面板与 Memo 面板批量上传**（`web/src/pages/ChatAgent/components/FilePanel.tsx`、`web/src/pages/ChatAgent/components/MemoPanel.tsx`、`web/src/pages/ChatAgent/components/filePanel/useFileUpload.ts`，钩子 `multi-file-upload`）：
  - 文件选择器（`multiple`）与拖拽支持一次添加多个文件，按每面板单一队列串行上传，中途新加入的文件排入队尾并立即更新总文件数，界面展示「第 i / n 个文件」、文件名与进度百分比；单个文件失败（Memo 面板逐文件校验类型与 5 MB 上限）记录后继续上传其余文件，结束时汇总展示全部失败项（`uploadFailedSummary`），整批完成后仅刷新一次文件列表（Memo 面板直接调 `uploadUserMemo`）。切换工作区或组件卸载时通过代数守卫（`processingGenRef`）丢弃未开始项，已在途的上传完成后仅刷新其启动时所属的工作区列表，新工作区的文件立即启动新循环而不必等待旧请求结束。

## 11. 开发规范

- **挂钩**：
  - 修改上游文件时，必须在改动附近加 `DALTON-HOOK(<id>)` 注释，`<id>` 必须登记在 `scripts/dalton/check_hooks.sh` 的 `ALLOWED_HOOKS` 里；
  - 改动前后 25 行以内要有标记；
  - 不允许在上游目录里新增文件；
  - `check_hooks.sh --strict --audit` 必须通过。
- **基线**：
  - `scripts/dalton/pr_baseline.sh --pr N --baseline-only` 依次运行：钩子审计、Python 单测（通过 `pytest-xdist` 并行：`-n ${DALTON_BASELINE_WORKERS:-4} --dist loadgroup --durations=25`）、broker 的 Node 测试、Web 类型检查和 vitest，以及 PR 描述核查（描述里用反引号标注的路径和标识符必须在仓库里存在）；
  - 结果会作为评论发到 PR 上。
- **多 worker**：代码必须兼容多 worker 部署（当前 Dalton 的 compose 是单 worker，不带 reload：更新代码后要显式重启后端）。请求路径上不能有进程内的全局状态；状态以 Postgres 为准，Redis 只做协调。
- **测试替身**：只用 `create_autospec`，或者 `patch(..., autospec=True)`。
- **开发流程**：
  - 由 agy（或 GLM）在独立克隆里开发，grok 和 codex 分别独立审查，lead 汇总意见并负责合并、部署；
  - 合并时如果发生了三方合并，要对**所有**改动过的文件跑一遍 `ruff --select F821`。自动合并可能悄悄删掉另一侧还在用的 import，#82 就发生过。

## 12. 部署与运维

**公网入口与卡片链接**：

- 公网流量经 **Tailscale Funnel `:8443`** 转发到本地 **edge（Caddy，`127.0.0.1:8088`）**。按 `deploy/dalton/Caddyfile`，edge 只把 `/api/*`、`/ws/*` 反代到 `backend:8000`（同时剥离客户端传入的 `X-Service-Token` 与 `X-User-Id` 请求头）；`/health`、`/health/*`、`/docs`、`/redoc`、`/metrics` 等后端根路径返回 HTTP 404；其余 `GET`/`HEAD` 请求由 `/srv`（挂载生产构建产物 `web/dist`）静态提供并做 SPA 回退（`try_files {path} /index.html`）。
- `DALTON_FRONTEND_URL` 是需要在后端 `.env` 里配置的环境变量（用于推送卡片里的链接，由 `src/dalton/settings.py` 的 `get_dalton_frontend_url` 读取，且绝不回退到后端 API 地址 `SERVER_BASE_URL`）。

**桥接层双令牌与接口权限**：

宿主机 `scripts/dalton/researchdata_bridge.py`（`127.0.0.1:8960`，配置位于 `~/.config/dalton/researchdata-bridge.env`）按职责拆分两个令牌：

| 令牌环境变量 | 持有方 | 允许访问的桥接接口 |
|---|---|---|
| `DALTON_BRIDGE_TOKEN`（MCP 令牌） | 宿主机桥接层、后端容器、沙箱容器 | 仅 `POST /mcp`（`_is_valid_mcp_token`；访问管理接口会被 `_require_admin_token` 拒绝并返回 HTTP 403） |
| `DALTON_BRIDGE_ADMIN_TOKEN`（管理令牌） | 仅宿主机桥接层与后端容器（**管理令牌不得进入沙箱**） | `POST /mcp`（`_is_valid_mcp_token`），以及全部管理接口：`POST /embed`、`POST /wiki/ingest`、`POST /feishu/*`（`/feishu/doc/read`、`/feishu/wiki/nodes`、`/feishu/wiki/node`、`/feishu/doc/create`、`/feishu/doc/append`）、`GET /jobs/conversation-archive`、`POST /jobs/conversation-archive/{enable,disable,run}`、`GET /jobs/ops`、`POST /notify`（`_require_admin_token`；未配置时返回 HTTP 401） |

`GET /healthz` 无需令牌。

**更新代码**：

1. 部署用的 checkout 在 `~/Projects/dalton-langalpha`（`dalton/main`），先 fast-forward 到最新。
2. 后端（`docker-compose.dalton.yml` 中 `backend` 容器内存与 `memswap_limit` 限制为 `4g`）：
   - `src`、`scripts`、`plugins`、`mcp_servers`、`libs/market-protocol`、`workflows` 等是 bind mount 进容器的，只改了这些目录下的代码时，`docker restart dalton-langalpha-backend-1` 就够了；
   - 改了 `.env` 或 `docker-compose.dalton.yml` 时，必须执行 `docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --no-deps backend` 重建容器，`restart` 不会重新读 `env_file`。
3. 前端：`bash scripts/dalton/build_frontend_prod.sh --require-supabase`。
4. 桥接层：`launchctl kickstart -k gui/$(id -u)/com.dalton.researchdata-bridge`。
5. 数据地图：`docker exec dalton-langalpha-backend-1 uv run --no-sync python scripts/dalton/data_source_health.py`。

**数据库迁移**：容器启动时，入口脚本会依次运行上游的 alembic 和 Dalton 自己的 alembic（`src/dalton/alembic.ini`）。Dalton 迁移脚本位于 `src/dalton/migrations/versions/`，当前版本为 **`d001` 至 `d019`**：

- `d001`–`d011`：知识层版本与结论、发布请求与出版物、自主信号与预检日志、预算账本、数据源体检、对话归档卡片；
- `d012_native_model_sync`（`src/dalton/migrations/versions/d012_native_model_sync.py`）：原生订阅模型巡检与同步运行记录表；
- `d013_price_move_firings`（`src/dalton/migrations/versions/d013_price_move_firings.py`）：股价异动按交易日与方向去重账本 `dalton_price_move_firings`；
- `d014_notify_deliveries`（`src/dalton/migrations/versions/d014_notify_deliveries.py`）：自动化结果私信推送投递账本 `dalton_notify_deliveries`；
- `d015_user_coverage`（`src/dalton/migrations/versions/d015_user_coverage.py`）：每用户覆盖名单 `dalton_coverage`、动态筛选规则组 `dalton_coverage_groups`、每用户财报日历镜像表 `dalton_earnings_calendar_mirror`；
- `d016_members_and_invites`（`src/dalton/migrations/versions/d016_members_and_invites.py`）：团队成员表 `dalton_members` 与 PM 单次邀请链接表 `dalton_invites`；
- `d017_wiki_ingests`（`src/dalton/migrations/versions/d017_wiki_ingests.py`）：公司 Wiki 入库审计与状态账本 `dalton_wiki_ingests`；
- `d018_workspace_sharing`（`src/dalton/migrations/versions/d018_workspace_sharing.py`）：按工作区、按通道（`cos_access` 与 `web_access`）配置的团队共享权限表 `dalton_workspace_sharing`；
- `d019_feishu_read_scopes`（`src/dalton/migrations/versions/d019_feishu_read_scopes.py`）：对外部成员开放读取的飞书知识空间白名单表 `dalton_feishu_read_scopes`。

**健康检查**：

| 检查项 | 位置 |
|---|---|
| 后端 | `/health` |
| 供应商状态 | `/api/v1/dalton/providers/status` |
| 今日预算 | `/api/v1/dalton/budget/today` |
| 自动任务总表 | `/api/v1/dalton/system-jobs` |
| 桥接层 | `/healthz`，含 xreach 版本 |
| 数据地图 | `dalton_data_source_health` 表 |

**注意事项**：

- 以 `docker compose run` 启动的一次性容器（例如对话归档回填），会被 `docker compose restart backend` 连带重启。所以只重启后端时，用 `docker restart`。
- 数据库写操作只走脚本：先存档，在事务里执行，并断言影响的行数。

**已观察到的问题**：2026-10-23 在提交 `7263bce7` 和 `c3088a67` 上，以同一环境运行 `tests/unit/server/services`，都有同样的 7 个用例失败（自动任务结算 `precheck_skipped` 四例、livefs 一例、插件解析器两例）。原因待排查。`pr_baseline.sh` 不运行这个目录。
