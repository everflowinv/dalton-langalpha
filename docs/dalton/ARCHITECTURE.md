# Dalton 技术文档

本文描述 Dalton **当前**的实现，面向接手开发和运维的工程师。各阶段的设计和验收历史见 `docs/dalton/dev-guide/`。那些文件按阶段编写，部分内容已经被后来的实现取代；如果和本文冲突，以本文和代码为准。

## 1. 定位与边界

Dalton 是构建在开源项目 LangAlpha 之上的基金研究分析师。LangAlpha 提供：智能体运行时（主智能体、子智能体、沙箱代码执行）、对话和流式传输、工作区和文件、自动任务调度，以及 Web 界面。Dalton 在此基础上增加了以下能力：

- 基金方法论和研究 skill，以及多步 workflow；
- 每家公司的结构化知识层，带版本和校验；
- 正式发布治理：数字核验、独立审稿、PM 审批；
- 自主研究：零成本预检、唤醒、研究总监；
- 数据接入：researchdata 桥接、SEC、行情、网页、X、团队对话归档；
- 模型接入与计费：订阅制 OAuth、原生 GLM、OpenClaw broker，以及每日现金额度；
- 团队模式：邮箱白名单登录、共享凭据、只读互看工作区。

**代码归属。** Dalton 自有代码只放在 `scripts/dalton/dalton_paths.txt` 列出的路径下，主要有：

- `src/dalton/`、`plugins/dalton/`、`plugins/dalton_data/`、`workflows/`、`scripts/dalton/`、`config/dalton/`、`deploy/dalton/`、`docs/dalton/`；
- `tests/*/dalton/`；
- `agent_config.dalton.yaml`、`docker-compose.dalton.yml`；
- 少量 `web/src/...dalton...` 文件。

除此以外都是上游 LangAlpha 的代码，只允许在"挂钩点"修改，见第 11 节。

## 2. 总体结构

```
宿主机（macOS）
  ├─ OpenClaw：模型 broker（ocb-*）、researchdata MCP、xreach、web_search / x_search 网关
  ├─ researchdata 桥接  127.0.0.1:8960   scripts/dalton/researchdata_bridge.py（launchd: com.dalton.researchdata-bridge）
  ├─ web_search 桥接     127.0.0.1:8961   （launchd: com.dalton.web-search-bridge）
  └─ 对话归档（每天 21:15） conversation_archive.py run（launchd: com.dalton.conversation-archive）

Docker（colima，数据盘在 EveSSD）
  ├─ backend   FastAPI + LangGraph，127.0.0.1:8000；挂载 src、scripts、plugins、mcp_servers、migrations、libs/market-protocol、workflows、config/dalton、var/dalton
  ├─ postgres  dalton-postgres:18-pgvector
  ├─ redis
  ├─ frontend  127.0.0.1:5173（开发）；生产包 web/dist 由 edge 提供
  ├─ edge      Caddy 127.0.0.1:8088，只代理 /api/* 与 /ws/*；公网经 Tailscale Funnel
  └─ sandbox   智能体执行代码的容器（所有公司工作区共用一台"computer"）
```

## 3. 智能体与角色

- **角色**由上游 `turn_route` 按工作区决定：
  - Home 工作区是 **Chief of Staff**，兼任研究总监；
  - 公司工作区是 **analyst**。
- Dalton 的工具按角色绑定（`src/dalton/tools/__init__.py`，钩子 `dalton-tools`）：

  | 角色 | 工具 |
  |---|---|
  | Chief of Staff | `dalton_coverage_board`、`dalton_record_plan`、`dalton_agenda_note`、`dalton_schedule_automation`、`conversation_search`，以及上游的 `delegate_to_analyst` |
  | 公司分析师 | `knowledge_update`、`knowledge_append_verdict`、`check_draft`、`publish_report`、`dalton_schedule_automation`、`conversation_search` |

