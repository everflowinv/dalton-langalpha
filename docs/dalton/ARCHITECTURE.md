# Dalton 技术文档

本文描述 Dalton **当前**的实现，面向接手开发和运维的工程师。各阶段的设计和验收历史见 `docs/dalton/dev-guide/`。那些文件按阶段编写，部分内容已经被后来的实现取代；如果和本文冲突，以本文和代码为准。

## 1. 定位与边界

Dalton 是构建在开源项目 LangAlpha 之上的基金研究分析师。LangAlpha 提供：智能体运行时（主智能体、子智能体、沙箱代码执行）、对话和流式传输、工作区和文件、自动任务调度，以及 Web 界面。Dalton 在此基础上增加了以下能力：

- 基金方法论、23 个研究 skill 与 6 个多步 workflow（带完成契约门禁 `dalton_check_done` 与断点续跑）；
- 每位用户独立的覆盖名单与动态筛选规则组（由 Chief of Staff 管理），以及每用户独立的财报日历；
- 每家公司的结构化知识层，带乐观锁版本号、JSON Schema 校验与直接编辑调和；
- 正式发布治理：数字核验、两轮语言审查（只标不改）、异族模型独立审稿、PM 审批，以及发布时由智能体渲染 HTML；
- 自主研究：预检与探针唤醒、股价异动按交易日单向触发、研究总监排期，以及自动化结果经飞书卡片 2.0 / Discord 私信推送；
- 数据接入：researchdata 桥接（MCP 令牌与管理令牌分离）、SEC XBRL、`yfinance` 优先与 FMP 共享额度熔断、盘前盘后行情标注、网页、X、团队对话归档；
- 模型接入与计费：订阅制 OAuth、原生 GLM、原生订阅模型巡检/同步/退役、OpenClaw broker，以及每日现金额度；
- 团队模式与安全边界：邮箱白名单登录、共享模型凭据、只读互看工作区；隐藏交易与券商连接功能（保留仪表盘「持仓」视图），后端拒绝券商开关与交易权限变更且不向智能体绑定任何下单工具。

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
  │    └─ 管理接口：POST /embed、GET|POST /jobs/conversation-archive*、GET /jobs/ops、POST /notify（_require_admin_token：仅校验 DALTON_BRIDGE_ADMIN_TOKEN）
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
  | Chief of Staff（Home） | `dalton_coverage`（管理每用户覆盖名单与动态规则组）、`dalton_coverage_board`、`dalton_record_plan`、`dalton_agenda_note`、`dalton_watch_event`、`dalton_resolve_signals`、`dalton_schedule_automation`、`dalton_coverage_tracking`、`dalton_system_jobs`、`conversation_search`、`check_language`（`allow_home_workspace=True`，省略工作区或传 `"home"` 时由 `resolve_caller_home_workspace_id` 解析到调用方 Home 工作区）、`dalton_check_done`（限于 `weekly-coverage-review` 与 `dalton-earnings-calendar`），以及上游协调工具 `delegate_to_analyst`、`manage_workspaces`、`manage_threads`、`agent_output` |
  | 公司分析师（公司工作区） | `knowledge_update`、`knowledge_append_verdict`、`dalton_check_done`、`check_draft`、`check_language`、`publish_report`、`dalton_schedule_automation`、`dalton_coverage_tracking`、`dalton_system_jobs`（范围锁定在当前公司工作区）、`conversation_search` |
  | 子智能体（`general-purpose`） | 在上游工具外额外绑定 `check_language`（`src/ptc_agent/agent/agent.py`，钩子 `dalton-subagent-tools`） |

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
  - workflow 每次运行的目录为 `.agents/threads/<short_thread_id>/workflows/<run_task_id>/`（`workflow/driver.py` 的 `run_dir`），各派发步骤的日志保存在运行目录的 `journal/<label>.json`（`src/dalton/workflows/journal.py` 的 `WorkflowJournal`，通过 `write_progress` 在工作区写入进度快照）。阻断写法因 workflow 而异：以 `full-coverage` 为例，`branchArtifacts`、`probeBranchFiles`（派发只读 `dalton-evidence` 子代理核对分支产物文件存在、非空白且达到最小字节数）与 `gateBlock` 在并行取证分支失败或缺产物时返回 `ok: false`、列出 `blocked` / `incomplete` 并附 `resume_hint`，阻断后续阶段；建模步骤失败则不阻断，继续写 memo 并在相应章节写明模型缺口。`initial-coverage`、`bullbear-doc`、`topic-research`、`earnings-deep-update` 用各自内联的 `blocked` 返回阻断；`weekly-coverage-review` 探测失败时不阻断：单家公司文件缺失时跳过该公司的 call，核验子任务整体失败时本轮不派任何 call 并标 `unverified_by_probe`；两种情况都继续生成周会稿，最终返回 `incomplete`。续跑时需向 `RunWorkflow` 显式传入原任务的 `resume` 参数（`workflow/tool.py` 的 `create_run_workflow_tool`，经 `resolve_resume_source` 与 `claim_resume` 校验并认领源运行目录），驱动层通过 `WorkflowJournal.find_replayable` 与 `digest_paths` 复用 `journal/<label>.json` 中 `status="ok"` 且磁盘产物摘要未变的已完成步骤，只重跑失败或未完成的部分。