- **默认模型**由 `agent_config.dalton.yaml` 和 `src/dalton/dalton.yaml` 决定：

  | 用途 | 模型 |
  |---|---|
  | 主模型 | `gpt-6-astra-oauth` |
  | 后台 | `gemini-4-argon-low-oauth` |
  | assist | `ocb-google-gemini-3.8-flash` |
  | 子代理 research / data-prep / general-purpose | `ocb-xai-grok-4.7` |
  | Dalton 子代理 `dalton-evidence` | `ocb-xai-grok-4.7` |
  | Dalton 子代理 `dalton-reviewer` | `gpt-6.1-sol-oauth` |

  assist 的模型在 `agent_config.dalton.yaml` 和 `dalton.yaml` 里各配了一个，前者优先，见 `src/dalton/llm/assist.py`。
- **Skill**：共 22 个，在 `plugins/dalton/skills/`，通过 `dalton_registry.py` 合并进上游 registry。有斜杠命令的会出现在输入框的 `/` 菜单里。
- **Workflow**：共 6 个，在 `workflows/<name>/workflow.js`：initial-coverage、full-coverage、bullbear-doc、earnings-deep-update、weekly-coverage-review、topic-research。它们没有自己的斜杠命令，由对应的 skill 通过 `RunWorkflow` 启动。

## 4. 知识层

每个公司工作区有 8 份结构化文档，存在 `data/<doc>.json`：company、thesis、debate、drivers、management、lessons、agenda、models。其中 6 份会渲染成根目录的 `.md` 文件；`verdicts.md` 由数据库渲染生成。路径定义在 `src/dalton/knowledge/paths.py`，schema 在 `src/dalton/knowledge/schemas/`。

- **写入**：
  - `knowledge_update`：带版本号的乐观锁，结构化 ops，按 JSON Schema 校验。失败时，返回信息里带有期望的条目结构。
  - `knowledge_append_verdict`：记录五词结论之一（NO_CHANGE / THESIS_STRENGTHENED / THESIS_WEAKENED / THESIS_BROKEN / NEW_THESIS），同时更新议程，并关闭对应的自主信号。
- **硬性规则**（`src/dalton/knowledge/validate.py`）：
  - thesis 的 evidence 只能追加；
  - `thesis.official` 只能通过发布来写；
  - house view 的目标价必须指向 `models.json` 里登记过的模型单元格。
- **版本**：每个版本留一份快照 `history/<doc>/<UTC>-vNNNN.json`。数据库表 `dalton_knowledge_versions` 不允许删除，也不允许改内容；`dalton_verdicts` 不允许修改。
- **手动修改**：用户直接改了文件，回合结束后系统会检测到，下一回合自动同步（`src/dalton/knowledge/reconcile.py`，钩子 `knowledge-reconcile`）。

## 5. 发布治理

`publish_report`（`src/dalton/tools/publish_tools.py`）对应的状态机在 `src/dalton/publish/service.py`，状态保存在 `dalton_publish_requests` 表：

1. **数字核验**：报告里的每个数字，都要对照 thesis 的证据、模型单元格、来源记录和事实底表，最后由 LLM 判定。只要有未核实或互相矛盾的数字，状态就是 `audit_failed`。
2. **独立审稿**：审稿人从 `publish.reviewer_models`（`src/dalton/dalton.yaml`）里挑，必须和作者属于不同的模型家族（`src/dalton/publish/review.py`），按 7 项检查。审稿意见为"需修改"时，状态是 `review_revise`。
3. **PM 审批**：
   - 交互式线程里会弹出审批卡片（`PublishApprovalCard.tsx`，钩子 `publish-card`）；
   - 自动任务里不打断，状态停在 `awaiting_pm`，并在议程里加一项待审批；
   - 只有 `DALTON_PM_USER_IDS` 里的用户能批准。
4. **一次批准对应一次发布**：批准之后，草稿内容的 sha256 一旦变化，状态就变成 `superseded`。
5. **写入**：
   - 生成不可修改的文件 `published/<日期>-<slug>.md`，以及可选的 `.xlsx`；
   - `dalton_publications` 和 `dalton_publication_files` 两张表不允许修改；
   - 如果设置了 `updates_thesis`，就写入 `thesis.official`。

`check_draft` 只做核验、不拦截，结果写到 `drafts/<slug>.audit.md`。

## 6. 自主研究

**挂钩点**：上游的自动任务执行器在启动沙箱之前，会对 `dalton-*` 任务调用 `preflight.decide()`（`src/dalton/autonomy/preflight.py`）：

- 没有信号：任务以 `PRECHECK_SKIPPED` 结束，几乎不花钱；
- 有信号：注入 `<dalton_wake>` 指令，唤醒分析师，并要求它最后用 `knowledge_append_verdict` 收尾。

**探针**（`src/dalton/autonomy/probes/`）：

| 探针 | 检查什么 |
|---|---|
| `sec` | SEC EDGAR |
| `cnhk` | 巨潮公告，覆盖 A 股和纯港股，标题匹配做了繁简归一 |
| `internal` | 销售邮件、AlphaEngine、Guidepoint、电话会转写有没有新文档 |
| `agenda` | 议程里到期的验证点和事件 |
| `earnings` | 是否处于 T-3、T0、T+1 窗口 |
| `calendar`、`director` | 给 Home 的任务用 |

- 每个探针各自按时间退避，间隔依次为 2 分钟、10 分钟、30 分钟、2 小时。
- 信号存在 `dalton_signals` 表里去重，7 天过期。每次决策记录在 `dalton_preflight_log`。
- 10-K、10-Q、8-K（Item 2.02）、巨潮 P1、议程、财报、价格这几类信号**直接唤醒**；其他信号先合并起来，用一次 assist 模型做分诊。所以严格说，预检并不是"零 token"。

**标准任务**（`src/dalton/autonomy/schedule.py`）：

- 每家公司：
  - `dalton-<t>-watch`：核心标的在交易日 06:00–20:00 每 30 分钟一次；观察名单每天 07:30 一次。
  - `dalton-<t>-delta`：21:30。
  - `-move-up` / `-move-down`：相对前收盘涨跌超过 ±4% 时触发，冷却 24 小时。
  - 时区：A 股、港股用 Asia/Shanghai，其余用 America/New_York。
- 财报的一次性任务：`-earn-<p>-tm3`、`-t0`、`-tp1`。
- Home：`dalton-earnings-calendar`（周六 09:00）、`dalton-director-daily`（交易日 07:30）、`dalton-director-weekly`（周六 10:00），时区是 `autonomy.timezone`。
- 建立方式：脚本 `scripts/dalton/bootstrap_watch.py`，或者工具 `dalton_schedule_automation`。cron 任务必须带 `next_run_at`，因为调度器只认领 `next_run_at` 不为空且已经到时间的行。见 #88。

**研究总监**：`dalton_coverage_board` 给覆盖池打分，分数 = 权重 × 紧急度 × 多久没更新；`dalton_record_plan` 拒绝当天预算放不下的计划，并把计划写到 `dalton/plans/<日期>.md`。

## 7. 预算与计费

- 只有一个约束：**每人每日的现金额度**，外加可选的单任务上限。不限调用次数，也不限重试次数（`src/dalton/budget/`）。
  - 默认每天 100 美元，按 Asia/Shanghai 的自然日计算。
  - 剩余不足 0.25 美元时拒绝新请求，返回 HTTP 429。
  - 额度按每块 1 美元预留，租期 300 秒。
- **只有现金花费计入额度。** `*-oauth` 模型和标记为 subscription 的模型只记名义成本。Coding Plan 的 GLM 配了价格为 0 的价格卡，这样每条计费路径算出来的现金都是 0。
- 预检发现额度用完时，跳过本次唤醒；一次性任务顺延一天。
- 相关代码：钩子 `budget-gate`、`budget-admission`；接口 `/api/v1/dalton/budget*`；设置页的 `DaltonBudgetCard`。

## 8. 模型接入

模型接入以 `src/dalton/llm/native_overlay.json` 为准，它被合并进上游模型目录（钩子 `llm-overlay`），`dalton_meta` 字段标明每个模型的家族和计费方式。