- **交付物格式**（`src/ptc_agent/agent/prompts/templates/components/user_profile.md.j2` 钩子 `html-md-canonical`）：提示词要求智能体将日常研究笔记与报告草稿保持为 Markdown（`.md`），不在起草或日常研究阶段生成 HTML；仅当用户的输出格式偏好为 HTML（`agent_preference.output_format == "html"`，且为主智能体）时，在 `publish_report` 接受定稿（到达 `awaiting_pm` 或 `published`）后，由智能体按提示词在草稿同目录渲染一份同名 `drafts/<slug>.html`（并非发布接口自动生成）。

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

`publish_report`（`src/dalton/tools/publish_tools.py`）对应的后端状态机 `run_publish` 在 `src/dalton/publish/service.py`，状态保存在 `dalton_publish_requests` 表（`run_publish` 本身执行数字核验、独立审稿、PM 审批、消费批准与落盘，不包含语言审查与 HTML 渲染；语言审查是 skill/workflow 的流程要求，由 `dalton_check_done` 契约门禁核对）：

1. **数字核验**（`src/dalton/publish/audit.py`、`src/dalton/publish/numbers.py`、`src/dalton/publish/match.py`）：
   - 先做确定性匹配（`match_figures`），对照知识层（`thesis` 证据、`drivers` 历史读数）、`models` 登记的 Excel 模型单元格、来源记录和事实底表（`data/fact_base.json`、`bg_data/<branch>.json`、`lenses_work/<lens>.json`）。
   - **引用定位**（`parse_footnotes`、`parse_facts_ledger` in `src/dalton/publish/numbers.py`）：识别数字附近的 Markdown 脚注 `[^id]` 以及正文内联引用 `[引用N]` / `[回报引用N]`（`_FOOTNOTE_REF_RE`、`_CITATION_REF_RE`），对照文末引用定义或来源表把对应证据源排在匹配首位。
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

- **单公司跟踪管理**（`src/dalton/autonomy/tracking.py`）：汇总工作区下 `watch`、`delta`、`move-up`、`move-down` 及 `earn-*` 任务的 `enabled` / `disabled` / `incomplete` 状态，支持按公司一键启停（通过 `metadata.paused_by_user` 区分用户主动暂停与待补建任务）及档位切换重排期（`set_tier_and_resync`）。
- **四域自动任务总表**（`src/dalton/autonomy/system_jobs.py`）：将四个域统一暴露给 REST 接口（`/api/v1/dalton/system-jobs*`、`/api/v1/dalton/workspaces/{id}/tracking*`）、前端「设置 → 模型」中的自动任务卡片（`web/src/pages/Settings/panels/dalton/DaltonSystemJobsCard.tsx`）以及工具 `dalton_coverage_tracking` 和 `dalton_system_jobs`：
  - `company`：单公司跟踪任务（`watch`、`delta`、`move-up`、`move-down`、`earn-*`）；
  - `home`（`HOME_JOB_TITLES`）：`dalton-earnings-calendar`（财报日历扫描）、`dalton-director-daily`（研究总监每日晨会）、`dalton-director-weekly`（研究总监每周复盘）、`dalton-data-health`（数据地图每日体检）；
  - `host`（`_fetch_host_conversation_archive_job`）：宿主机 `conversation-archive`（团队对话归档，经桥接层 `/jobs/conversation-archive*` 查询、开关与立即运行）；
  - `ops`（`DEFAULT_OPS_JOBS`、`_fetch_ops_jobs`）：宿主机 launchd 运维任务 `ops-db-backup`（数据库备份）、`ops-log-rotate`（日志轮转）、`ops-backend-watchdog`（后端看门狗），经桥接层 `GET /jobs/ops` 只读查询状态。

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

## 9. 数据接入

**researchdata 桥接与双令牌隔离**（`scripts/dalton/researchdata_bridge.py`、`src/dalton/settings.py`）在宿主机上运行，只用 Python 标准库：

- 通过 stdio 包装 OpenClaw 的 researchdata MCP 服务，使用**两个独立令牌**隔离沙箱数据调用与宿主机管理操作：
  - **MCP 令牌（`DALTON_BRIDGE_TOKEN`）**：仅能访问 `POST /mcp`（通过 `Authorization: Bearer` 或 `X-Dalton-Bridge-Token` 请求头传递，由 `_is_valid_mcp_token` 校验；旧 `/mcp/<token>` 路径仅作兼容保留）。该令牌传给后端和沙箱供 `execute_code` 调用 `catalog`、`research`、`finance`、`macro`、`community`；拿它访问任何管理接口都会被 `_require_admin_token` 拒绝并返回 HTTP 403。
  - **管理令牌（`DALTON_BRIDGE_ADMIN_TOKEN`）**：除同样能访问 `POST /mcp`（`_is_valid_mcp_token` 同时接受两种令牌）外，可访问 `POST /embed`（向量化）、`GET /jobs/conversation-archive`、`POST /jobs/conversation-archive/{enable,disable,run}`（宿主机对话归档开关与触发）、`GET /jobs/ops`（宿主机 launchd 任务状态）以及 `POST /notify`（飞书卡片 2.0 与 Discord 私信发送）。未配置时管理接口 fail-closed 返回 HTTP 401。该令牌只配置在宿主机桥接层与后端容器内，**管理令牌不得进入沙箱**：`src/ptc_agent/core/sandbox/ptc_sandbox.py`（`_build_sandbox_env_vars`）以及 `sanitize_sandbox_env_vars` / `sanitize_mcp_client_config_server`（`src/dalton/settings.py`）会剥离任何包含管理令牌的环境变量或 MCP 配置项。
  - `GET /healthz` 无需鉴权。
- 以下路线由桥接层自己执行：
  - `xueqiu`、`employee`、`last30days`；
  - `x-timeline`：只读地调用 OpenClaw 已登录的 `xreach`，只放行 `tweets`、`tweet`、`thread`、`search`，并会转交代理。
- 参数处理：
  - `offset` 和 `max_chars` 会被截到网关允许的范围；
  - 传错位置的 document id 会被映射或给出提示；
  - `transcript` 路线的主题词查询会被丢弃，只用 ticker 去查。

**行情与财务数据：yfinance 优先、FMP 按需与共享额度熔断**：

- **`yfinance` 优先与 FMP 兜底**（`FallbackFinancialSource` in `src/dalton/market_data_fallback.py`，钩子 `market-data-fallback`）：公司概况、实时报价、利润表、现金流量表、关键指标、财务比率、股价表现、分析师目标价与评级、历史 EPS、股票筛选与搜索（`_YFINANCE_FIRST`）一律**优先调用 `yfinance`**，仅在抛错、返回空或价格为 0 时回退 FMP；**只在 `yfinance` 不提供的接口上直连 FMP**（`_FMP_ONLY`：分部收入 `get_revenue_by_segment` 与指定历史日期的板块表现 `get_sector_performance(target_date=...)`）。
- **共享 FMP 额度熔断**（`src/data_client/fmp/fmp_client.py`，钩子 `fmp-quota-breaker`）：任一 worker 收到 FMP HTTP 429 或 HTTP 200 `"Limit Reach"` 正文时，`trip_quota_marker` 在 Redis 写入 `fmp:quota_exhausted`，过期时间对齐到下一个美东 15:00（`next_quota_reset`）或响应头 `x-ratelimit-reset` / `retry-after`；熔断生效期间所有 worker 的 `FMPClient._make_request` 直接抛 `FmpQuotaExhausted` 短路，不再发网络请求（HTTP 402 套餐限制只记单次失败，不触发全员熔断）。
- **每进程 yfinance SQLite 缓存隔离**（`configure_process_cache` in `src/dalton/yfinance_cache.py`，钩子 `yfinance-cache`）：将 `yfinance` 的时区、cookie 和 ISIN SQLite 缓存目录设为 `/tmp/langalpha-yfinance/<pid>` 并在 `fork()` 后重置连接单例，消除多进程并发写 WAL 导致的 `database is locked`。
- **盘前盘后与休市报价标注**（`src/dalton/extended_hours.py`）：美股在盘前、盘后及休市窗口（按 `XNYS` 交易日历判定）从 `ticker.info` 补齐盘前盘后价与涨跌幅（`enrich_yfinance_snapshot_extended_hours`），并通过 `format_quote_change_parts` / `snapshot_quote_time_labels` 区分常规收盘（标注 `close MM-DD` 或 `last close MM-DD`）与盘前盘后（标注 `vs prev close $X` 与 `@ HH:MM ET`），防止把上一交易日收盘标成 `today`。

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

## 10. 团队对话归档

**导出**：`scripts/dalton/conversations/export_openclaw.py` 只读取 OpenClaw 的 SQLite（使用 backup API）。

- 只保留真人会话，丢弃 cron、heartbeat、子代理和测试会话；
- 只取文本，并做脱敏。