| 接入方式 | 供应商 | 说明 |
|---|---|---|
| 订阅 OAuth（上游） | `codex-oauth`、`claude-oauth` | |
| 订阅 OAuth（Dalton） | `antigravity-oauth` | Google 账号；Gemini，以及经它转接的 Claude、GPT-OSS |
| 订阅 OAuth（Dalton） | `muse-oauth` | 登录后向 `/muse-code/key` 换发模型 API key；会话头和 Muse Code CLI 1.4.12 一致 |
| 订阅 OAuth（Dalton） | `xai-oauth` | SuperGrok：`auth.x.ai` 设备码登录，带 Redis 锁的刷新，经 `cli-chat-proxy.grok.com` 调用。`x-grok-client-version` 由 `DALTON_XAI_GROK_CLIENT_VERSION` 指定，过旧会收到 426 |
| 订阅 API key | `z-ai-cn-coding` | 智谱 GLM Coding Plan，Anthropic 协议；模型 `glm-5.3-plan`、`glm-5.3-flash-plan`，默认思考强度 low。只认自己名下的 key，不会回落到按量计费的父供应商。频率限制较严，批量调用时要低并发并退避 |
| OpenClaw broker | `openclaw-broker` | `ocb-*`，`http://{HOST_IP}:18789/dalton-broker/v1`，用 `scripts/dalton/sync_openclaw_models.py` 同步 |
| 现金 | `deepseek`、`xai`（API key）等 | |

**角色策略**（`src/dalton/llm/role_policy.py`）：

- contributor 变体在所有角色里都禁用；
- 主角色和子代理必须支持 tool calling；
- 审稿人必须和作者属于不同家族；
- 可以通过 `models.disabled_providers` 整体关掉某个供应商。

**团队凭据**：在 `credentials.sharing: team` 并设置了 `DALTON_PM_USER_IDS` 时，所有凭据都解析到第一位 PM（`src/dalton/credentials.py`）。

## 9. 数据接入

**researchdata 桥接**（`scripts/dalton/researchdata_bridge.py`）在宿主机上运行，只用 Python 标准库：

- 通过 stdio 包装 OpenClaw 的 `researchdata_mcp.py`，对外提供 `POST /mcp`（需要 token）、`GET /healthz`、`POST /embed`。
- 工具有 `catalog`、`research`、`finance`、`macro`、`community`。智能体在沙箱里通过 `execute_code` 调用。
- 以下路线由桥接层自己执行：
  - xueqiu、employee、last30days；
  - x-timeline：只读地调用 OpenClaw 已登录的 `xreach`，只放行 tweets、tweet、thread、search，并会转交代理。
- 参数处理：
  - `offset` 和 `max_chars` 会被截到网关允许的范围；
  - 传错位置的 document id 会被映射或给出提示；
  - transcript 路线的主题词查询会被丢弃，只用 ticker 去查。

**数据地图**：可用性和说明以 `src/dalton/data_map/sources.yaml` 为准。

- 每天体检一次（`src/dalton/data_map/health.py`），结果写进 `dalton_data_source_health`，再渲染进提示词。
- 资料库探测"最新一条"的方式：
  - tmtb、funda-ai、company-wiki 用按时间排序的 `list`；
  - alphaengine、guidepoint、sales-notes 用 30 天窗口搜索；transcript 用 120 天窗口，因为电话会按季度开；
  - `all` 取它实际查询的六个来源中最新的日期；
  - last30days 调用成本高，不做自动探测，状态固定为 `unprobed`。
- 港股公告走 `cn-hk` 路线的 `filings` 操作（巨潮的 hke 栏目），覆盖率大约 95%；仅英文版的公告和股东大会投票结果，退回 `site:hkexnews.hk` 搜索。

**其他数据入口**：

| 入口 | 说明 |
|---|---|
| `dalton_sec` | stdio MCP；XBRL 期间归一，可选按原始披露或按最新重述取数 |
| 行情 | `get_quote` 等，盘前盘后数据在 `src/dalton/extended_hours.py` |
| 网页搜索 | `search_api: openclaw`，经 web_search 桥接 |
| 网页读取 | `fetch_chain: ["inhouse", "firecrawl"]`：自家抓取失败的 URL 才交给 Firecrawl（`FIRECRAWL_API_KEY`，和 OpenClaw 共用账号） |
| `x_search` | 经桥接层转到 OpenClaw 网关 |

## 10. 团队对话归档

**导出**：`scripts/dalton/conversations/export_openclaw.py` 只读取 OpenClaw 的 SQLite（使用 backup API）。

- 只保留真人会话，丢弃 cron、heartbeat、子代理和测试会话；
- 只取文本，并做脱敏。

**处理流程**：`scripts/dalton/conversation_archive.py` 提供 `run` 和 `backfill` 两个命令。