**处理流程**：`scripts/dalton/conversation_archive.py` 提供 `run` 和 `backfill` 两个命令；当更换抽卡模型或提示词版本需要全量重抽时，使用 `scripts/dalton/reextract_conversations.py`（正常流程 `plan` → `extract` → `apply` → `render`：新卡先暂存到运行目录的 `STAGED_FILE`，`apply` 在单事务内原子替换旧卡并保留完整快照；出错时用 `rollback` 删除本轮新卡、恢复旧卡，之后再跑一次 `render`）。

- 分段：间隔超过 30 分钟就切开，每段最多 6000 字、25 个事件；
- 每段抽取成原子化的**卡片**（`src/dalton/conversations/extractor.py`），每张卡片包含类型、claim、原话引用、数字、ticker、entities、置信度；
- 程序做确定性兜底校验：原话子串核对（`verify_verbatim_quote`）、数字与期间/币种核对、专名与禁用结论词检查、未上市私企分流至 `entities`、券商渠道词过滤，以及结合每用户覆盖名单别名表（`get_user_aliases_sync` + `SUPPLEMENTAL_TICKER_ALIASES`）的股票代码归一与双上市回填；
- 经宿主机桥接层 `POST /embed`（携带 `DALTON_BRIDGE_ADMIN_TOKEN`）向量化后存入 `dalton_conversation_cards`（pgvector），用 `content_hash` 去重。

**使用**：分析师和 Chief of Staff 都可以用 `conversation_search` 检索卡片。检索方式是向量、trigram 和 RRF 混合排序，时间衰减的半衰期参数为 180 天，权重最低不低于 0.7。卡片还会按天渲染成文件：只涉及一家公司、并且这家公司已有工作区的卡片，写到该工作区的 `conversations/<日期>.md`；跨公司主题的卡片，以及还没有工作区的公司的卡片，写到 Home 的 `research/topics/conversations/<日期>.md`（`src/dalton/conversations/render.py`）。

**调度**：每天 21:15 由 launchd 运行（宿主机上的脚本），状态可通过桥接层 `/jobs/conversation-archive` 在设置页查看和开关。

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
| `DALTON_BRIDGE_ADMIN_TOKEN`（管理令牌） | 仅宿主机桥接层与后端容器（**管理令牌不得进入沙箱**） | `POST /mcp`（`_is_valid_mcp_token`），以及全部管理接口：`POST /embed`、`GET /jobs/conversation-archive`、`POST /jobs/conversation-archive/{enable,disable,run}`、`GET /jobs/ops`、`POST /notify`（`_require_admin_token`；未配置时返回 HTTP 401） |

`GET /healthz` 无需令牌。

**更新代码**：

1. 部署用的 checkout 在 `~/Projects/dalton-langalpha`（`dalton/main`），先 fast-forward 到最新。
2. 后端：
   - `src`、`scripts`、`plugins`、`mcp_servers`、`libs/market-protocol`、`workflows` 等是 bind mount 进容器的，只改了这些目录下的代码时，`docker restart dalton-langalpha-backend-1` 就够了；
   - 改了 `.env` 或 `docker-compose.dalton.yml` 时，必须执行 `docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --no-deps backend` 重建容器，`restart` 不会重新读 `env_file`。
3. 前端：`bash scripts/dalton/build_frontend_prod.sh --require-supabase`。
4. 桥接层：`launchctl kickstart -k gui/$(id -u)/com.dalton.researchdata-bridge`。
5. 数据地图：`docker exec dalton-langalpha-backend-1 uv run --no-sync python scripts/dalton/data_source_health.py`。

**数据库迁移**：容器启动时，入口脚本会依次运行上游的 alembic 和 Dalton 自己的 alembic（`src/dalton/alembic.ini`）。Dalton 迁移脚本位于 `src/dalton/migrations/versions/`，当前版本为 **`d001` 至 `d015`**：

- `d001`–`d011`：知识层版本与结论、发布请求与出版物、自主信号与预检日志、预算账本、数据源体检、对话归档卡片；
- `d012_native_model_sync`（`src/dalton/migrations/versions/d012_native_model_sync.py`）：原生订阅模型巡检与同步运行记录表；
- `d013_price_move_firings`（`src/dalton/migrations/versions/d013_price_move_firings.py`）：股价异动按交易日与方向去重账本 `dalton_price_move_firings`；
- `d014_notify_deliveries`（`src/dalton/migrations/versions/d014_notify_deliveries.py`）：自动化结果私信推送投递账本 `dalton_notify_deliveries`；
- `d015_user_coverage`（`src/dalton/migrations/versions/d015_user_coverage.py`）：每用户覆盖名单 `dalton_coverage`、动态筛选规则组 `dalton_coverage_groups`、每用户财报日历镜像表 `dalton_earnings_calendar_mirror`。

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