- 分段：间隔超过 30 分钟就切开，每段最多 6000 字、25 个事件；
- 每段抽取成原子化的**卡片**，每张卡片包含类型、claim、原话引用、数字、ticker、置信度；
- 程序会做兜底校验：数字、期间、币种、专名、结论词、ticker；
- 向量化后存入 `dalton_conversation_cards`（pgvector），用 `content_hash` 去重。

**使用**：分析师和 Chief of Staff 都可以用 `conversation_search` 检索卡片。检索方式是向量、trigram 和 RRF 混合排序，带 180 天半衰期的时间衰减。卡片还会按天渲染成文件：只涉及一家公司、并且这家公司已有工作区的卡片，写到该工作区的 `conversations/<日期>.md`；跨公司主题的卡片，以及还没有工作区的公司的卡片，写到 Home 的 `research/topics/conversations/<日期>.md`（`src/dalton/conversations/render.py`）。

**调度**：每天 21:15 由 launchd 运行（宿主机上的脚本）。抽取版本由 `conversations.prompt_version` 控制。

## 11. 开发规范

- **挂钩**：
  - 修改上游文件时，必须在改动附近加 `DALTON-HOOK(<id>)` 注释，`<id>` 必须登记在 `scripts/dalton/check_hooks.sh` 的 `ALLOWED_HOOKS` 里；
  - 改动前后 25 行以内要有标记；
  - 不允许在上游目录里新增文件；
  - `check_hooks.sh --strict --audit` 必须通过。
- **基线**：
  - `scripts/dalton/pr_baseline.sh --pr N --baseline-only` 依次运行：钩子审计、Python 单测、broker 的 Node 测试、Web 类型检查和 vitest，以及 PR 描述核查（描述里用反引号标注的路径和标识符必须在仓库里存在）；
  - 结果会作为评论发到 PR 上。
- **多 worker**：后端以多个 worker 运行。请求路径上不能有进程内的全局状态；状态以 Postgres 为准，Redis 只做协调。
- **测试替身**：只用 `create_autospec`，或者 `patch(..., autospec=True)`。
- **开发流程**：
  - 由 agy（或 GLM）在独立克隆里开发，grok 和 codex 分别独立审查，lead 汇总意见并负责合并、部署；
  - 合并时如果发生了三方合并，要对**所有**改动过的文件跑一遍 `ruff --select F821`。自动合并可能悄悄删掉另一侧还在用的 import，#82 就发生过。

## 12. 部署与运维

**更新代码**：

1. 部署用的 checkout 在 `~/Projects/dalton-langalpha`（`dalton/main`），先 fast-forward 到最新。
2. 后端：
   - `src`、`scripts`、`plugins`、`mcp_servers`、`libs/market-protocol` 等是 bind mount 进容器的，只改了这些目录下的代码时，`docker restart dalton-langalpha-backend-1` 就够了；
   - 改了 `.env` 时，必须执行 `docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --no-deps backend` 重建容器，`restart` 不会重新读 `env_file`。
3. 前端：`bash scripts/dalton/build_frontend_prod.sh --require-supabase`。
4. 桥接层：`launchctl kickstart -k gui/$(id -u)/com.dalton.researchdata-bridge`。
5. 数据地图：`docker exec dalton-langalpha-backend-1 uv run --no-sync python scripts/dalton/data_source_health.py`。

**数据库迁移**：容器启动时，入口脚本会依次运行上游的 alembic 和 Dalton 自己的 alembic（`src/dalton/alembic.ini`）。

**健康检查**：

| 检查项 | 位置 |
|---|---|
| 后端 | `/health` |
| 供应商状态 | `/api/v1/dalton/providers/status` |
| 今日预算 | `/api/v1/dalton/budget/today` |
| 桥接层 | `/healthz`，含 xreach 版本 |
| 数据地图 | `dalton_data_source_health` 表 |

**注意事项**：

- 以 `docker compose run` 启动的一次性容器（例如对话归档回填），会被 `docker compose restart backend` 连带重启。所以只重启后端时，用 `docker restart`。
- 数据库写操作只走脚本：先存档，在事务里执行，并断言影响的行数。

**已知问题**：`tests/unit/server/services` 下有 7 个用例在主分支上本来就失败（自动任务结算、livefs、插件解析器），跟 Dalton 的改动无关，有待单独排查。
