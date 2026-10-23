# PS：分析师 skill、workflow 与自动任务开发指南（P2–P5）

版本 0.3，2026-10-18。基座 LangAlpha main `8e0dce94`，分支 `dalton/main`。上位文档：`docs/dalton/blueprint.md`（已批准，§4.2 职责与机制对应、§4.5 skill 家族表）。

**修订记录 v0.3（2026-10-18，按 owner 与 lead 决定统一修订）**
- 基金 wiki 只读：删除 PS.14 的 wiki 回写、1.6 表的“wiki 回写”行和 PS.17 的“发布后回写 wiki”；成果只留在工作区知识层和 `published/`。
- 合规已定：专家访谈、管理层交流和 Guidepoint 检索结果都可以用，标专家身份不写姓名，Guidepoint 原话不超过 20 个英文词。
- researchdata 超时改为按路由：桥默认 240 秒，长文（AlphaEngine 全文、Guidepoint、大型 filing）在调用方预算允许时最长 600 秒；LangAlpha 侧这个 server 的单次调用预算默认 300 秒。超时只用来兜住挂起，删掉“timeout ≤45”的写法。
- 自动任务文件名与 PK 对齐：价格触发改为 `dalton-<t>-move-up.json` / `dalton-<t>-move-down.json`；新增的 `dalton-<t>-delta.json`、`dalton-earnings-calendar.json` 由 PK 预检识别。Home 给公司工作区写自动任务走后端 REST helper `PUT /api/v1/dalton/automations/{name}`；分析师对自己的 `dalton-*` 自动任务文件有常设授权；研究总监派单 `auto_approve_handoffs` 已开。
- 财报日期只认公司官方来源；FMP 和其他日历只作“待确认”候选（`date_status: estimated`）；每周六（Asia/Shanghai）刷新，与 PK.19 一致。
- 每晚 thesis 增量的预检用 PK.16 的后端 `consensus` 探针（经 researchdata 桥，零 LLM 调用），删除“便宜模型跑第一步”的备选。
- 事件五词 JSON 统一为 `NO_CHANGE`、`THESIS_STRENGTHENED`、`THESIS_WEAKENED`、`THESIS_BROKEN`、`NEW_THESIS`；md 和界面显示无变化、强化、削弱、证伪、新论点。
- `publish_report` 新增 kind `bullbear`，`bullbear-doc` 用它发布。
- PK 简化：直接修改知识文件不再被拦截或还原，改为本轮结束后检测并记成 `direct_edit` 版本，不合 schema 时给分析师加警告；草稿永不被阻止，核验只在发布时卡一次。各步相应措辞已改。
- 模型键统一为 `ocb-<provider>-<model>`；CLI gateway 类模型只做纯文本角色（compaction、后台摘要、审稿人），不给用工具的子智能体；审稿家族由 `role_policy.family_of()` 判定。
- workflow 放仓库根 `workflows/<name>/workflow.js`，P0.1 的 compose overlay 已挂载 `./workflows`，改完重启后端即可，不必重建镜像。
- 代码位置按 P0：Dalton 后端代码在 `src/dalton/`；工作区名就是 ticker（`META`），兄弟路径 `../META/`。
- 本阶段只做美股；港股、A 股的深度支持移到 P6，相关步骤标“P6”。SPCX 留在 Core；美团代码 `3690.HK`；价格默认 yfinance（FMP 以后可选）。
- 预算名称按 P0.8：`get_budget_today(user_id)["remaining_usd"]`、`get_run_charge_usd(run_ref)`；不设任何次数限制，只有美元预算、按时间退避和供应商配额（80% 告警，不设闸门）。
- 待决问题只留 6 项（见文末），其余都已定。工时重估，见第 3 节。

本文件只覆盖“分析师怎么做研究”这一层：基金方法从 OpenClaw skill 提炼出来后，在 LangAlpha 上落成哪些 skill、workflow、自动任务和子智能体，每一步怎么建、怎么跑真实数据、怎么验收。以下内容属于其他文件，本文只引用：

| 依赖 | 文件 | 本文用到的部分 |
|---|---|---|
| 平台骨架 | `docs/dalton/dev-guide/P0-platform.md` | 模型接入与角色（P0.4 overlay、P0.6 角色模型与 `role_policy.py`）、本地预算（P0.8 `get_budget_today`、`get_run_charge_usd`）、compose overlay 挂载 `workflows/`（P0.1）、`plugins/dalton/` 包 |
| 数据接口与入职 | `docs/dalton/dev-guide/P0D-P1-data-and-onboarding.md` | `researchdata` MCP（P0-D.1 桥、P0-D.2 注册；6 个工具：`catalog`、`finance`、`macro`、`news`、`research`、`community`）、来源面板（P0-D.3 是 P0 的免迁移降级方案；P0-D.3b 是 P3 的逐文档来源记录）、`dalton_sec`（P0-D.5：`get_company_facts`、`list_filings`）、价格与一致预期快照 `data/consensus/`（P0-D.6）、`dalton-conventions` 与 `dalton_registry.py`（P1.1）、覆盖名单 `config/dalton/coverage.yaml`（P1.2）、公司工作区骨架（P1.3）、迁入认知（P1.4） |
| 知识层、发布、自主议程 | `docs/dalton/dev-guide/PK-knowledge-governance-autonomy.md` | 文件布局（PK.1）、schema（PK.2）、`knowledge_update` / `knowledge_append_verdict`（PK.4）、直接修改的事后检测（PK.5）、`dalton-knowledge` 读序（PK.6）、`check_draft` 与 `publish_report`（PK.9、PK.11）、`dalton-reviewer`（PK.10）、`dalton-agenda` 收尾三步（PK.14）、自动任务命名与 REST helper（PK.15）、零成本预检、`consensus` 探针与 `<dalton_wake>`（PK.16）、退避（PK.18）、研究总监（PK.19）、界面（PK-UI.*） |

三份文件 v0.3 按同一份决定同步修订。若之后它们再改，以它们为准。

---

## 0. 怎么读这份指南

- 每个交付物一个步骤，编号 PS.x。每步固定八栏：**目标；前置依赖；新增/改动文件（精确路径）；具体做法；验证方式（真实运行）；验收标准；风险与注意；预估工时**。
- 路径后标“（已存在）”的，撰写时已在仓库里核实过；标“（新建）”的由该步创建；标【待验证】的是推断，动手前先核实。
- 所有输出模板都是中文，保留英文术语（beat、guidance、bogey、EPS、ARR 等）。模板里的标题顺序是验收内容的一部分；篇幅、章节数、图表数不设机械门槛（蓝图 §3.2）。
- “真实运行”指：在本机 LangAlpha 界面（`make up` 起的全栈，本地 Docker 沙箱）里，用 P0.6 配置的角色模型（lead 默认原生 `claude-opus-5-5-oauth`，可切换），在对应公司工作区里触发 skill 或 workflow，跑真实数据，产物落到工作区。不接受单元测试或 mock 数据代替。
- 第 18 节（PS.18）是给 lead 验收用的 PM 评分样本和评分表，每个 P2–P5 交付物都有对应条目。

---

## 1. 平台机制核实（动手前必读）

下面每条都在源码里核实过。和直觉不一致的地方用“注意”标出，后面各步的设计都依赖这些结论。

### 1.1 skill 的打包、斜杠命令与 `requires`

- 插件包里的 skill 目录（`plugins/<bundle>/skills/<name>/SKILL.md`）在启动时由 `src/ptc_agent/config/plugins.py: bundled_skill_dirs()` 读入，同步进沙箱的 `.agents/skills/<name>/`。智能体读这个路径下的 `SKILL.md`，这个 skill 就算加载（`registry.py` 的 `skill_md_path` 匹配）。
- **注意：随包发布的 skill，斜杠命令和 `requires` 不从 frontmatter 读。** 两者只存在于 `src/ptc_agent/agent/middleware/skills/registry.py` 的 `SKILL_REGISTRY`（`SkillDefinition.command`、`SkillDefinition.requires`）。仓库里所有 SKILL.md 的 frontmatter 都只有 `name` 和 `description`。没登记的随包 skill（例：`plugins/alternative_data/skills/x-api`）照样同步进沙箱、出现在 skill 清单里，但没有斜杠命令，也不受 `requires` 保护。
  - 用户上传的 skill 会读 frontmatter 的 `command:`（`src/server/services/user_skills/validate.py: _declared_command`），但不支持 `requires`。
  - `requires` 的作用只有一个：被依赖的 skill 不会因为用户单独关掉它而从沙箱消失（`resolve_disabled_skills`）。它**不会**自动加载被依赖的 skill。每个 skill 仍要在正文第一段写明“先读 `.agents/skills/dalton-conventions/SKILL.md`”。
  - `resolve_disabled_skills` 只处理直接声明的依赖，不传递（P0D P1.1 已核实）。
  - 结论（与 P0D P1.1 一致）：每个 Dalton skill 都在 `src/ptc_agent/agent/middleware/skills/dalton_registry.py` 的 `DALTON_SKILL_DEFINITIONS` 里登记一条 `SkillDefinition`（`command`、`requires=("dalton-conventions", "research-conventions", ...)`、`skill_md_path`），`registry.py` 末尾合并。不登记的 skill 会以“Your skills（用户内容）”出现，失去平台身份和 `requires` 保护。SKILL.md 正文前 12 行内必须有这一行：`Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.`
- registry 的 `description` 和 SKILL.md frontmatter 的 `description` 要逐字一致（registry 注释：前者驱动 PTC 发现，后者驱动沙箱清单）。
- `RunWorkflow` 工具在 `run-workflow` skill 加载之前是隐藏的（`registry.py` 的 `tool_names`，并受 `config.yaml: workflow.enabled` 控制）。所以每个“启动 workflow 的 skill”第一步都要读 `.agents/skills/run-workflow/SKILL.md`。

### 1.2 子智能体

- 随包插件可以带子智能体：`plugins/<bundle>/agents/<name>.md`。由 `src/server/services/plugins/bundled.py: bundled_agent_plans()` 读入，格式同 `src/server/services/plugins/agents.py`（frontmatter + 正文作为指令）。
  - **注意：`bundled_agent_plans` 有 `lru_cache`，改了 agents 文件要重启后端。**
  - frontmatter 可用的键：`description`（必填，有长度上限 `DESCRIPTION_MAX`）、`model`（必须是具体模型 id；写 `sonnet/opus/haiku` 这类家族名会被忽略、回落到线程模型）、`effort`、`tools`、`search_provider`、`search_depth`、`connectors`、`brokerages`、`preferred_connectors`、`preferred_skills`（或 Claude Code 风格的 `skills`，作用是预加载）、`available_to`（角色：analyst / chief_of_staff）。`enabled: false` 会被忽略。
  - `tools` 用的是“工具词”：`web`、`market_data`、`code`、`shell`（`src/ptc_agent/agent/subagents/merge.py: TOOL_WORDS`），也接受 Claude Code 工具名（`Bash` 对应 `code`+`shell`，`WebSearch`/`WebFetch` 对应 `web`）。**文件读写工具每个子智能体都有，不能用 `tools` 去掉。** 不写 `tools` 时绑定 Analyst 预设（`DEFAULT_CUSTOM_TOOLS`：web、market_data、code、shell）。
  - 子智能体没有只读开关：绑了 `filesystem` 工具组就可写（PK D5 已核实 `subagents/compiler.py`）。所以 PK 把**权威审稿放在服务端**：`publish_report` 内部用 `LLMService.complete` 调另一家族的模型，结论写数据库；`plugins/dalton/agents/dalton-reviewer.md`（PK.10 交付）只用于草稿期的自愿审阅，两处共用同一份 prompt。本文的 workflow 在草稿期派它做审阅，它的意见只用于修订，不能替代发布时的服务端审稿。
  - `knowledge_update` / `knowledge_append_verdict` / `publish_report` 只挂在主智能体上（`src/dalton/tools/`），**子智能体不能经工具写知识层、不能发布**（PK.4 (7)）。所以本文所有 workflow 的子智能体只写 `research/`、`drafts/`、`model/` 下的自由文件，并把知识层修改写成“修订草案”文件；由启动 skill 的主智能体在 workflow 结束后读草案、调用 `knowledge_update` 落库。子智能体如果直接改了 `data/*.json`，不会被拦截，PK.5 在本轮结束后检测并记一个 `direct_edit` 版本（不合 schema 时给分析师加警告），但这不是正规路径，提示词里要写明不要这样做。
  - 子智能体的 `model`：用工具的子智能体（`dalton-evidence` 等）不写 `model`，跟随线程模型；绝不配 CLI gateway 类的 text-only 模型（`ocb-claude-cli-gateway-*`、antigravity、muse），那类模型只用于 compaction、后台摘要和审稿人（P0.6）。需要写死时只用 manifest 键（`ocb-<provider>-<model>` 或原生键）。
- 内置子智能体（`src/ptc_agent/agent/subagents/builtins.py`）：`research` 只有 `web_search` 和文件工具，**没有 `execute_code`，调不了 PTC 的 MCP 数据函数**；`general-purpose` 和 `equity-analyst` 有全套工具。凡是要调内部研究库、wiki、SEC、行情的取证支线，`agentType` 必须用 `general-purpose`、`equity-analyst` 或 Dalton 自己的取证子智能体，不能用 `research`。
- Chief of Staff 角色（Home）不带 `equity-analyst` 子智能体，新研究交给工作区分析师（`delegate_to_analyst`）。

### 1.3 workflow

- **两层存放：**
  1. 仓库自带：仓库根目录 `workflows/<name>/workflow.js`，`meta.name` 必须等于目录名（`src/ptc_agent/agent/middleware/background_subagent/workflow/prebuilt.py`）。注册表读 `<进程工作目录>/workflows/*/workflow.js`，并按进程缓存。上游 `docker-compose.yml` 的开发栈没有挂载 `./workflows`；P0.1 的 `docker-compose.dalton.yml` 已加 `./workflows:/app/workflows:ro`，所以改了自带 workflow 只需 `docker compose restart backend`，不必重建镜像（不改上游文件）。
  2. 用户层：`.agents/workflows/<name>.js`（存在 LangGraph store 里，经文件挂载可读写），**同名时用户层覆盖自带层**（`tool.py` 的 `_resolve`）。
  - **注意：插件包不能带 workflow。** `plugins.py` 只读 `mcp.json`、`skills/`、`agents/`。Dalton 的 workflow 放仓库根目录 `workflows/`，不放 `plugins/dalton/`。开发迭代时可以先写到 `.agents/workflows/<name>.js` 调试，定稿后再搬到 `workflows/<name>/workflow.js`，并删掉用户层同名文件，否则用户层会一直盖住自带版本。
- **脚本 API**（`prelude.js`、`plugins/langalpha_service/skills/run-workflow/SKILL.md`）：`agent(prompt, {agentType, label, phase, schema})`、`parallel(thunks)`、`pipeline(items, ...stages)`、`phase()`、`log()`、`args`。脚本本身在服务端 QuickJS 里跑，**不能读写工作区文件**；它派出的子智能体可以。子智能体从空白开始，看不到对话、脚本和兄弟节点，提示词必须自带全部上下文（文件路径、要读的 skill、输出位置）。失败的槽位返回 `null`。
- **上限**（`config.yaml` 的 `workflow:` 段，已核实）：每次运行最多派 64 个子智能体；同时 8 个；单个子智能体 1800 秒；整次运行 16200 秒；每个线程同时最多 2 个运行；单次派发提示词 40 万字符；返回 schema 最多 4096 字节、嵌套 5 层、每层 32 个键；子智能体结果 1 MB；交给模型的运行摘要 96 KB。
  - 设计含义：大段正文让子智能体**写文件**，只把路径和结构化摘要（小 schema）返回给脚本；周会这种“每家公司一个子智能体”的 fan-out 要按 64 的上限算账。
- 运行产物落在 `.agents/threads/<thread>/workflows/<run-id>/`（每个子智能体的记录和 `result.json`）。

### 1.4 自动任务

- 模型：`src/server/models/automation.py`。触发类型 `cron`、`once`、`price`。
- 文件形式：`.agents/user/automations/<file_name>.json`（`src/server/services/automations/file.py`）。文件名 1–64 位字母数字 `-` `_`，以 `.json` 结尾。可编辑字段**只有**：`name, description, status, trigger_type, cron_expression, next_run_at, trigger_config, timezone, instruction, workspace_id, thread, llm_model, delivery, max_failures`。`workspace_id` 可写 `"home"`；`thread` 取 `"new"`、`"persistent"` 或线程 id。文件末尾的 `state` 块只读。 Dalton 的模板一律不写 `max_failures`（用平台默认值）：Dalton 自己不加任何次数限制，只靠美元预算、按时间退避（PK.18）和供应商配额告警。
  - **注意：文件里不能写 `additional_context`。** 要让自动任务开跑时预加载 skill，有两条路：(a) 走 REST `POST /api/v1/automations`，带 `additional_context: [{"type":"skills","name":"dalton-earnings-preview"}]`（`automation_executor.py` 把它原样传进 `ChatRequest`）；(b) 用文件方式，`instruction` 以斜杠命令开头，例如 `"/dalton-earnings-preview META FY26Q3 ..."`。(b) 依赖聊天入口从消息文本识别斜杠命令（`src/server/handlers/chat/request_prep.py` 第 309 行附近有“从消息文本检测斜杠命令”的逻辑），自动任务是否走同一条识别路径【待验证】。PS.0 先验证 (b)；不行就统一走 (a)，并让 skill 在 instruction 里再写一句“先读 `.agents/skills/<name>/SKILL.md`”兜底。
- 价格触发（`PriceTriggerConfig`）：`symbol` 为不带前缀的代码，最长 10 个字符；`conditions` 是 AND 关系，类型有 `price_above`、`price_below`、`pct_change_above`、`pct_change_below`，参照价 `previous_close` 或 `day_open`；`retrigger.mode` 为 `one_shot` 或 `recurring`，`recurring` 时 `cooldown_seconds` 要么不写（默认到下一个交易日）、要么不少于 14400 秒。
  - 设计含义：一只股票“涨超 X% 或跌超 X%”要建**两条**自动任务（多个条件是 AND，不是 OR）。非美股在其交易时段内按 REST 轮询（`price_monitor.py`）。
- 自动任务可以由智能体自己写文件创建（AGENTS.md：智能体可以给自己排期）。保存失败时工具结果里会出现 `NOT SAVED`。once 任务**跑完后**不能再改（`file.py`：“Write a new file for another run.”），跑之前可以改 `next_run_at`。
- Dalton 自动任务的文件名约定由 PK.15 规定，预检靠文件名识别 Dalton 任务，本文全部遵守（`<t>` = ticker 小写、`.` 换成 `-`）：
  - `dalton-<t>-watch.json`（cron，事件轮询 + 预检）；
  - `dalton-<t>-delta.json`（cron，每晚 thesis 增量，PS.11；预检用 PK.16 的 `consensus` 探针）；
  - `dalton-<t>-move-up.json`、`dalton-<t>-move-down.json`（price，涨、跌各一条，PS.10）；
  - `dalton-<t>-v<YYYYMMDD>-<slug>.json`（once，下一项验证）；
  - `dalton-<t>-earn-<period>-<k>.json`（once，`k` ∈ `tm3`/`t0`/`tp1`，即 T-3 前瞻、T0 快讯、T+1 深度点评）；
  - `dalton-earnings-calendar.json`（home，每周六刷新财报日期，PS.7）、`dalton-director-daily.json`、`dalton-director-weekly.json`（home）。
- 权限已定：分析师对自己工作区的 `dalton-*` 自动任务文件有常设授权，创建、修改不需要再问 PM（PK.15）；研究总监派单给分析师已开 `auto_approve_handoffs`。Home 的研究总监为公司工作区建或改自动任务，**不跨工作区写文件**，一律调后端 REST helper `PUT /api/v1/dalton/automations/{name}`（`src/dalton/app/router.py`，PK.15 定义；请求体就是自动任务文件内容，带 `workspace_id`，服务端走现有自动任务文件服务校验）。

### 1.5 工作区与 `agent.md`

- 每个工作区是沙箱里的一个文件夹；同一台“计算机”上的工作区互相可读写（AGENTS.md：mutually trusted）。Home 是 Chief of Staff 的文件夹。所有公司工作区在同一台 computer 上（P0.1：一个共享沙箱）。工作区名就是 ticker（`META`），文件夹名相同，兄弟工作区用相对路径访问（P0D P1.2 用 `../Home/dalton/coverage.yaml`；PK.6 用 `../META/thesis.md`）。
- 工作区根目录的 `agent.md` 是分析师自己的索引（`WorkspaceLayout.AGENT_MD_FILE`），修改会以 `agent_md_changed` 行进入运行时上下文。Home 的 `agent.md` 是 Chief of Staff 的“PM 关注什么”笔记，结构由 `<role>` 规定，不要把分析师工作区的版式写进 Home 的 `agent.md`（`src/ptc_agent/agent/prompts/templates/AGENTS.md`）。

### 1.6 本文使用的数据、知识层与发布接口（来自 P0D、PK）

| 用途 | 写法 | 来源 |
|---|---|---|
| 内部研究库 | `researchdata.research(route=..., operation=..., query=..., start=YYYY-MM-DD, end=..., limit=..., params={...})`；route：`company-wiki`（基金 wiki，`params.company=TICKER`）、`all`（wiki + sales-notes + TMTB + FUNDA AI 融合检索）、`alphaengine`（`operation="search_library"` 后 `get_document`）、`guidepoint`、`transcript`、`sales-notes`、`tmtb`、`funda-ai`。每个线程先调一次 `catalog()`。只走 PTC（沙箱代码里调用）。超时按路由由桥决定（默认 240 秒；AlphaEngine 全文、Guidepoint、大型 filing 等长文在调用方预算允许时最长 600 秒；LangAlpha 对这个 server 的单次调用预算默认 300 秒），超时只用来兜住挂起，不用来省时间；窗口要窄是为了相关性。AlphaEngine 全文有供应商配额（80% 告警，不设闸门），只读需要的。`status: empty` 记“本轮未取得”。结果带 `_sources`（identity、document_id、as_of、url）；P0 的来源面板用免迁移降级显示（P0-D.3），逐文档来源记录在 P3 上线（P0-D.3b） | P0D P0-D.1、P0-D.2 |
| 一致预期快照 | `researchdata.finance(route="estimates", ticker=T)`：EPS 与收入（0q、+1q、0y、+1y）、EPS 趋势（7/30/90 天前）、修正次数、目标价、评级、近 8 期财报日期、价格位置、`implied_model`、近 4 次财报次日涨跌。Yahoo 没有历史：每次取数都存快照到公司工作区 `data/consensus/<YYYY-MM-DD_HHMM>.json`（原始 JSON + `_meta`），这是修正历史的唯一来源。`implied_model` 标 `model-derived` | P0D P0-D.6 |
| 一致预期细项 | yfinance 插件 `get_earnings_estimates`、`get_revenue_estimates`、`get_growth_estimates`、`get_analyst_price_targets`、`get_upgrades_downgrades`、`get_earnings_history` | 已存在 |
| 估值历史 | `fundamentals.get_historical_valuation`（备选 `yf_fundamentals.compare_valuations`） | P0D P0-D.6 |
| 行情 | 默认 yfinance（`yf_price.get_stock_history` 等）；FMP 系列（`get_daily_prices`、`get_quote`、`price_data` MCP）只在以后配置 `FMP_API_KEY` 后可选；收益计算统一基准日 | P0D P0-D.6 |
| SEC | `dalton_sec.list_filings(ticker, forms, since, until, limit)`、`dalton_sec.get_company_facts(ticker, concepts, periods, forms)`（单位只取发行人 fact、Q 只配 3 个月、Q4 = FY − 9M 标 `derived_filing_actual`）；正文读取用 `researchdata.finance(route="sec", ...)` 的 read-item / search-text，或自带 `get_sec_filing` | P0D P0-D.5 |
| 港股/A 股财务 | `researchdata.finance(route="cn-hk", ...)` 可以调用，但本阶段不做港股/A 股深度支持，移到 P6 | P0D P0-D.7 |
| 覆盖名单 | 规范文件 `config/dalton/coverage.yaml`；智能体读 Home 里的副本 `Home/dalton/coverage.yaml`，公司工作区用 `../Home/dalton/coverage.yaml`。每家有 `tier`、`market`、`peer_group`、`peers`、`etfs`、`aliases`、`earnings.ir_url`、`workspace.status`、`coverage_stage`。SPCX 在 Core；美团是 `3690.HK` | P0D P1.2 |
| 知识层写入 | 正规路径是主智能体的原生工具：`knowledge_update(doc, ops, reason, base_version, evidence_refs)`（doc ∈ company/thesis/debate/drivers/management/lessons/agenda/models），`knowledge_append_verdict(verdict, agenda_ops)`。**子智能体没有这两个工具**。直接 Write/Edit/Bash 改 `data/*.json` 或生成的 `.md` 不会被拒绝，也不会被还原：本轮结束后检测，记成 `direct_edit` 版本，不合 schema 时在 `agent.md` 托管块给分析师加警告。保留的硬约束只有三条：证据台账只追加；`thesis.official` 和 house view 的正式版本只经 `publish_report` 写；目标价必须指向模型单元格 | PK.4、PK.5 |
| 知识层读取 | 根目录生成的 `thesis.md`、`debate.md`、`drivers.md`、`management.md`、`lessons.md`、`agenda.md`、`verdicts.md`（最近 60 条）；机器真值 `data/*.json`；`agent.md` 托管块；读序见 `dalton-knowledge` skill | PK.1、PK.6 |
| 收尾 | 每次工作结束按 `dalton-agenda` 的“收尾三步”：写结论（`knowledge_append_verdict`，五词 + `next_validation` + `progress`）、在同一次调用的 `agenda_ops` 里更新议程、必要时写 once 自动任务给自己排期 | PK.14、PK.15 |
| 草稿反馈 | `check_draft`（数字核验，只给反馈，写 `drafts/<slug>.audit.md`；从不阻止草稿） | PK.9 |
| 正式发布 | `publish_report(draft_path, title, kind, slug, attachments, updates_thesis, request_id)`；kind ∈ `initial_coverage`、`full_coverage`、`bullbear`、`event_note`、`earnings_preview`、`earnings_review`、`thesis_update`、`topic`、`conviction_call`（周报不经 `publish_report`，PS.13）。内部依次做数字核验（确定性，P3 起依赖 P0-D.3b 的逐文档来源）→ 服务端独立审稿（不同模型家族，`LLMService.complete`）→ PM 审批卡（`ToolApprovalCard`，批一次用一次；无人值守 run 里停在 `awaiting_pm`）→ 不可变写入 `published/`。这是唯一的核验闸门 | PK.11 |
| 事件唤醒 | 公司 watch / delta 自动任务 + 零成本预检（探针：sec、earnings、price、indicator、pm_question、internal、consensus），命中时新线程第一条上下文是 `<dalton_wake>`，信号 id 填进结论的 `trigger.ref`；无信号记 `PRECHECK_SKIPPED`，花费 0 | PK.16 |
| 基金 wiki | **只读**。经 `researchdata.research(route="company-wiki")` 检索和阅读，任何阶段都不回写；成果只留在工作区知识层和 `published/` | P0D P0-D.4 |

---

## 2. 所有 PS 交付物共用的约定

### 2.1 目录

```
plugins/dalton/                      （P1.1 创建；PK 往里加 dalton-knowledge、dalton-publish、dalton-agenda、dalton-research-director 四个 skill 和 agents/dalton-reviewer.md；本文加下列 skill 和 agents/dalton-evidence.md）
  plugin.json
  skills/
    dalton-conventions/              （P1 交付，本文只引用）
    dalton-company-background/
    dalton-bullbear/
    dalton-financial-lenses/
    dalton-initial-coverage/         （启动 workflow 的薄 skill）
    dalton-full-coverage/            （同上）
    dalton-earnings-calendar/
    dalton-earnings-preview/
    dalton-earnings-flash/
    dalton-earnings-deep/
    dalton-stock-move/
    dalton-thesis-delta/
    dalton-filing-review/
    dalton-weekly-review/            （启动 workflow 的薄 skill）
    dalton-topic-research/           （同上）
    dalton-call/
    dalton-pm-qa/
  agents/
    dalton-evidence.md               （本文 PS.1）
    dalton-reviewer.md               （PK.10，本文只引用）
workflows/                           （仓库根目录，已存在，仅 .gitkeep；P0.1 overlay 挂载为 /app/workflows:ro）
  initial-coverage/workflow.js
  full-coverage/workflow.js
  bullbear-doc/workflow.js
  earnings-deep-update/workflow.js
  weekly-coverage-review/workflow.js
  topic-research/workflow.js
docs/dalton/dev-guide/automation-templates/   （新建，自动任务 JSON 模板，见各步）
```

注册：`src/ptc_agent/agent/middleware/skills/dalton_registry.py`（P1.1 新建，本文往里加条目）。

每个 skill 目录内：`SKILL.md`（必有）、`references/`（按需加载的长参考）、`scripts/`（确定性计算，Python 标准库优先；需要第三方库时写明，在沙箱里用 `uv run --with` 或沙箱已装的包，【待验证】沙箱 Python 环境）、`templates/`（输出骨架）。

### 2.2 SKILL.md frontmatter 模板

```yaml
---
name: dalton-xxx
description: "一句话说清做什么、何时触发。触发词：……"
---
```
frontmatter 只写 `name` 和 `description`（随包 skill 不解析其他键）；`command` 和 `requires` 写在 `dalton_registry.py`。`description` 两处逐字一致。

正文前 12 行内固定写两句：

> Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
> 公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

### 2.3 产物落盘位置（公司工作区内）

| 类型 | 路径 | 说明 |
|---|---|---|
| 待发布草稿 | `drafts/<YYYY-MM-DD>-<kind>.md`（必要时附 `.html`/`.xlsx`；`check_draft` 写侧车 `<slug>.audit.md`） | kind 例：`background`、`bullbear`、`initial-coverage`、`memo`、`preview-FY26Q3`、`flash-FY26Q3`。PK.1 把 `drafts/` 定义为“待发布草稿”，不打算发布的事件笔记放 `research/` |
| 事件笔记、取证材料 | `research/<YYYY-MM-DD>-<slug>.md`、`research/<YYYY-MM-DD>-<kind>/`（支线文件、数字底表、检索覆盖日志、知识层修订草案） | 自由文件，不进发布 |
| 模型 | `model/<TICKER>-model-v<NN>.xlsx`，每版新文件，用 `knowledge_update(doc="models")` 登记 | 只增不改，见 PS.6、PS.9 |
| 一致预期快照 | `data/consensus/<YYYY-MM-DD_HHMM>.json` | 每次取一致预期都存（P0D P0-D.6） |
| 正式成果 | `published/` | 只由 `publish_report` 写入 |

跨公司产物（周会、跨公司专题、财报日历台账）放在 Home 的 `dalton/` 目录下（`Home/dalton/drafts/`、`Home/dalton/research/`），与 P0D P1.2 的 `Home/dalton/coverage.yaml` 同处。

### 2.4 写作与数字纪律（每个 skill 的自检清单都包含这些）

从 OpenClaw skill 提炼、所有 PS 交付物通用。正式定义在 `dalton-conventions`，这里列出本文各模板直接依赖的条目：

1. 判断先行：先给立场、依据、投资含义，局限只说一次，放在影响判断的地方。
2. 每个数字带期间（FY2025、2Q26、1H26、过去 12 个月（至 YYYY-MM-DD））、单位、口径（GAAP/non-GAAP、币种），第一次出现时带来源身份。
3. 来源标注放句末并写明身份：`（UBS [引用9]）`、`（CFO [引用5]）`、`（10-K [引用2]）`、`（某行业专家 [引用14]）`。一句一个来源；同一份研报被邮件、TMTB、FUNDA AI、X 转述只算一条来源链。
4. 信息身份分层：公司指引、卖方一致预期、具名机构预测、买方门槛、本稿判断分开写；house case 和市场基准分开。
5. 分析师在电话会提问里说出的数字，管理层没有确认的，标“分析师提问口径，管理层未确认”。
6. “本轮未取得”不等于“公司未披露”；工具失败、额度用尽、未登录要分别写，不能写成“无更新”。
7. 假设写成 `[假设] 数字（推导依据）`。
8. 正式发布的成果不能有无来源的数字；草稿里未核实的数字标 `[未核实]`，不阻止保存。
9. 不强迫看多；没有足够预期差就写“暂无高 conviction”。

### 2.5 从 OpenClaw 迁移时统一剥掉的东西

| 类别 | 例子 | 处理 |
|---|---|---|
| 渲染与投递 | 飞书分块写入、`feishu_grant_perm.sh`、Discord 不用表格、禁止 `$` 和 `~`（飞书 LaTeX/删除线）、`<url>` 包裹 | 删除。LangAlpha 渲染 Markdown；投递走自动任务 `delivery` 或 PK 定义的通知 |
| 时间与批次上限 | 25 分钟硬杀、10/12/8 分钟检索上限、900 秒批次、每节落盘标记 `<!-- 已落盘 -->` | 删除。改用 workflow 的 `child_timeout` 和本地预算 |
| 写死的模型 | `claude-cli-gateway/claude-opus-5-5`、`openai/gpt-6.1-sol`、`google/gemini-4-argon` 等 | 删除。lead 和子智能体模型由 P0.6 的角色配置决定（可切换）；草稿期审稿人模型写在 `plugins/dalton/agents/dalton-reviewer.md` 一处（manifest 键，如 `ocb-openai-gpt-6.1-sol`），发布时审稿人由 PK 的 `reviewer_models` 按 `role_policy.family_of()` 选 |
| 人名与账号 | Richard、Mark、Lumos、Eve、PM 的个人邮箱、飞书 folder token | 改成工作区配置（覆盖名单文件、PM 称呼），不写进 skill |
| OpenClaw 运行时调用 | `sessions_spawn`、`sessions_yield`、`researchdata__*` 工具名、`data_gateway.py`、`NO_REPLY` | 换成 workflow `agent()` 和 P0D 的 MCP 函数 |
| 本机特殊情况 | roic.ai 403、东财被封、Reddit 无路由、AlphaEngine 每日约 190 次额度 | 不写进方法；额度类约束由数据接口返回错误时处理 |

### 2.6 事件结论五词与知识层写入（按 PK）

所有事件驱动的 skill（前瞻、快讯、深度点评、异动、thesis 增量、公告、周会、PM 问答）结束时都走 `dalton-agenda` 的收尾三步（PK.14）：

1. **写结论**：`knowledge_append_verdict(verdict={...}, agenda_ops=[...])`。五词取一（JSON 用 Playbook 正式词形）：`NO_CHANGE`、`THESIS_STRENGTHENED`、`THESIS_WEAKENED`、`THESIS_BROKEN`、`NEW_THESIS`；md 与界面显示为无变化、强化、削弱、证伪、新论点。必须有 `next_validation`（what + due）和 `progress`；`trigger.kind` 用 PK.2.10 的枚举（`filing`、`earnings`、`price_move`、`indicator`、`due_validation`、`pm_question`、`internal_material`、`agenda`、`director`、`manual`），被 `<dalton_wake>` 唤醒时 `trigger.ref` 填信号 id。没看到新证据也写 `NO_CHANGE` + `progress=none`（PK.18 的按时间退避靠它）。`THESIS_BROKEN` 时应同时把相应支柱状态改为 broken/changed（PK 只给警告，不拒绝）。
2. **改知识层**：结论不是 `NO_CHANGE` 时，用 `knowledge_update(doc=..., ops=[...], reason=..., base_version=...)` 改 thesis / debate / drivers / management / lessons；服务端自动留旧值、新值、原因、版本（蓝图 §3.1 第 5 条）。证据行的 `source.tier` 应与 `source.type` 对应（filing=1、company_release=2、broker_report=3、official_data=4、expert=5、news=6；不一致只给警告）。
3. **排期**：只有 watch / delta 自动任务和研究总监都覆盖不到时，才写 once 自动任务（`dalton-<t>-v<YYYYMMDD>-<slug>.json`），不需要再问 PM。
4. thesis 的正式版本（`thesis.official`，含正式 house view）只能经 `publish_report(updates_thesis=True)` 由 PM 批准后写入；`house_view.target` 必须指向模型单元格（PK 保留的硬约束）。

本文各步的“知识层写入”一栏只写**改哪个 doc、改什么**；字段名以 PK.2 的 JSON Schema 为准。

---

## 3. 步骤总览

| 步骤 | 交付物 | 类型 | 斜杠命令 | 阶段 | 依赖 | 工时 |
|---|---|---|---|---|---|---|
| PS.0 | 脚手架、`dalton_registry.py` 登记、自动任务预加载验证、试跑记录规范 | 基础 | — | P2 前 | P0D、P1、PK.4 | 0.75 天 |
| PS.1 | 子智能体 `dalton-evidence`；草稿期使用 PK.10 `dalton-reviewer` 的约定 | subagent | — | P2 | PS.0、PK.10 | 0.5 天 |
| PS.2 | `dalton-financial-lenses`（净现金双口径、隐含资产、股东回报 R1/R2/R3；本阶段只做美股） | skill + scripts | `/dalton-financial-lenses` | P2 | PS.0 | 2.25 天 |
| PS.3 | `dalton-company-background`（9 段） | skill | `/dalton-company-background` | P2 | PS.0 | 1.5 天 |
| PS.4 | `dalton-bullbear` 快速版 + workflow `bullbear-doc`（v4.2） | skill + workflow + scripts | `/bullbear` | P2 | PS.1、PS.2 | 4 天 |
| PS.5 | workflow `initial-coverage` + 启动 skill | workflow + skill | `/initial-coverage` | P2 | PS.2–PS.4 | 3.5 天 |
| PS.6 | workflow `full-coverage` + 启动 skill（12 问 memo、Excel 模型、审稿、发布申请） | workflow + skill | `/full-coverage` | P3 | PS.5、PK 发布 | 6 天 |
| PS.7 | `dalton-earnings-calendar`（只认官方日期、三态台账、经 REST helper 生成 tm3/t0/tp1 自动任务；每周六由 `dalton-earnings-calendar.json` 调用） | skill + automation | `/dalton-earnings-calendar` | P4 | PS.0、PK.15、PK.19 | 2 天 |
| PS.8 | `dalton-earnings-preview`（T-3 交易日 once 任务） | skill + automation | `/dalton-earnings-preview` | P4 | PS.7 | 2.5 天 |
| PS.9 | `dalton-earnings-flash`（T0+N 小时）+ workflow `earnings-deep-update`（深度点评 → 模型更新 → thesis 校准 → 前瞻对账） | skill + workflow + automation | `/dalton-earnings-flash`、`/dalton-earnings-deep` | P4 | PS.7、PS.8、PS.6 模型 | 4.5 天 |
| PS.10 | `dalton-stock-move` + 每只 Core 价格触发（`move-up` / `move-down`） | skill + scripts + automation | `/dalton-stock-move` | P4 | PS.0 | 3 天 |
| PS.11 | `dalton-thesis-delta` + 每晚 cron（PK.16 `consensus` 探针预检；无重要变化不推送） | skill + scripts + automation | `/dalton-thesis-delta` | P4 | PK.4、PK.16 | 2 天 |
| PS.12 | 公告监控：被 PK watch 预检的 sec 信号唤醒后的 `dalton-filing-review` | skill | `/dalton-filing-review` | P4 | PK.16 | 1.5 天 |
| PS.13 | workflow `weekly-coverage-review` + 启动 skill + 每周 cron | workflow + skill + scripts + automation | `/weekly-review` | P5 | PS.10、PS.11 | 5 天 |
| PS.14 | workflow `topic-research` + 启动 skill（不回写 wiki） | workflow + skill | `/topic-research` | P5 | PS.1 | 3.25 天 |
| PS.15 | `dalton-call`（预期差三问、三情景 IRR、仓位级别、对冲） | skill + scripts | `/dalton-call` | P5 | PS.4、PS.6 | 2.5 天 |
| PS.16 | PM 问答行为：`dalton-pm-qa`（回答结构与路由，读序沿用 PK.6 `dalton-knowledge`） | skill | `/ask` | P5 | PK.6、PK.19 | 1 天 |
| PS.17 | 草稿期审稿接入各 workflow；与发布时服务端审稿的分工 | 横切 | — | P3–P5 | PS.1、PK.10–PK.11 | 并入各步 |
| PS.18 | skill 质量评估：PM 可评分的验收样本与评分表 | 验收 | — | P2–P5 | 全部 | 每阶段 0.5 天 |

合计约 46 人日（v0.2 约 48；PS.18 每阶段 0.5 天另计，共 2 天；不含 PM 评阅时间）。减少的部分：PS.0 不再需要验证 workflow 部署和等授权，PS.2 去掉港股/A 股，PS.11 去掉“便宜模型跑第一步”的备选，PS.14 去掉 wiki 回写。各阶段可并行的步骤：P2 的 PS.2/PS.3 与 PS.1 并行；P4 的 PS.7–PS.12 中，PS.10、PS.11 与财报链并行。

---

## PS.0 脚手架、登记与试跑规范

**目标**
让后面每个 skill、workflow、自动任务都有固定落点，能被斜杠命令触发，能被自动任务预加载，并且每次真实试跑都留下可复查的记录。

**前置依赖**
P0D 已接通 `researchdata` 与 `dalton_sec`；P1.1 已建 `plugins/dalton/plugin.json`、`dalton-conventions` 和 `dalton_registry.py`；P1.2 已生成 `config/dalton/coverage.yaml` 并同步到 `Home/dalton/coverage.yaml`；P1.3/P1.4 已为 META、AMZN、GOOGL、MSFT 建好工作区并迁入认知；PK.4 的 `knowledge_update` / `knowledge_append_verdict` 可用（P2 起需要）。

**新增/改动文件**
- `plugins/dalton/skills/<各 PS skill>/`（新建）
- `src/ptc_agent/agent/middleware/skills/dalton_registry.py`（P1.1 新建；本步往 `DALTON_SKILL_DEFINITIONS` 加条目）
- `tests/unit/middleware/skills/test_shared_skill_layer.py`（P1.1 已加“引用 dalton-conventions 的 skill 必须 requires”的检查；本步新增 skill 自动受它约束）
- `workflows/`（已存在；由 P0.1 的 `docker-compose.dalton.yml` 挂载为 `/app/workflows:ro`，改完 `docker compose restart backend` 即可）
- `docs/dalton/dev-guide/automation-templates/`（新建）
- `docs/dalton/dev-guide/trial-runs/`（新建，每次真实试跑一个 Markdown 记录，见下）

**具体做法**
1. 在 `dalton_registry.py` 为下表每个 skill 登记 `SkillDefinition`，`description` 与 SKILL.md frontmatter 逐字一致，`skill_md_path="skills/<name>/SKILL.md"`。所有条目的 `requires` 都同时含 `dalton-conventions` 和 `research-conventions`（P1.1 规则，因为依赖不传递），下表只列额外依赖。

   | skill | command | 额外 requires |
   |---|---|---|
   | dalton-company-background | `dalton-company-background` | dalton-financial-lenses |
   | dalton-bullbear | `bullbear` | dalton-financial-lenses, dalton-knowledge, dalton-agenda |
   | dalton-financial-lenses | `dalton-financial-lenses` | — |
   | dalton-initial-coverage | `initial-coverage` | run-workflow, dalton-company-background, dalton-financial-lenses, dalton-bullbear, dalton-knowledge, dalton-agenda, comps-analysis |
   | dalton-full-coverage | `full-coverage` | run-workflow, dalton-financial-lenses, dalton-knowledge, dalton-agenda, dalton-publish, 3-statements, dcf-model, comps-analysis, check-model, model-update, xlsx |
   | dalton-earnings-calendar | `dalton-earnings-calendar` | — |
   | dalton-earnings-preview | `dalton-earnings-preview` | dalton-knowledge, dalton-agenda |
   | dalton-earnings-flash | `dalton-earnings-flash` | dalton-knowledge, dalton-agenda |
   | dalton-earnings-deep | `dalton-earnings-deep` | run-workflow, dalton-knowledge, dalton-agenda, model-update, thesis-tracker, check-model, xlsx |
   | dalton-stock-move | `dalton-stock-move` | dalton-knowledge, dalton-agenda |
   | dalton-thesis-delta | `dalton-thesis-delta` | dalton-knowledge, dalton-agenda |
   | dalton-filing-review | `dalton-filing-review` | dalton-knowledge, dalton-agenda, dalton-financial-lenses |
   | dalton-weekly-review | `weekly-review` | run-workflow, dalton-stock-move, dalton-call |
   | dalton-topic-research | `topic-research` | run-workflow, dalton-knowledge, dalton-agenda |
   | dalton-call | `dalton-call` | dalton-bullbear, dalton-knowledge, dalton-agenda, dalton-publish |
   | dalton-pm-qa | `ask` | dalton-knowledge, dalton-agenda, dalton-bullbear |

   `run-workflow` 有 `system_gate`（`config.yaml: workflow.enabled`），关掉时依赖它的 skill 要在正文里说明“workflow 不可用时按顺序手动执行各阶段”。斜杠命令不要和内置命令冲突（现有 `earnings-preview`、`earnings-analysis`、`initiating-coverage`、`thesis-tracker`、`trade-pitch`、`catalyst-calendar` 等），所以 Dalton 版本加 `dalton-` 前缀；`bullbear` 一名 P1.1 已预留；`initial-coverage`、`full-coverage`、`weekly-review`、`topic-research`、`ask` 没有内置同名，用短名。
2. 每个 skill 先放一个只有 frontmatter 和固定开头两句的占位 `SKILL.md`，重启后端，在界面输入 `/` 确认命令出现、来源显示为平台 skill（不是“Your skills”）。
3. 验证自动任务预加载（1.4 的 (b) 路径）：在 META 工作区写 `.agents/user/automations/dalton-meta-v<今天>-preload-probe.json`（遵守 PK.15 命名，预检会放行 once 计划任务；分析师对 `dalton-*` 文件有常设授权，不需要再确认）：
   ```json
   {
     "name": "PS0 预加载探针",
     "status": "active",
     "trigger_type": "once",
     "next_run_at": "<当前时间+3分钟，带时区偏移>",
     "timezone": "Asia/Shanghai",
     "instruction": "/dalton-company-background META —— 只回答：你是否已加载 dalton-company-background skill？列出你读过的 SKILL.md 路径。不要做研究，收尾写一条 NO_CHANGE（trigger.kind=manual）。",
     "workspace_id": "<META 工作区 id>",
     "thread": "new"
   }
   ```
   运行后看线程：若 skill 已加载（线程元数据里有 `{"type":"skills","name":"dalton-company-background"}`，或智能体回答读过该 SKILL.md），(b) 可用；否则后续自动任务一律改用 REST 路径（PK.15 的 `PUT /api/v1/dalton/automations/{name}` helper，由它带上 `additional_context` 调现有自动任务服务），并在 instruction 里写“先读 `.agents/skills/<name>/SKILL.md`”兜底。结论写进 `trial-runs/PS0-preload.md`。
4. 验证 workflow 的 `agentType` 能否填插件子智能体名（`validation.py` 的 `known_subagent_types` 是否包含插件子智能体【待验证】）：PS.1 建好子智能体后跑一个一行的 workflow：`return await agent('回答 ok', {agentType: 'dalton-evidence'})`。若不包含，本文所有 `agentType: 'dalton-evidence'` 改为 `'general-purpose'`，并把取证员规则写进每次派发的提示词开头（“先读 `.agents/skills/dalton-conventions/references/evidence-and-sources.md`”）。
5. 试跑记录模板 `docs/dalton/dev-guide/trial-runs/<样本编号>-<公司>-<日期>.md`：触发方式、线程 id、workflow run id、模型（manifest 键）、耗时、花费（P0.8 的 `get_run_charge_usd(run_ref)`）、产物路径、结论 id（`VD-...`）、PM/lead 评分（PS.18 表）、发现的问题、上次问题是否解决。验收时 lead 只看这些记录和产物。

**验证方式（真实运行）**
- 界面输入 `/`，16 个 Dalton 命令全部出现，选中后线程里显示对应 skill 已加载。
- 探针自动任务按时触发，结论写入试跑记录。
- `RunWorkflow(workflow="ps0-probe")` 能运行放在 `workflows/ps0-probe/workflow.js` 的探针脚本：改一行脚本后只 `docker compose restart backend`，新版本生效（确认 P0.1 的挂载），随后删除探针。

**验收标准**
- 命令全部可见可触发，来源为平台 skill；`test_shared_skill_layer.py` 通过。
- 自动任务预加载路径、插件子智能体能否作 `agentType`、workflow 改后重启即生效，三项都有明确结论并写入试跑记录。

**风险与注意**
- 用户层 `.agents/workflows/<name>.js` 会盖住自带同名 workflow，调试结束要删除。
- 不要把 workflow 写进 `plugins/dalton/`：插件包不能带 workflow。

**预估工时** 0.75 天。

---

## PS.1 子智能体 `dalton-evidence`（取证员）与审稿人的使用约定

**目标**
给所有 workflow 提供一个固定的取证角色：能调 PTC 数据函数、懂基金来源纪律、只取证不下结论、产物写文件。审稿人 `dalton-reviewer` 由 PK.10 交付，本步只规定 workflow 怎样在草稿期使用它。

**前置依赖** PS.0；PK.10 的 `plugins/dalton/agents/dalton-reviewer.md`（审稿 workflow 阶段需要；没就绪时该阶段跳过并在结果里标“审稿缺席”，草稿仍可读，见 PS.17）。

**新增/改动文件**
- `plugins/dalton/agents/dalton-evidence.md`（新建）

**具体做法**
1. `dalton-evidence.md`（frontmatter 键依据 `src/server/services/plugins/agents.py`；`description` ≤400 字符；正文 ≤16 KB）：
   ```markdown
   ---
   description: Dalton evidence gatherer. Give it one evidence branch (industry/competition, sell-side and experts, primary filings, off-street signals, or a company's weekly five sources) and an output path; it searches the fund research library, wiki, SEC and market data by source tier and writes a cited branch file. It never writes conclusions or the knowledge layer.
   tools: [web, market_data, code, shell]
   preferred_connectors: [researchdata, dalton_sec]
   skills: [dalton-conventions]
   effort: high
   ---
   ```
   正文要点（中文）：
   - 先读 `.agents/skills/dalton-conventions/SKILL.md` 和其 `references/evidence-and-sources.md`；每个线程先 `researchdata.catalog()`。
   - 只取证不下结论；来源层级 filing > 公司公告 > 署名研报 > 官方或协会数据 > 专家（标身份）> 新闻（只作线索）；同源转述算一条来源链；FUNDA AI、TMTB、digest 转述同一报告算一条。
   - “本轮未取得”三种状态分开写：无相关结果 / 渠道失败（含额度用尽、未登录）/ 未覆盖。
   - 引用编号按支线：`[A-1]`、`[B-3]`…，放句末并写身份（`（UBS [B-3]）`）；专家访谈、管理层交流（含 wiki 里转入的）和 Guidepoint 检索结果都可以用，写专家身份不写人名，Guidepoint 原话 ≤20 个英文词；不要以 MNPI 为由排除。
   - 目标公司数字只取任务给出的数字底表，缺了写“底表缺 X”；同业数字现取并写（期间，来源）。
   - 文件末尾：逐题小结（证据、最强反证、未决项、主笔要补什么）+ `<!-- 完成 -->`。
   - 不调用 `knowledge_*` 和 `publish_report`（子智能体本来就没有）；不改 `data/`、`published/`、`history/`。
   - 最终回复只给：输出文件路径、每个来源的命中与使用数、关键发现 ≤5 条、缺口。
   不写 `model`，跟随线程模型；它要调工具，所以绝不能配 text-only 的 CLI gateway 模型（P0.6 的 `role_policy.py` 会对此告警）。
2. **草稿期审稿的用法**（各 workflow 共用）：派 `agent(prompt, {agentType: 'dalton-reviewer'})`，提示词只给成稿路径、相关支线文件路径和本交付物的审稿重点；审稿人的回复文本是唯一采信的产物。审稿前后对成稿做 `sha256` 比对（由下一阶段的修订子智能体先算一次），不一致就判审稿无效。草稿期审稿只用于修订，权威结论在 `publish_report` 的服务端审稿（PK.10、PK.11）。`dalton-reviewer` 是纯文本角色，可以用 text-only 模型，但家族必须不同于 lead（`role_policy.family_of()`）。
3. 重启后端，在“子智能体”页确认 `dalton-evidence` 出现、来源为插件。

**验证方式（真实运行）**
- 在 META 工作区用 Task 派 `dalton-evidence`：“按 B 支线规则，检索 META 过去 30 天的卖方与 sales note 观点，写到 `research/ps1-probe/eb.md`”。检查文件里每条引用都能用文件里的 document_id 回指到 `researchdata` 结果的 `_sources`（P0 的来源面板只显示 `mcp:researchdata` 父记录和降级标题，P0-D.3；逐文档显示要到 P3 的 P0-D.3b）。
- 让它尝试调用 `knowledge_update`，确认没有这个工具。

**验收标准**
- `dalton-evidence` 可被 Task 和 workflow `agent()` 调用（或按 PS.0 第 4 条的结论退回 `general-purpose`）。
- 支线文件引用 100% 可回指工具结果；“本轮未取得”的三种状态用法正确。

**风险与注意**
- 取证员带 `code` 和 `shell`，可以写任何自由文件；workflow 提示词里要写明输出路径，避免它把文件写进工作区根目录。

**预估工时** 0.5 天。

---

## PS.2 `dalton-financial-lenses`：净现金双口径、隐含资产、股东回报 R1/R2/R3

**目标**
把基金的三把“财务镜头”做成一个 skill：同一家公司、同一报告期、同一汇率和市值基准下，算出基础/严格净现金、隐含资产（折前/折后）、股东回报评分（R1/R2/R3，满分 20）。它被 company-background、bullbear、initial-coverage、full-coverage（数字底表）复用。

**前置依赖** PS.0；`dalton_sec`（P0D P0-D.5）。本阶段只做美股；港股、A 股（含报表币种为人民币时的汇率口径）移到 P6。

**来源（只借原理）**
`~/.openclaw/agents/main/agent/workshop-skills/financial-analysis/`（路由）、`~/.openclaw/workspace/skills/findata-netcash/`、`findata-hiddenasset/`、`findata-return/`（SKILL.md、`references/scoring.md`、`references/output-schema.md`、`scripts/*_cli.py`）。

**新增/改动文件**（全部新建）
- `plugins/dalton/skills/dalton-financial-lenses/SKILL.md`
- `plugins/dalton/skills/dalton-financial-lenses/references/netcash.md`
- `plugins/dalton/skills/dalton-financial-lenses/references/hidden-assets.md`
- `plugins/dalton/skills/dalton-financial-lenses/references/shareholder-return.md`（R1/R2/R3 评分细则，移植自 `findata-return/references/scoring.md`）
- `plugins/dalton/skills/dalton-financial-lenses/references/output-schema.md`（三把镜头的 JSON 输出字段）
- `plugins/dalton/skills/dalton-financial-lenses/scripts/return_score.py`（移植 `findata_return_cli.py score`，只用标准库）
- `plugins/dalton/skills/dalton-financial-lenses/scripts/anchors.py`（统一市值、汇率、报告期锚点；移植 `analyze` 的市值和汇率部分并修 bug）
- `plugins/dalton/skills/dalton-financial-lenses/scripts/test_return_score.py`（回归样例，见验收）

**具体做法**
1. **锚点先行**（`anchors.py`）。输入 `--ticker --period-end [--market-cap-usd-mn]`，输出 JSON：`market{US|HK|CN}`、`listing_currency`、`financial_currency`、`fx{pair, rate, as_of, source}`、`market_cap{value_usd, price_local, price_usd, shares, as_of, source}`、`period_end`。三把镜头和后续所有数字共用这一组锚点；差异要写明。修掉原脚本的两个 bug：
   - 港股报表币种是人民币时，汇率按 `financialCurrency`（CNYUSD）取，不按挂牌币种（原脚本对港股一律用 HKDUSD）；
   - `price_usd` 要真的换算成美元（原 hiddenasset 脚本把本币价格填进了 `price_usd`）。
   第一条修正在美股样本上用不到，代码照写，P6 接港股时验证。数据：行情默认 yfinance（FMP 以后可选），股本用最新 10-Q/10-K 封面股数（`dalton_sec.get_company_facts` 的 `dei:EntityCommonStockSharesOutstanding`）优先，yfinance 只作交叉核对。
2. **净现金双口径**（`references/netcash.md`）。原 skill 的 SKILL.md 和脚本内置提示词口径打架（受限资金是否计入基础口径、严格口径是否扣“不自由现金”、租赁是否算债、有没有“大存大贷”检查）。本文采用 SKILL.md 版本（它更保守、和基金方法一致），按此实施；owner 的确认列在文末待决问题：
   - 基础现金端：现金及现金等价物（含受限资金）、定期存款、FVPL/FVOCI 中明确属于理财、货币基金、低风险高流动性金融资产的部分；不纳入长久期投资、权益类投资。
   - 基础债务端：有息银行借款（短期+长期）、公司债、可转债、其他明确有息融资。
   - `基础净现金 = 基础现金端 − 基础债务端`。
   - 严格净现金：在基础之上扣减类债务项目和不自由现金，按行业逐项列示：教育（递延收入、预收学费、退款负债）；医美/连锁/服务（会员储值、合同负债、预收款）；制造/经销（融资性应付票据）；所有行业的受限存款、质押存款从现金端剔除。`严格净现金 = 基础净现金 − 类债务项目 − 不自由现金`。
   - 强制“大存大贷”检测：大额定存与大额借款并存时，判断是否存贷套利结构，估算真正自由现金，标注“大存大贷”。
   - 租赁资产/负债不参与计算，但列示金额备查。
   - 营运资本 = 流动资产合计 − 流动负债合计，列出所用科目。其他应付/其他应收超过流动负债/流动资产 20% 时提示（可能藏合同负债、应付票据、经销商保证金、预付款、关联方借款）。
   - 现金质量评级：`clean` / `moderate` / `complex`（定义照搬原文）。
   - 每一项扣减都列 `item, value_local, value_usd, include, reason`。
3. **隐含资产**（`references/hidden-assets.md`）。四类纳入：A 联营/合营权益；B 上市参股股权；C 投资性房地产（仅可分离部分）；D 其他可变现非核心资产（采矿权、FVPL 金融资产、可交易税收抵免等）。默认排除经营性厂房设备、使用权资产、商誉、普通品牌价值。估值：上市股权=现价×持股，再打折；上市联营=市值×持股比例；非上市且无市场锚的，写“无可靠独立估值依据”，不硬给数。折扣表照搬（上市可自由交易 10–20%；锁定/控制权 20–40%；投资性房地产 20–40%；上市联营 20–30%；非上市联营 30–50%）。`折后 = 折前 × (1 − 折扣)`；折后/市值 >10% 为 significant，3–10% moderate，<3% minimal。账面值低于市值 0.5% 的资产不检索、按账面值和标准折扣处理。隐含资产要和净现金去重（理财类 FVPL 已计入现金端的不能再算隐含资产）。
4. **股东回报 R1/R2/R3**（`references/shareholder-return.md`，原文移植）：
   - 必算 7 项：过去 12 个月已实现分红率、回购率、总回报率；最近三个完整年度每年总回报率及三年平均；未来 12 个月预计分红率、回购率、总回报率。分母统一为锚点里的当前美元市值，不回溯历史市值。
   - 规则：只算实际执行的回购，授权不算；分红按**付款日**归入窗口；特别股息计入当年，但预测时只有连续两年以上有特别股息才按最近一次计入，否则记 0 并写明；股票股利不计。
   - 回购性质三选一：真实股东回报型 / SBC 或员工激励稀释对冲型 / 混合型。真实回报比例 = 1 − 用于员工计划再发行的股数 / 回购股数（股数口径）；拆不出时用 SBC 费用/回购金额近似（费用口径）；都算不出时混合型按 50%，并注明口径。SBC 年稀释比例能算就必须写。
   - `R1 = 已实现分红率 + 回购率 × 真实回报比例`；`R2 = 三年总回报率算术平均`；`R3 = 未来 12 个月名义总回报率 × 政策可信度`；`R = max(R1, R2, R3)`。政策可信度五档：100%（明确金额和时间，或同一量化政策连续两年兑现）、90%（明确比例或金额且已有执行）、75%（正式量化政策尚未执行，或近期有执行但未来金额不明）、60%（仅方向性政策且历史执行少）、50%（仅授权或口头表态）。
   - `基础分 = min(R, 20)`（R=7.5% 记 7.5 分）；持续性调整 −1 至 +2，同一因素不重复加减；`最终分 = clamp(基础分 + 调整, 0, 20)`，**只在最后一步四舍五入到一位小数**。
   - 输出两张表，期间固定六行：FY-3、FY-2、FY-1、三年平均、过去 12 个月（至日期）、未来 12 个月（预测）。表 1 比率（期间 | 分红率 | 回购率 | 总股东回报率 | 主要依据 ≤25 字），表 2 绝对金额（USD 百万：期间 | 分红 | 回购 | 合计 | 备注）。另列回购性质与真实回报比例、SBC 稀释、最新回购执行（授权余额、最近一季金额、节奏）、管理层最新回报政策（中文 + 原文出处）、未来 12 个月预测依据、评分拆解（R1、R2、R3、可信度档位与理由、R、基础分、每项调整一句、最终分）。
   - 美股数据：现金流量表 `PaymentsForRepurchaseOfCommonStock`、`PaymentsOfDividends`、`ShareBasedCompensation`、股本变化；10-K/10-Q 正文检索 “share repurchase program”。同一份 filing 内交叉核对单位。
5. `return_score.py`：CLI `--ttm-div --ttm-buyback --true-return-share --annual a,b,c --fwd-total --credibility {100,90,75,60,50} --adjust "+1:理由" ...`，输出 JSON（`R1, R2, R3, R, driver, base_score, adjustment, adjustment_clamped, final_score, max_score=20, rationale_zh`）。修掉原脚本的两个问题：`rationale_zh` 里的调整分按实际小数显示（原 `{adj:+.0f}` 会把 +0.5 显示成 +0/+1）；文档示例和回归值统一。
6. **SKILL.md 流程**（智能体照做）：
   1. 先读 `dalton-conventions`；确认要哪几把镜头（被其他 skill 调用时默认三把都做）。
   2. 跑 `anchors.py`，锁定报告期、汇率、市值。
   3. 取最新 10-Q/10-K 资产负债表科目（`dalton_sec.get_company_facts`，按 R1–R10 规则带单位与期间），附注细节用 `researchdata.finance(route="sec")` 的 read-item / search-text 读正文（大型 filing 的长文读取由桥给到较长超时），逐项归类。
   4. 按 references 算三把镜头，回报评分用 `return_score.py` 算，不手算。
   5. 结果写 `research/<日期>-lenses/lenses.json`（字段见 `output-schema.md`）和 `drafts/<日期>-lenses.md`。被 workflow 调用时只返回 JSON 路径和一段 ≤10 行摘要。
   6. 自检：三把镜头锚点一致；每个科目有 filing 出处（表名、期间）；隐含资产与现金去重；回购只计执行。

**输出模板**（`drafts/<日期>-lenses.md`）
```
# {公司}（{TICKER}）财务镜头｜报告期 {period_end}
锚点：市值 {x} 亿美元（{日期}，{来源}）；汇率 {pair}={rate}（{日期}）；报表币种 {ccy}
## 一、净现金
- 基础净现金 {x}（占市值 {y}%）；严格净现金 {x}（占市值 {y}%）；现金质量 {clean/moderate/complex}
- 现金端 / 债务端 / 严格口径扣减（逐项：科目｜本币｜美元｜是否计入｜理由）
- 营运资本；其他应付/应收提示；大存大贷；租赁备查
## 二、隐含资产
- 结论：{significant/moderate/minimal/none}，折后合计 {x}（占市值 {y}%）
- 逐项：类型｜账面值｜估值依据｜折前｜折扣与理由｜折后｜折后/市值｜兑现难度
## 三、股东回报
**评分：{x.x} / 20**（R={r}%，由 {R1/R2/R3} 驱动）
- 表 1、表 2（六行）
- 回购性质与真实回报比例；SBC 稀释；最新回购执行；管理层政策；未来 12 个月依据；评分拆解
## 口径与未核实项
## 引用清单
```

**知识层写入** 无直接写入。数值进入调用方的数字底表；full-coverage 用它填 `net_cash_strict`、`net_cash_basic`、`hidden_asset_net`、`return_r1/r2/r3`、`return_score`。

**发布** 不单独发布，作为 initial-coverage / full-coverage 的组成部分。

**与 LangAlpha 自带的关系** 新建。自带的 dcf-model 只要求“net debt”一个锚点，没有严格口径、隐含资产和回报评分；本 skill 的锚点 JSON 可以直接喂给 dcf-model 的 equity bridge（净现金用严格口径，另列基础口径做敏感度）。

**验证方式（真实运行）**
- `python scripts/test_return_score.py`：原脚本的自测样例（ttm_div 4.73、ttm_buyback 6.70、真实回报比例 0.78、三年 11.24/11.88/15.05、forward 14.61、可信度 90、调整 ±1）必须得到 R3≈13.15、由 R3 驱动、最终 13.1；另加一个 +0.5 调整的样例检查显示。
- 在 META 和 P2 样本公司工作区各跑一次 `/dalton-financial-lenses {TICKER}`。（港股样例 0700.HK 的 CNYUSD 汇率检查移到 P6。）

**验收标准**
- META 的三把镜头：每个科目能回指 10-Q/10-K 的表名和期间；回购只计执行；评分拆解完整；PM 抽查 5 个数字全部对得上原件。

**风险与注意**
- 净现金口径已按 SKILL.md 版本实施，owner 确认见文末待决问题；若 owner 改口径，只改 `references/netcash.md`。
- 港股、A 股不在本阶段；遇到非美股时 skill 直接说明“P6 支持”，不硬算。

**预估工时** 2.25 天。

---

## PS.3 `dalton-company-background`：公司背景 9 段

**目标**
回答“这家公司是做什么的”，给出 9 段标准背景：一手财务数据（3 个已审计财年 + 4 个报告季度）、业务构成、商业模式与竞争、看点与风险。它是初次覆盖的第一块砖，也可以单独用。

**前置依赖** PS.0；PS.2 的 `anchors.py`（市值、汇率）。

**来源（只借原理）** `~/.openclaw/workspace/skills/company-background/SKILL.md`（9 段模板、6 项质量门、执行守则）。批量模式（3 并发、800 字下限、进度文件）不迁移，批量需求由 workflow 处理。

**新增/改动文件**（新建）
- `plugins/dalton/skills/dalton-company-background/SKILL.md`
- `plugins/dalton/skills/dalton-company-background/templates/background.md`

**具体做法（SKILL.md 流程）**
1. 读 `dalton-conventions` 和本工作区知识层；若 `agent.md` 已有背景稿且不到 90 天，只做增量（新季度、新分部口径）。
2. 一手财务：
   - 美股：最近 3 个已审计财年（10-K）分部收入和占比、营收、毛利率、净利；最近 4 个报告季度（10-Q/8-K EX-99.1）营收、同比、环比、净利。`dalton_sec.get_company_facts` 优先（注意 companyfacts 没有分部维度，分部数据读 10-K/10-Q 的分部附注正文），自带 `get_sec_filing` 与 fundamentals MCP 次之，yfinance 只做交叉核对，不冒充一手。
   - 港股/A 股：本阶段不做，P6 支持。
   - 缺的年份或季度写明缺什么、为什么缺。
3. 至少三轮独立检索：基础信息、财务细节、客户与竞争对手。检索源：`researchdata.research(route="company-wiki")`（公司页和行业页）、`researchdata.research`（sales-notes、tmtb、funda-ai、alphaengine 的公司深度报告）、公司 IR、web_search。二手结果与一手披露交叉验证，不一致时保留差异并标口径和日期。
4. 非美元金额括号内给美元换算（用 PS.2 的同一汇率锚点）。没有公开分部拆分写“未公开披露分部拆分”。
5. 写 9 段（模板见下），每个数字带来源、期间、单位；无来源的数字不出现，训练记忆里的事实不用。
6. 自检六项（照搬原质量门）：3 个财年、4 个季度、3 轮检索、交叉验证、9 段齐全（缺数据也保留段落并写缺口）、数字溯源。

**输出模板**（`drafts/<日期>-background.md`）
```
# {公司名}（{TICKER}）业务概览
**1. 一句话定位**
> ……
**2. 核心业务构成（最新财年 FY{yyyy}，收入金额与占比）**
- **{分部 A}**：{x} 亿美元（{y}%）—— {说明}
**3. 年度营收与盈利趋势（最近 3 个已审计财年）**
- **FY{yyyy}**：{x} 亿（约 USD {y} 亿）｜YoY {z}%｜毛利率 {a}%｜净利 {b} 亿
**4. 最近 4 个报告季度**
- **{Qn FYyyyy}**：{x} 亿｜YoY｜QoQ｜净利
**5. 资产负债表关键数据**：总资产、股东权益、总债务、现金、净现金/净负债（引用 PS.2 基础口径）、D/E
**6. 市场数据（注明日期和来源）**：股价、市值、52 周高低、PE（TTM）、流通股
**7. 商业模式与竞争**：模式｜核心客户｜核心供应商｜对标对手
**8. 核心看点**（具体、有数据）
**9. 核心风险**（具体、量化，绑定业务变量）
*资料来源：filing（期间、表名）；市场数据（日期）；其他原始链接；汇率来源和日期*
```

**知识层写入** 若 `agent.md` 没有“业务一句话”，写入；不改 thesis。被 initial-coverage 调用时由 workflow 汇总后统一写。

**发布** 草稿。不单独发布。

**与 LangAlpha 自带的关系** 新建。自带 `company-profile` 只出一页幻灯片，保留作附件（蓝图 §4.5）；自带 `initiating-coverage` Task 1 是 6,000–8,000 字的研究文档，过重，不适合作为初次覆盖的第一步。

**验证方式（真实运行）** 在 PS.18 指定的 P2 样本公司工作区跑 `/dalton-company-background {TICKER}`；另在 META 跑一次，和 P1 迁入的认知对照，看有无矛盾。

**验收标准** 9 段齐全；3 个财年和 4 个季度数字逐一可回指 filing；PM 抽 5 个数字全对；“看点”和“风险”每条都绑定一个业务变量（不接受“宏观不确定”这类泛泛风险）。

**风险与注意** 季度口径要用公司的财季标签（FY 和 CY 不同的公司，例如 MSFT 6 月财年）；分部重分类时要标注可比性断点。

**预估工时** 1.5 天。

---

## PS.4 `dalton-bullbear`（对话快速版，默认）+ workflow `bullbear-doc`（v4.2 完整成稿）

**目标**
PM 问“X 的多空在争什么”时，十几分钟内在对话里给出判断先行、带来源的快速版；需要成稿时，跑 workflow 产出 v4.2 结构的完整文档。两种形态共用同一套方法：共识（price in 了什么）→ 真正的分歧 → 盈利预测修正地图 → 催化剂 → 改判条件。

**前置依赖** PS.1（审稿人、取证员）、PS.2（股东回报、净现金锚点）；`researchdata.finance(route="estimates")`（eps_trend、修正次数）。

**来源（只借原理）** `~/.openclaw/agents/main/agent/workshop-skills/bullbear-discussion/`（SKILL.md、`references/common_task.md`、`return_task.md`、`reformat_task.md`、`scripts/render.py` 的格式检查、`scripts/fincheck.py` 的财务体检规则）。

**新增/改动文件**（新建）
- `plugins/dalton/skills/dalton-bullbear/SKILL.md`（快速版流程 + 何时转成稿）
- `plugins/dalton/skills/dalton-bullbear/references/v42-structure.md`（成稿结构与写作规则，移植 `common_task.md`）
- `plugins/dalton/skills/dalton-bullbear/references/return-branch.md`（股东回报支线规则，指向 `dalton-financial-lenses/references/shareholder-return.md`，只补“第一节 ≤40 行、附录 ≤30 行”的写法）
- `plugins/dalton/skills/dalton-bullbear/references/ai-revenue-rules.md`（AI 收入口径规则）
- `plugins/dalton/skills/dalton-bullbear/scripts/bb_check.py`（移植 `render.py check()` 的格式检查，去掉飞书分块和 `$`/`~` 禁令）
- `plugins/dalton/skills/dalton-bullbear/scripts/fincheck.py`（财务体检：资产负债表大额/跳变科目、Capex 强度；数据改用 `dalton_sec.get_company_facts` 取无维度的资产负债表与现金流科目，去掉 edgartools 依赖；科目清单在脚本里写死为 us-gaap concept 列表，取不到的科目记“底表缺”）
- `plugins/dalton/skills/dalton-bullbear/scripts/implied_model.py`（隐含费用测算：`implied_pretax = EPS×shares/(1−tax)`、`implied_total_expenses = rev − pretax + other_income`、`eps_per_10bn_expense`、`eps_per_1pct_revenue`，移植 `consensus_estimates.py implied_model`）
- `workflows/bullbear-doc/workflow.js`

**快速版流程（SKILL.md，默认路径）**
触发：`/bullbear {TICKER}`，或 PM 问“多空观点 / earnings revision / 近期催化剂”但没要文档。主智能体自己做，不派 workflow。
1. 读 `dalton-conventions`，按 `dalton-knowledge` 读序读 `thesis.md`、`debate.md`、`drivers.md`。已有 debate 的，以更新为主：哪些 debate 的倾向或置信度变了，为什么。
2. 一致预期：`researchdata.finance(route="estimates", ticker=T)` 取下一财年 EPS、收入，7/30/90 天轨迹，30 天上调/下调次数；跑 `implied_model.py` 得到隐含费用空间和敏感度。
3. 内部库：`sales-notes`、`tmtb`、`funda-ai`、`company-wiki` 路由 各查三次（公司名、核心产品、“capex/费用/预测”）。sales color 按公司名和代码分别搜、按 message_id/doc_id 去重、读全文；按正文里的身份分类，不按库的分类（sales 内容可能放在 foreign report 下）。
4. AlphaEngine 中英文各搜一次，读 3–6 篇，优先外资财报前瞻和预测调整报告（`sort=relevance`，同时给 `date_from`、`date_to`，窗口默认 92 天）。
5. 最新 8-K/10-Q 的指引原文，带 accession。
6. 按下面顺序回复（对话里，不写文件，除非 PM 要）：

```
**结论**：修正方向（上修/下修/持平）、财报风险方向、置信度（高/中/低）——一句话
**多空在争什么（估值之外）**：2–4 个 debate，每个一行：分歧变量｜Bull｜Bear｜我的倾向
**盈利预测修正三层**：收入｜费用/capex 或利润率｜一次性项目；附隐含费用空间与敏感度（[假设] 卖方费用比一致预期隐含空间多 X，对应 EPS 少 Y）
**3–6 个月催化剂**（按时间排）：事件｜日期｜检验哪个 debate
**改判条件**：什么证据出现我会换边
**来源**：每类来源命中与使用数；未取得的写明原因
```
7. 结束：快速版结论写入知识层（见下），并问 PM 是否要成稿（或按议程自动排成稿）。

**完整成稿 workflow `bullbear-doc`**
参数：`{ticker, company, market, workspace_dir, as_of, research_days: 92, prior_draft?: path}`。
```js
export const meta = { name: 'bullbear-doc', description: 'Dalton bull/bear v4.2 完整成稿：回报支线 + 主稿 + 格式检查 + 独立审稿' }
const W = args.workspace_dir, T = args.ticker, D = args.as_of
const runDir = `${W}/research/${D}-bullbear`
const conv = `先读 .agents/skills/dalton-conventions/SKILL.md 与 .agents/skills/dalton-bullbear/references/v42-structure.md。`

phase('准备')
const prep = await agent(`${conv}
为 ${args.company}（${T}）准备 bull/bear 成稿的底稿，写到 ${runDir}/：
1) 跑 .agents/skills/dalton-financial-lenses/scripts/anchors.py 锁定市值与汇率 → anchors.json；
2) 跑 .agents/skills/dalton-bullbear/scripts/fincheck.py → fincheck.md；
3) 一致预期快照 + implied_model.py → consensus.md；
4) 价格锚（收盘价、日期、来源）→ price.md。
返回各文件路径与一句话摘要。`, { agentType: 'dalton-evidence', label: `${T} 准备`, schema: PREP_SCHEMA })

phase('股东回报支线')
const ret = prep === null ? null : await agent(`${conv}
按 .agents/skills/dalton-bullbear/references/return-branch.md 写股东回报第一节（≤40 行）、附录（≤30 行）和 5 条摘要，
分母用 ${runDir}/anchors.json，评分用 return_score.py。输出 ${runDir}/return.md、return_appendix.md、return_summary.md。`,
  { agentType: 'dalton-evidence', label: `${T} 股东回报`, schema: FILES_SCHEMA })

phase('主稿')
const main = await agent(`${conv}
写 ${args.company}（${T}）Bull/Bear Discussion 主稿到 ${runDir}/main.md，严格按 v4.2 结构。
研究窗口 ${args.research_days} 天（截至 ${D}）。股东回报只引用 ${runDir}/return_summary.md，不自行计算。
${args.prior_draft ? `上一版：${args.prior_draft}，“我的判断”有变化要写原因。` : ''}
必须检索：内部库（sales-notes、tmtb、funda-ai）、company-wiki、AlphaEngine（foreign_report、sell_side_comment、meeting_minutes 分开搜）、Guidepoint（英文）、最新 filing 与电话会。
写完跑 python .agents/skills/dalton-bullbear/scripts/bb_check.py ${runDir}/main.md，直到 0 WARN。`,
  { agentType: 'general-purpose', label: `${T} 主稿`, schema: DRAFT_SCHEMA })

phase('合并与审稿')
// 合并由一个子智能体完成（脚本不能写文件）：把 return.md 插到 <!-- INSERT:return -->，附录插到 <!-- INSERT:return_appendix -->，
// 输出 drafts/${D}-bullbear.md，再跑 bb_check.py。
const merged = main === null ? null : await agent(`合并 ${runDir}/main.md 与回报支线文件 → ${W}/drafts/${D}-bullbear.md，跑 bb_check.py 到 0 WARN，返回路径和 sha256。`,
  { agentType: 'general-purpose', label: `${T} 合并`, schema: FILES_SCHEMA })
const review = merged === null ? null : await agent(`审阅 ${W}/drafts/${D}-bullbear.md（支线材料在 ${runDir}/）。按你的审稿清单输出。重点：每个 Debate 的“我的判断”是否有判断、改判条件是否可观察；§2 是否把价格走势当共识；§4 修正分层是否能回指一致预期快照。`,
  { agentType: 'dalton-reviewer', label: `${T} 审稿` })

phase('修订')
const fixed = review === null ? null : await agent(`按审稿意见修订 ${W}/drafts/${D}-bullbear.md：P0 全改，P1 能改就改，不新增未核实数字。审稿意见如下：\n${review}\n返回改动清单与剩余 P1。`,
  { agentType: 'general-purpose', label: `${T} 修订` })
return { draft: `${W}/drafts/${D}-bullbear.md`, review_verdict: review ? review.split('\n').find(l => l.includes('可发布') || l.includes('需修订') || l.includes('不得发布')) : null, fixed, failed: [prep, ret, main, merged, review].map(x => x === null) }
```
（`PREP_SCHEMA`、`FILES_SCHEMA`、`DRAFT_SCHEMA` 在脚本顶部定义，均为 `{type:'object', properties:{files:{type:'array',items:{type:'string'}}, summary:{type:'string'}, gaps:{type:'array',items:{type:'string'}}}, required:['files']}` 一类的小 schema，满足 4096 字节和 32 键上限。派发数 6。）

**v4.2 成稿结构**（`references/v42-structure.md`，标题顺序逐字）
```
# {公司名}（{TICKER}）Bull / Bear Discussion
核心判断：……（立场、依据、投资含义，一段）
研究窗口：{start} 至 {end}｜价格锚：{价格}（{日期}，行情 [引用1]）｜信源覆盖：一行
<!-- INSERT:return -->
## 一、股东回报            （来自支线，≤40 行；评分：x.x / 20）
## 二、市场共识：现在 price in 的是什么
- **看多的人默认成立**：（2–4 条子弹，每条 ≤45 字，带来源）
- **看空的人默认成立**：（2–4 条）
- **两边都不争的**：（1–3 条）
## 三、Top N debates：真正有分歧的地方
### Debate k：{可证伪问题}（{≤12 字短名}）
- **分歧**：……
- **Bull 怎么看**：2–4 条，最后一条固定为“比共识乐观在哪：……”
- **Bear 怎么看**：2–4 条，最后一条固定为“比共识悲观在哪：……”
- **双方最强反驳**：“Bull 对 Bear：……”“Bear 对 Bull：……”
- **我的判断**：“立场与置信度：偏 Bull / 偏 Bear / 中性，高 / 中 / 低（和上一版比有变化就说原因）”“依赖假设：[假设] ……（推导依据）”“改判条件：……”
## 四、Earnings revision map
- **一致预期现状**：下一财年 EPS 与收入、7/30/90 天轨迹、30 天上调/下调次数、买方 whisper（有则写）
- **修正分层**：收入 / 费用、capex 或利润率 / 一次性项目，按 EPS 影响排序
- Revenue、Margin、EPS 驱动各 ≤3 条，写成“变量 → Debate N 短名 → 敏感度”；EPS 只列税率、利息、股本、一次性项目
- [假设] 卖方费用比一致预期隐含空间多 X，对应 EPS 少 Y（快照敏感度）
## 五、Catalyst & KPI
- 3 / 6 / 12 个月分组：事件｜日期或窗口｜检验哪个 debate｜bull/bear 阈值（[假设]）
- KPI 3–5 个：当前值、bull 区间、bear 区间
- 若 3 个月内有财报：日期、过去 4 次财报次日涨跌、持仓情况、哪次财报通常给下一年指引 → 财报风险方向与置信度
## 六、财务体检（≤12 行，结论先行）
- **结论**：触发项数量与关键项
- **核心项目**：“**科目**：金额、占总资产比例、同比 → 机制 → 关联 debate 短名”
- **其他触发项**；**Capex**：三年，“Capex 相当于净利润的 x%”，与同年 D&A 对比
- 无触发写“资产负债表没有触发提示项”；不重复现金、有息债务、递延收入
## 七、Variant perception
- 只写一个观点：市场为什么没看到（信息盲区 / 时间维度错配 / 叙事惯性），体现在第四节哪个变量，何时验证
## 八、一句话
- “The stock works if X; it breaks if Y; the key debate is Z.”，每次提到 debate 都带条件“条件（debate N 短名）”
[可选专题节]
<!-- INSERT:return_appendix -->
## 九、股东回报附录
## 十、引用清单
- [引用N] 来源类型｜机构/作者｜标题或内容概述｜日期｜文档 id 或链接；超 3 个月加“（超 3 个月）”
- 信源覆盖段：各来源命中与使用；未覆盖的写原因
```
写作规则（移植自 `common_task.md`）：一句一个来源，来源标签放句末；全文同一来源的同一数字只出现一次；四到八节用“Debate N 短名”回指，不重复解释；每个数字带期间；“我的判断”必须有立场，只列局限不下判断的段落算 P1。AI 收入规则（`ai-revenue-rules.md`）：同一句里区分已确认收入、ARR、TCV；用 filing 全文检索产品与合作方名称，记下 RPO、付款方、现金还是股权结算、退出条款；“AI 提效”之前先重算两年 GAAP 和 non-GAAP 费用增速，区分平台迁移节省与 AI 提效。

`bb_check.py` 检查项（移植，去掉飞书相关）：正文引用编号集合 = 引用清单集合；回报附录同理；股东回报节 ≤48 行；§2 三个父条目且子条目数 2–4/2–4/1–3；§3 每个 Debate 五块齐全；§6 ≤12 行；§4 含“→”的行必须含“→ Debate N”；§8 不出现“Debate N 和/与/同时成立”这类裸 debate 写法；引用清单之后不能再有正文节；不出现“你的判断”。

**知识层写入**（主智能体；成稿 workflow 结束后由 `dalton-bullbear` 收尾）
- `knowledge_update(doc="debate")`：每个 debate 一条（分歧变量、多方、空方、最强反驳、我的判断、改判条件、来源）；倾向或置信度变了用 `set`，服务端自动在 `debates[].history` 留旧值和原因。
- `knowledge_update(doc="drivers")`：§4 的驱动变量和敏感度、§5 的 KPI 及 bull/bear 区间（新读数用 `append`）。
- `knowledge_append_verdict`：trigger.kind=`manual`（PM 发起）或 `agenda`，verdict 按与上一版 debate 的比较给出（`NO_CHANGE` / `THESIS_STRENGTHENED` / `THESIS_WEAKENED` 等正式词形）；`agenda_ops` 把 §5 的催化剂写进 `watch.events`，改判条件写进 `next_validations`。

**发布** 快速版不发布。成稿默认草稿（草稿期可用 `check_draft` 看数字核验反馈，不阻止任何事）；PM 要求时走 `publish_report(kind="bullbear")`（PK 已加此 kind）。

**与 LangAlpha 自带的关系** 新建。自带 `trade-pitch` 的情景与表达方式只在 §7–§8 借用；自带 `earnings-analysis` 的 debate map 表（Side｜一句话｜本季变化｜证伪｜催化剂）是单季视角，不适合作为多空主文档。

**验证方式（真实运行）**
- 快速版：META 工作区 `/bullbear META`，计时，检查回复结构和来源说明。
- 成稿：在 P2 样本公司跑 `RunWorkflow(workflow="bullbear-doc", params={...})`，检查 6 次派发全部非 null、`bb_check.py` 0 WARN、草稿期审稿已执行（审稿人模型以 PK.10 配置为准）。

**验收标准** 见 PS.18 的 B1、B2 条目。硬性：无编造数字；每个 Debate 有立场、置信度和可观察的改判条件；§4 的一致预期数字能回指快照文件。

**风险与注意**
- 快速版的价值在速度：不要在快速版里跑 fincheck 和股东回报。
- 旧版 debate（P1 迁入）与新结论冲突时，必须写“和上一版比的变化及原因”，不能直接覆盖。

**预估工时** 4 天（快速版 1 天，成稿 workflow 与脚本 3 天）。

---

## PS.5 workflow `initial-coverage`（P2 初次覆盖）

**目标**
PM 指派或研究总监按议程安排一家新公司时，一次运行产出：初次覆盖报告（12 问初筛形式）+ 初始化的公司知识层（thesis、debate、drivers、management 初稿、agenda）。对应蓝图 §1.1 的“初次覆盖”：主营、行业特性（是否周期）、长短期驱动、竞争位置与壁垒（形成还是被破坏）、历史股价与估值驱动、当前 bull/bear debate、股价位置、叙事演绎、高 conviction 的预期差（含宏观驱动）。

**前置依赖** PS.1–PS.4；公司工作区已建（P1 的建工作区脚本，或 Chief of Staff 的 `manage_workspaces`）；PK 的知识层文件初始化方式。

**新增/改动文件**（新建）
- `workflows/initial-coverage/workflow.js`
- `plugins/dalton/skills/dalton-initial-coverage/SKILL.md`（启动 skill：问清参数 → 读 run-workflow → 调 RunWorkflow → 运行结束后检查产物、写知识层、向 PM 汇报）
- `plugins/dalton/skills/dalton-initial-coverage/references/branch-industry.md`（行业支线规则）
- `plugins/dalton/skills/dalton-initial-coverage/references/branch-valuation-history.md`（估值历史与可比支线规则）
- `plugins/dalton/skills/dalton-initial-coverage/references/screen-12q.md`（12 问初筛写法）
- `plugins/dalton/skills/dalton-initial-coverage/templates/initial-coverage.md`

**输入参数** `{ticker, company, market, workspace_dir, as_of, peers?: [...], sector?: string, pm_question?: string}`。`peers` 不给时由行业支线自己定，并说明选取理由。

**流程**
```
阶段 1（parallel，4 支线，同时跑）
  A 公司背景        → dalton-company-background 方法 → drafts/<D>-background.md
  B 行业支线        → research/<D>-ic/industry.md
  C 财务镜头        → dalton-financial-lenses → research/<D>-ic/lenses.json + drafts/<D>-lenses.md
  D 估值历史与可比  → research/<D>-ic/valuation.md（+ research/<D>-ic/comps.xlsx）
阶段 2  bull/bear 快速版（书面）→ research/<D>-ic/bullbear-quick.md（读 A–D 的产物）
阶段 3  12 问初筛成稿 → drafts/<D>-initial-coverage.md
阶段 4  知识层初始化草案 → research/<D>-ic/knowledge-proposal.json（由启动 skill 的主智能体审读后写入）
阶段 5  审稿（dalton-reviewer）→ 修订
```
派发数：4 + 1 + 1 + 1 + 1 + 1 = 9。

**B 行业支线规则**（`branch-industry.md`，取 deep dive A 支线的精简版）
1. 行业定义与 TAM：至少两种方法（自上而下；自下而上 单价×数量×渗透率；成熟市场类比），过去 5 年增速与未来 3–5 年预测，标来源与年份。
2. 行业特性：是否周期、周期由什么驱动（需求、产能、价格、信贷、政策）、2–3 个可跟踪的高频指标。
3. 竞争格局：5–8 家主要玩家的份额与 3 年趋势；同业的现取财务（市值、收入及同比、毛利率、净利率、主要倍数），每个数字写（期间，来源）。
4. 壁垒：类型（网络效应、规模、转换成本、品牌、牌照、技术、成本）＋“正在形成还是正在被破坏”的判断＋一个可证伪指标。
5. 行业专用框架（按类型选一个）：互联网 MAU×变现×take rate；电商 GMV×take rate×履约；SaaS ARR×NRR、Rule of 40、LTV/CAC；半导体/硬件；消费品牌；连锁的单店模型；物流。
6. 长短期驱动：短期（未来 2–4 个季度）和长期（3–5 年）各列 2–4 个，标“结构变化”还是“周期波动”。
7. 宏观敏感度：利率、汇率、消费、企业 IT 支出、政策中与本公司相关的 1–3 项，说明传导路径。
8. 来源：`company-wiki` 路由的行业页优先（基金已有认知），其次 `researchdata.research`（alphaengine 行业报告、sales-notes、funda-ai）、Guidepoint 专家纪要（英文检索，引用 ≤20 个英文词）、官方或协会数据、web_search。

**D 估值历史与可比规则**（`branch-valuation-history.md`）
1. 过去 3 年（不足则上市以来）股价、远期 P/E（或 EV/Sales、EV/EBITDA，按行业选）、远期 EPS 一致预期的路径。数据：`get_daily_prices`、一致预期快照历史【P0D 若无历史一致预期，用 AlphaEngine 历史报告的 EPS 预测点或财报季的实际 EPS 近似，并标明】。
2. **“EPS 驱动还是倍数驱动”分解**：按年（或按重要事件区间）`ln(P1/P0) = ln(EPS1/EPS0) + ln(PE1/PE0)`，EPS 用当时的远期 12 个月一致预期，给表：区间｜股价涨跌｜EPS 预期变化贡献｜倍数变化贡献｜主导事件。这是蓝图 §2 指出的缺口。
3. 股价位置：当前倍数在自身历史区间的分位、相对同业的折溢价、距 52 周高低。
4. 可比表：调用自带 `comps-analysis` 的方法（`.agents/skills/comps-analysis/SKILL.md`），3 家以上一档同业，输出 `research/<D>-ic/comps.xlsx`；初次覆盖用 screen-grade 即可。
5. 重要事件复盘：过去 3 年 3–6 个让股价重估的事件（财报、指引、产品、监管、宏观），区分经营变化与市场重估。

**12 问初筛**（`screen-12q.md`；问题来自 Playbook v1 深度认知 12 问，与 full-coverage 的 12 问同一套【题目原文以 `dalton-conventions` 为准】，下面用 deep dive 的 12 问作为默认）
初筛只要求每题给出“当前判断 + 关键证据 + 置信度 + 还缺什么”，重点题 Q1–Q5、Q7、Q8、Q12 必须实答，其余可以写“初筛暂不展开：缺什么、计划在完整覆盖哪条支线补”：
- Q1 这家公司靠什么赚钱？
- Q2 关键驱动变量是什么，趋势如何？
- Q3 市场预测里藏着哪些隐含假设？
- Q4 管理层和企业文化值得信任吗，股东如何受益？
- Q5 股价为什么在当前位置？
- Q6 哪些高频数据能帮助我们检验判断？
- Q7 市场共识、多方和空方分别怎么看？
- Q8 做多、做空还是观察，我们的差异化观点是什么？
- Q9 催化剂如何使判断兑现？
- Q10 投资判断需要多长时间兑现？
- Q11 需要对冲吗，应该对冲什么风险？
- Q12 我们可能错在哪里，遗漏了什么风险？

**输出模板**（`templates/initial-coverage.md`）
```
# {公司}（{TICKER}）初次覆盖
基本信息：代码｜日期｜股价（日期）｜市值｜30 日均成交额｜行业｜是否周期
## 核心判断（≤3 句）
一句话认知 → 当前倾向（关注 / 观察 / 暂不覆盖 / 建议完整覆盖）→ 最大的不确定性
## 一、12 问初筛
### Q1. 这家公司靠什么赚钱？
判断｜关键证据（来源）｜置信度｜还缺什么
……（Q1–Q12）
## 二、业务与行业（摘要，详见背景稿与行业支线）
主营与收入结构｜行业特性与周期｜长短期驱动｜竞争位置与壁垒（形成/被破坏）
## 三、股价与估值
历史上 EPS 驱动还是倍数驱动（分解表）｜当前位置（分位、相对同业）｜关键重估事件
## 四、当前 debate（bull/bear 快速版）
## 五、叙事演绎与预期差
市场当前叙事 → 叙事怎么演变而来 → 我们可能的预期差（含宏观驱动）→ 要验证什么
若无足够预期差：写“暂无高 conviction”，并写在等什么
## 六、财务镜头（净现金双口径、隐含资产、股东回报评分）
## 七、初始 thesis 草案
支柱（3–5 条，每条：主张、当前证据、确认/警告/证伪阈值）
## 八、研究议程
完整覆盖要回答的 3–5 个关键问题｜下一项验证｜待观察事件与日期
## 引用清单
```

**知识层写入**（启动 skill 的主智能体在 workflow 结束后执行；有 PM 在场时先展示再写，自动运行时直接写工作版本）
- 读 `research/<D>-ic/knowledge-proposal.json`，按 doc 逐个调用 `knowledge_update`（新工作区的 doc 不存在时 `base_version=null` + `create`，已有时用 `insert`/`append`/`set`）：
  - `company`：一句话判断、行业、是否周期、`coverage_stage=initial`（同步工作区描述）；
  - `thesis`：第七节的 3–5 条支柱（每条 confirm/warn/break 阈值），证据台账首批条目来自报告引用（`source.type`/`tier` 对应）；`house_view` 初值（reason ≥30 字）；
  - `debate`：第四节的 debate；
  - `drivers`：行业支线的驱动、高频指标、宏观敏感度，D 支线的“EPS 还是倍数驱动”结论；
  - `management`：管理层与指引风格的初步记录（标“初筛，待完整覆盖核实”）；
  - `agenda`：第八节（完整覆盖要回答的问题、下一项验证、待观察事件与日期）。
- `knowledge_append_verdict`：一条 `NEW_THESIS`（新论点），trigger.kind=`manual` 或 `director`。

**发布** 草稿。PM 盲评“可用”后可按 PM 意愿发布（`publish_report(kind="initial_coverage")`）；P2 的完成标准不要求正式发布（蓝图 §5）。

**与 LangAlpha 自带的关系** 新建。自带 `initiating-coverage` 是 5 个任务、30–50 页卖方式报告，一次一任务、不用子智能体；Dalton 的初次覆盖是买方初筛，要的是认知和议程，不是 30 页报告。自带的 `comps-analysis` 在 D 支线里直接复用。

**workflow 脚本骨架**
```js
export const meta = { name: 'initial-coverage', description: 'Dalton 初次覆盖：背景∥行业∥财务镜头∥估值历史 → bull/bear 快速版 → 12 问初筛 → 知识层草案 → 审稿' }
const { ticker: T, company: C, workspace_dir: W, as_of: D } = args
const R = `${W}/research/${D}-ic`
const base = `先读 .agents/skills/dalton-conventions/SKILL.md；公司 ${C}（${T}），市场 ${args.market}，截至 ${D}。`
const OUT = { type: 'object', properties: { files: { type: 'array', items: { type: 'string' } }, summary: { type: 'string' }, gaps: { type: 'array', items: { type: 'string' } } }, required: ['files', 'summary'] }

phase('取证')
const [bg, ind, lens, val] = await parallel([
  () => agent(`${base} 按 .agents/skills/dalton-company-background/SKILL.md 写背景稿到 ${W}/drafts/${D}-background.md。`, { agentType: 'dalton-evidence', label: '公司背景', schema: OUT }),
  () => agent(`${base} 按 .agents/skills/dalton-initial-coverage/references/branch-industry.md 写行业支线到 ${R}/industry.md。${args.peers ? '同业候选：' + args.peers.join(',') : ''}`, { agentType: 'dalton-evidence', label: '行业支线', schema: OUT }),
  () => agent(`${base} 按 .agents/skills/dalton-financial-lenses/SKILL.md 做三把镜头，JSON 到 ${R}/lenses.json，草稿到 ${W}/drafts/${D}-lenses.md。`, { agentType: 'dalton-evidence', label: '财务镜头', schema: OUT }),
  () => agent(`${base} 按 .agents/skills/dalton-initial-coverage/references/branch-valuation-history.md 写估值历史与可比到 ${R}/valuation.md，可比表 ${R}/comps.xlsx。`, { agentType: 'dalton-evidence', label: '估值历史与可比', schema: OUT }),
])
const got = { bg, ind, lens, val }
const missing = Object.entries(got).filter(([, v]) => v === null).map(([k]) => k)
if (missing.length === 4) return { ok: false, reason: '四条支线全部失败', missing }
log(`支线完成 ${4 - missing.length}/4，缺：${missing.join(',') || '无'}`)

phase('bull/bear 快速版')
const bb = await agent(`${base} 读 ${R}/ 下全部支线文件与 ${W}/drafts/${D}-background.md，按 .agents/skills/dalton-bullbear/SKILL.md 的快速版方法写成文字版到 ${R}/bullbear-quick.md（可补充检索内部库与 AlphaEngine）。缺失支线：${missing.join(',') || '无'}。`, { agentType: 'general-purpose', label: 'bull/bear 快速版', schema: OUT })

phase('12 问初筛')
const draft = await agent(`${base} 按 .agents/skills/dalton-initial-coverage/references/screen-12q.md 和 templates/initial-coverage.md，综合 ${R}/ 全部文件，写 ${W}/drafts/${D}-initial-coverage.md。缺失支线对应的问题写“缺口：需要什么、已尝试路径”。`, { agentType: 'general-purpose', label: '初筛成稿', schema: OUT })

phase('知识层草案')
const kp = draft === null ? null : await agent(`${base} 从 ${W}/drafts/${D}-initial-coverage.md 抽取知识层草案，按 PK.2 的 JSON Schema（.agents/skills/dalton-knowledge/SKILL.md 有字段说明）写 ${R}/knowledge-proposal.json，结构为 {doc: [ops]}，可直接交给 knowledge_update（thesis 支柱、debates、drivers、management 初记、agenda）。只抽取，不新增判断。`, { agentType: 'general-purpose', label: '知识层草案', schema: OUT })

phase('审稿')
const review = draft === null ? null : await agent(`审阅 ${W}/drafts/${D}-initial-coverage.md（支线材料在 ${R}/）。这是买方初次覆盖，重点看：Q1/Q2/Q5/Q7/Q8/Q12 是否有判断；“预期差”是否只是复述共识；壁垒“形成还是被破坏”是否有证据；估值分解是否算对。`, { agentType: 'dalton-reviewer', label: '审稿' })
const fix = review === null ? null : await agent(`按审稿意见修订 ${W}/drafts/${D}-initial-coverage.md，P0 全改，不新增未核实数字；同步修订 ${R}/knowledge-proposal.json。审稿意见：\n${review}`, { agentType: 'general-purpose', label: '修订', schema: OUT })
return { ok: draft !== null, draft: `${W}/drafts/${D}-initial-coverage.md`, knowledge_proposal: kp && `${R}/knowledge-proposal.json`, missing, review_done: review !== null, fix_summary: fix && fix.summary }
```

**验证方式（真实运行）** 在 PS.18 指定的 P2 样本公司工作区 `/initial-coverage {TICKER}`。记录运行时长、花费、9 次派发的状态。启动 skill 结束后，检查知识层文件确实写入、`verdicts.md` 有一条“新论点”（`NEW_THESIS`）、`agenda.md` 有下一次唤醒（自动任务文件存在）。

**验收标准** PS.18 的 P2-1 条目：PM 盲评“可用”；12 问重点题全部有判断；估值分解表数学正确（两项贡献之和等于股价对数变化）；知识层初始化完整且与报告一致。

**风险与注意**
- 四条支线同时检索内部库，注意 AlphaEngine 等数据源的额度；数据接口返回额度错误时，支线要写“额度用尽，未覆盖”，不能写“无相关研究”。
- 历史一致预期数据可能缺失（见 D 支线第 1 条），要在报告里写明近似方法。
- 知识层写入由启动 skill 的主智能体完成：子智能体没有 `knowledge_*` 工具（PK.4）；子智能体若直接写了 `data/`，PK.5 会在本轮后记成 `direct_edit` 版本并给警告，主智能体要读警告、按 ops 重新提交。

**预估工时** 3.5 天。

---

## PS.6 workflow `full-coverage`（P3 完整覆盖）

**目标**
PM 安排完整覆盖后，一次运行产出：12 问深度研究 memo（按基金 11 部分结构）+ 带公式的 Excel 模型（三表、DCF、comps）+ 独立审稿意见，修订后由分析师发起 `publish_report`，经数字核验、审稿、PM 审批后正式发布。同时修正初次覆盖的早期认知（thesis、debate、drivers、management 都要更新）。

**前置依赖** PS.1、PS.2、PS.5（同一公司已有初次覆盖和知识层）；PK 的 `publish_report`；自带 `xlsx`、`3-statements`、`dcf-model`、`comps-analysis`、`check-model` skill（已存在于 `plugins/langalpha_research/skills/` 与 `plugins/langalpha_deliverables/skills/xlsx/`）。

**来源（只借原理）** `~/.openclaw/agents/main/agent/workshop-skills/company-deepdive-mx-0911/`：12 问（`SKILL.md`）、问题与支线/章节映射（`scripts/research_structure.py`）、数字底表（`<slug>_facts.md` 的 FACTS-LEDGER）、问题清单 frame、四条支线规则（`references/evidence_{a,b,c,d}_*.md`、`common_rules.md`）、写作指南（`references/writing_guide.md`）、机械检查（`validate`）、干净上下文审稿（`references/review_task.md`）。LangAlpha 的 `initiating-coverage`：保留它的工作簿清单和建模任务（Task 2、Task 3），研究正文换成 12 问 memo（蓝图 §4.5）。

**新增/改动文件**（新建）
- `workflows/full-coverage/workflow.js`
- `plugins/dalton/skills/dalton-full-coverage/SKILL.md`（启动 skill）
- `plugins/dalton/skills/dalton-full-coverage/references/facts-ledger.md`（数字底表规则与必填键）
- `plugins/dalton/skills/dalton-full-coverage/references/frame.md`（问题清单 frame 规则）
- `plugins/dalton/skills/dalton-full-coverage/references/branch-a-industry.md`、`branch-b-sellside-expert.md`、`branch-c-primary.md`、`branch-d-offstreet.md`、`branch-common.md`
- `plugins/dalton/skills/dalton-full-coverage/references/memo-writing.md`（写作指南 + 11 部分结构 + 12 问放置规则）
- `plugins/dalton/skills/dalton-full-coverage/references/model-spec.md`（工作簿清单与 Dalton 增补）
- `plugins/dalton/skills/dalton-full-coverage/scripts/research_structure.py`（移植：QUESTIONS 映射表、frame 校验、memo 结构校验）
- `plugins/dalton/skills/dalton-full-coverage/scripts/facts.py`（底表 upsert / 读取：`facts.py set --key --label --value --period --source`、`facts.py get`）
- `plugins/dalton/skills/dalton-full-coverage/scripts/validate_memo.py`（移植 `validate`，去掉 `$`/`~` 规则）
- `plugins/dalton/skills/dalton-full-coverage/scripts/test_research_structure.py`（移植原 unittest 的 5 个回归）
- `plugins/dalton/skills/dalton-full-coverage/templates/memo.md`、`templates/questions.md`

**输入参数** `{ticker, company, market, workspace_dir, as_of, profile: "tob"|"toc", form: "10-K"|"20-F", pm_brief?: string, focus_questions?: [...]}`。

**流程**
```
阶段 0  一手材料与数字底表（1 个子智能体）
        最近 4 份定期报告 + 最近 4 次电话会 + 招股书（如有）+ 内部人交易；
        财务镜头（复用 PS.2）；盈利质量（Sloan 比率、CFO/NI、现金转换周期）
        → research/<D>-fc/facts.md（FACTS-LEDGER）
阶段 0.8 问题清单 frame（1 个子智能体）→ research/<D>-fc/questions.md，必须通过 research_structure.py 校验
阶段 1  四条取证支线（parallel，4 个 dalton-evidence）
        A 行业与竞争 → ea.md；B 卖方+内部+专家 → eb.md；C 一手精读（含 3 年指引可信度）→ ec.md；D 场外信号 → ed.md
阶段 2  建模（2 个子智能体，与 memo 第一部分并行）
        模型：3-statements + DCF → model/<T>_model_v1_<D>.xlsx；comps → 同一工作簿的 Comparable Companies 页
阶段 3  memo 三段式写作（3 个子智能体顺序执行）+ validate_memo.py 到 0 FAIL
阶段 4  check-model 审模型（1）∥ dalton-reviewer 审 memo（1）
阶段 5  修订（1）→ 知识层修订草案（1）
阶段 6  （启动 skill，主智能体）展示给 PM → 写知识层 → publish_report
```
派发数约 15。

**数字底表规则**（`facts-ledger.md`）
- 底表放在 `facts.md` 的 `<!-- FACTS-LEDGER {"ledger":[{key,label,value,period,source}]} -->` 块里，并渲染成表。
- 必填键：`price`、`market_cap_usd`、`shares_diluted`、`net_cash_basic`、`net_cash_strict`、`hidden_asset_net`、`return_r1`、`return_r2`、`return_r3`、`return_score`、`qoe_sloan`、`qoe_cfo_ni`、`qoe_ccc`、最近 8 个季度的营收/毛利/营业利润/EPS、最近 3 个财年的 FCF 与 SBC。
- 硬规则：之后所有阶段**引用目标公司数字只能用底表**，缺了写“底表缺 X”并回到阶段 0 补（用 `facts.py set`）；唯一例外是同业数字，由 A 支线现取并写（期间，来源）。
- 底表就是发布时数字核验的对账基准之一：`facts.md` 作为 `publish_report` 的附件传入（`role: "facts"`），PK.9 的数字核验读它的 FACTS-LEDGER 块，再加 P0-D.3b 的逐文档来源记录。

**问题清单 frame**（`frame.md`，移植 `<slug>_questions.md`）
头部三项：已读一手材料、关键驱动、关键矛盾。每个问题五项：公司问题（把通用 12 问落成这家公司的具体问题）、关键假设、取证安排（哪条支线、查什么）、论证章节、证据进展。不能留“待填写”。PM 的 `pm_brief` 和 `focus_questions` 在这里落地。

**四条支线规则要点**（详见各 references 文件，移植原规则并去掉 OpenClaw 运行时）
- **通用**（`branch-common.md`）：引用编号 `[A-1]`…`[D-n]` 放句末带来源身份；每条发现标注对应 Q 号；文件末尾逐题小结（证据、最强反证、未决项、主笔需补什么）+ `<!-- 完成 -->`；来源优先级 filing > 交易所/监管披露 > 署名研报 > 官方统计 > 一般新闻 > 社区；百度百科不用。
- **A 行业与竞争**：A1 行业定义与 TAM（≥2 种方法）；A2 竞争（5–8 家，份额与 3 年趋势，现取财务；议价能力；壁垒类型＋可证伪指标）；A3 周期（2–3 个高频指标）；A4 1–3 个全球对标；A5 政策；A6 行业专用框架；A7 缺口；A8 引用。
- **B 卖方、内部与专家**：B1 AlphaEngine（先 `list_filters`；一次一个类别；`sort=relevance`；结论必须 `get_document` 读全文）；B2 卖方一致预期地图（两年中位数、2–4 个分歧点、12 个月评级与目标价路径、最近一次集体修正的触发事件）；B3 内部记忆（`researchdata.research(route="all")` + `company-wiki` 路由）；B4 Guidepoint（英文检索；客户流失、赢单输单、渠道、前高管、定价；引用 ≤20 个英文词并带出处）；B5 卖方盲点 2–4 条；B6 缺口；B7 引用。
- **C 一手文件精读**：C1 最近 4 份定期报告（MD&A 措辞漂移、风险因素逐条 diff、分部变化、收入确认、关联交易、“经常性的非经常性” non-GAAP 调整）；C2 最近 4 次电话会（指引措辞并排、分析师高频问题计数、管理层回避的问题、2–4 个话题的措辞漂移；只用公司电话会原文，取不到时依次退到 IR 网播 → 8-K/6-K Ex-99 → 写“电话会未覆盖”）；C3 年报与招股书（招股书承诺 vs 今天）；**C4 三年指引可信度**：12 个季度的“指引 vs 实际”逐季表（指引中点、实际、偏差、超/低于、管理层事后解释），归纳指引风格（保守/中性/激进；收入还是利润更保守；全年指引的修正节奏）；C5 8-K 事件、内部人交易（区分 10b5-1）、13D/13G；管理层引语保留原文；yfinance 不作一手来源。
- **D 场外信号**：D1 X（检索不到不证明不存在）；D2 雪球（P6 接口，本期 web 检索）；D3 员工评价；D4 社区与新闻聚合；D5 招聘信号；D6 13F（只按机构查）；D7 toC 公司做产品体验表（质量/价格/服务/物流/售后 × 正面词 × 负面词 × 平台数 × 趋势；≥2 个平台确认才算确认），toB 公司做从业者信号表（采用、替换、招投标、集成商口碑、GitHub 活跃度）。

**问题与支线、章节映射**（`research_structure.py` 的 QUESTIONS，原样移植）

| Q | 支线 | 论证章节 |
|---|---|---|
| Q1 | A、C | 一、二 |
| Q2 | A、C、D | 一、三、八 |
| Q3 | B、C | 四、八、九 |
| Q4 | B、C、D | 一、二、七 |
| Q5 | B、C | 八 |
| Q6 | A、D | 十 |
| Q7 | B、D | 五、八 |
| Q8 | 主笔综合 A/B/C/D | 二、四、六、九 |
| Q9 | B、C | 九、十 |
| Q10 | 主笔综合 B/C | 六、九、十 |
| Q11 | 主笔综合 A/D | 五、六 |
| Q12 | 主笔综合 A/B/C/D | 五、十 |

每题回答标准：直接判断 → 关键证据与因果分析 → 投资含义 → 必要的反方与改判条件。重点题 Q1–Q5、Q7、Q8、Q12。分析框架：Good Business（赚钱容易、赚钱多、赚钱久：ROIC vs WACC、FCF 率、护城河）/ Good People（文化、能力、利益是否一致）/ Good Price（现价要求公司做到什么）。分析链：驱动树 → 关键变量与行业框架 → 市场隐含假设 → 差异化观点 → 三情景估值与回报 → 催化剂、跟踪与证伪。差异化观点三问：“市场认为 ___”“但实际上 ___，因为 ___”“错误定价为什么还存在（惯性 / 抱团 / 旧叙事拖累 / 近因偏差 / 覆盖不足）”。

**memo 输出模板**（`templates/memo.md`，标题逐字，`validate_memo.py` 按此校验）
```
# {公司名}（{TICKER}）投资备忘录
## 基本信息        代码 / 日期 / 股价 / 市值 / 30 日均成交额 / 当前建议
## 核心判断        一句话差异化观点 → Thesis 摘要 → 主要风险 → 风险收益 → 建议（≤3 句）
## 零、12 个核心问题与投资判断
### Q1. 这家公司靠什么赚钱？
（回答）
来源：一手 / 卖方 / 专家 / 场外 / 底表 —— [C-3][B-7]
详见：第一章、第三章
……（Q1–Q12，缺口写“来源：缺口 —— 需要什么、已尝试的路径”）
## 一、公司与业务  1.1 沿革 1.2 产品与分部 1.3 Revenue Driver Tree 1.4 财务（利润表 8 季 / 资产负债表 / 现金流）1.5 盈利质量记分卡 1.6 股东回报
## 二、业务质量    Good Business / Good People / Good Price / 综合评级
## 三、行业与竞争  TAM / 格局 / 周期 / 全球对标 / 行业框架
## 四、核心投资逻辑 4.1 差异化观点三问 4.2 市场隐含预期（PIE）4.3 Thesis 逐条 4.4 与组合的关系
## 五、风险与反方证据 5.1 Risk Matrix 5.2 Pre-mortem（若一年后亏 30%，最可能怎样发生，3–5 条按可能性排序）5.3 我们自己的隐含假设
## 六、风险收益    多/中/空三情景 / 敏感度 / 预期回报与流动性
## 七、管理层与股权 履历 / 3 年指引可信度（C4 表）/ 持股变化 / 期权池 / 内部人交易
## 八、市场预期    历史估值与事件 / 卖方预期 / 我们与卖方逐驱动对比
## 九、估值        三角验证（DCF、可比、历史倍数）/ 可比表 / 催化剂日历 / IRR
## 十、后续跟踪    高频指标 / 关键日期 / 监控清单（指标｜阈值｜来源｜窗口｜越线怎么办）
## 附录 财务表 / 盈利质量明细 / 产品体验或从业者信号表 / 管理层访谈问题清单
## 引用清单
```
三段式写作：第一段（基本信息、核心判断、零、一、二）→ 第二段（三至六，需要模型的情景结果）→ 第三段（七至十、附录、引用清单）。每段写完跑 `validate_memo.py --mode full`，0 FAIL 才进入下一段。行数下限（原 700 行）**不作为门槛**（蓝图 §3.2 去掉机械门槛），只保留结构、引用闭环、12 问位置、Thesis 和 Risk Matrix 行的引用率（>30% 行无引用为 FAIL）等实质检查。

**Excel 模型规格**（`model-spec.md`）
- 工作簿清单沿用自带 `initiating-coverage` 的顺序：`Cover`、`Revenue Model`、`Income Statement`、`Cash Flow Statement`、`Balance Sheet`、`Scenarios`、`DCF Inputs`、`DCF`、`Sensitivity Analysis`、`Comparable Companies`、`Valuation Summary`、`Checks`。Dalton 增补三页：`Facts`（数字底表镜像，所有历史输入从这里引用）、`Guidance vs Actual`（C4 的 12 季表）、`Street vs House`（逐驱动对比：一致预期｜house case｜差异｜依据）。
- 建法：严格按 `.agents/skills/3-statements/SKILL.md`、`.agents/skills/dcf-model/SKILL.md`、`.agents/skills/comps-analysis/SKILL.md` 和 `.agents/skills/xlsx/SKILL.md`：公式不硬编码、蓝色输入带出处批注（`Source: 10-K FY2025, p.42`）、`<task>/build_model.py` 保存可重跑、`recalc.py` 后 `audit.py --strict`、Checks 页全部 OK。
- house case 与一致预期分开：`Scenarios` 页至少 Bear/Base/Bull 三套驱动；`Street vs House` 页显示 house 与一致预期在每个驱动上的差异，这就是 memo 第八章“我们与卖方逐驱动对比”的出处。
- DCF 净债务用严格净现金（来自底表），基础口径作为敏感度。
- 文件命名 `model/<TICKER>-model-v01.xlsx`，此后每次更新由 `model-update` 另存 v2、v3，不覆盖。

**workflow 脚本骨架**
```js
export const meta = { name: 'full-coverage', description: 'Dalton 完整覆盖：一手材料与数字底表 → frame → A∥B∥C∥D 取证 → 建模∥memo → check-model∥独立审稿 → 修订 → 知识层修订草案' }
const { ticker: T, company: C, workspace_dir: W, as_of: D } = args
const R = `${W}/research/${D}-fc`, S = '.agents/skills/dalton-full-coverage'
const base = `先读 .agents/skills/dalton-conventions/SKILL.md 与 ${S}/SKILL.md；公司 ${C}（${T}），截至 ${D}；数字底表 ${R}/facts.md（只引用底表，缺了写“底表缺 X”）。`
const OUT = { type: 'object', properties: { files: { type: 'array', items: { type: 'string' } }, summary: { type: 'string' }, gaps: { type: 'array', items: { type: 'string' } } }, required: ['files', 'summary'] }

phase('一手材料与数字底表')
const facts = await agent(`${base} 按 ${S}/references/facts-ledger.md 建 ${R}/facts.md：读最近 4 份定期报告、4 次电话会、内部人交易；按 dalton-financial-lenses 做三把镜头；算盈利质量；所有必填键用 scripts/facts.py set 写入。`, { agentType: 'dalton-evidence', label: '数字底表', schema: OUT })
if (facts === null) return { ok: false, reason: '数字底表失败，后续阶段不启动' }

phase('问题清单')
const frame = await agent(`${base} 按 ${S}/references/frame.md 写 ${R}/questions.md。PM 要求：${args.pm_brief || '无'}；重点问题：${(args.focus_questions || []).join('；') || '默认'}。写完跑 python ${S}/scripts/research_structure.py validate-agenda ${R}/questions.md 到通过。初次覆盖稿与知识层在 ${W}/drafts/ 与 ${W}/ 根目录。`, { agentType: 'general-purpose', label: 'frame', schema: OUT })

phase('取证')
const branches = [['a', 'branch-a-industry'], ['b', 'branch-b-sellside-expert'], ['c', 'branch-c-primary'], ['d', 'branch-d-offstreet']]
const ev = await parallel(branches.map(([k, f]) => () => agent(`${base} 问题清单 ${R}/questions.md。按 ${S}/references/branch-common.md 与 ${S}/references/${f}.md 取证，写 ${R}/e${k}.md，文件末尾 <!-- 完成 -->。profile=${args.profile}。`, { agentType: 'dalton-evidence', label: `支线 ${k.toUpperCase()}`, schema: OUT })))
const evMissing = branches.filter((_, i) => ev[i] === null).map(([k]) => k.toUpperCase())

phase('建模与 memo 第一段')
const [model, memo1] = await parallel([
  () => agent(`${base} 按 ${S}/references/model-spec.md 建模型 ${W}/model/${T}-model-v01.xlsx（三表 + DCF + comps + Facts/Guidance vs Actual/Street vs House 三页），支线材料 ${R}/e*.md。recalc 与 audit --strict 必须通过，Checks 全 OK。返回 Scenarios 三情景的每股价值与关键驱动。`, { agentType: 'general-purpose', label: '建模', schema: OUT }),
  () => agent(`${base} 按 ${S}/references/memo-writing.md 写 memo 第一段（基本信息、核心判断、零、一、二）到 ${W}/drafts/${D}-memo.md，材料 ${R}/。缺失支线：${evMissing.join(',') || '无'}。跑 validate_memo.py --part 1 到 0 FAIL。`, { agentType: 'general-purpose', label: 'memo 第一段', schema: OUT }),
])
const memo2 = memo1 === null ? null : await agent(`${base} 续写 ${W}/drafts/${D}-memo.md 第二段（三至六），第六章情景与敏感度引用模型 ${W}/model/${T}-model-v01.xlsx${model === null ? '（模型失败：第六章写缺口）' : ''}。validate --part 2。`, { agentType: 'general-purpose', label: 'memo 第二段', schema: OUT })
const memo3 = memo2 === null ? null : await agent(`${base} 续写第三段（七至十、附录、引用清单），第七章用 C4 指引可信度表，第九章估值与模型一致。跑 validate_memo.py --mode full 到 0 FAIL。`, { agentType: 'general-purpose', label: 'memo 第三段', schema: OUT })

phase('审模型与审稿')
const [mcheck, review] = await parallel([
  () => model === null ? null : agent(`按 .agents/skills/check-model/SKILL.md 审 ${W}/model/${T}-model-v01.xlsx，只出报告不改模型，报告写 ${R}/model-check.md。`, { agentType: 'general-purpose', label: 'check-model', schema: OUT }),
  () => memo3 === null ? null : agent(`审阅 ${W}/drafts/${D}-memo.md，支线 ${R}/e*.md，底表 ${R}/facts.md，模型 ${W}/model/${T}-model-v01.xlsx（只读）。先只凭核心判断和零章复述投资论点，再逐题检查，重点 Q1–Q5、Q7、Q8、Q12。`, { agentType: 'dalton-reviewer', label: '独立审稿' }),
])

phase('修订')
const fix = review === null ? null : await agent(`${base} 按审稿意见与 ${R}/model-check.md 修订 memo 和模型（模型改动走 .agents/skills/model-update/SKILL.md，另存 v02，并用 knowledge_update(doc="models") 登记，不覆盖）。P0 全改。审稿意见：\n${review}`, { agentType: 'general-purpose', label: '修订', schema: OUT })
const kp = fix === null ? null : await agent(`${base} 对比 ${W}/ 根目录现有知识层与修订后的 memo，按 PK.2 的 JSON Schema 写知识层修订草案 ${R}/knowledge-revision.json（{doc: {ops, reason, evidence_refs}}）：每处改动给旧值、新值、原因、证据（引用编号）。管理层指引风格写入 management 部分。`, { agentType: 'general-purpose', label: '知识层修订草案', schema: OUT })
return { ok: memo3 !== null, memo: `${W}/drafts/${D}-memo.md`, model: model && `${W}/model/${T}-model-v01.xlsx`, review_done: review !== null, model_check: mcheck && `${R}/model-check.md`, knowledge_revision: kp && `${R}/knowledge-revision.json`, evidence_missing: evMissing }
```

**启动 skill `dalton-full-coverage` 的收尾（主智能体）**
1. 读 workflow 结果；若审稿结论是“不得发布”或仍有 P0，停在草稿，向 PM 汇报原因和计划。
2. 把 `knowledge-revision.json` 的改动逐条展示给 PM（旧 → 新 → 原因），确认后按 doc 调 `knowledge_update`：`thesis`（支柱、阈值、证据行、`house_view`；若给目标价，`house_view.target` 必须带 `model_ref.kind="model_cell"`，这是 PK 保留的硬约束）、`debate`、`drivers`、`management`（C4 的 `guidance_history` 与 `guidance_style`）、`lessons`（推翻初次覆盖某个判断时记一条：适用条件、反例）、`models`（登记 `model/<TICKER>-model-v01.xlsx`）、`company`（`coverage_stage=full`）。然后 `knowledge_append_verdict`（通常 `THESIS_STRENGTHENED` / `THESIS_WEAKENED` / `NEW_THESIS`，对照初次覆盖的 thesis），`agenda_ops` 写完整覆盖后的跟踪议程。
3. 先 `check_draft(drafts/<D>-memo.md)` 看数字核验反馈，修到没有未匹配数字；再调 `publish_report(draft_path="drafts/<D>-memo.md", title=..., kind="full_coverage", slug=..., attachments=[{"path":"model/<TICKER>-model-v01.xlsx","role":"model"}], updates_thesis=True)`。`attachments` 里同时带 `{"path":"research/<D>-fc/facts.md","role":"facts"}`，PK.9 的核验以它为对账基准。核验退回或服务端审稿 `revise` 时逐条修，再调用；PM 在审批卡片上批（无人值守时停在 `awaiting_pm`，等 PM 打开线程）。
4. 批准后 `thesis.official` 由发布流程写入（`updates_thesis=True`），`published/` 生成不可变副本。

**知识层写入** 见收尾第 2 步。

**发布** memo + 模型走 `publish_report(kind="full_coverage", updates_thesis=True)`，这是 P3 的完成标准（蓝图 §5）。支线文件、底表、草稿期审稿意见留在 `research/`，不进发布包。

**与 LangAlpha 自带的关系** 基于 `initiating-coverage` 另起一版（fork）：保留 Task 2/Task 3 的建模与估值方法、工作簿清单、`xlsx` 质量契约；替换 Task 1（研究文档）和 Task 5（30–50 页报告）为 12 问 memo；去掉“一次一任务、每步停下”的执行模型，改为 workflow 一次跑完、子智能体并行取证；增加数字底表、问题清单、四条支线、不同家族审稿和发布治理。原因：基金要的是买方深度 memo 和可追溯的数字，不是卖方式首次覆盖报告。

**验证方式（真实运行）** 对 PS.5 做过初次覆盖的同一家公司，`/full-coverage {TICKER}`。检查：约 15 次派发的状态；`facts.md` 必填键齐全；`research_structure.py` 与 `validate_memo.py` 通过；模型 `recalc.py`、`audit.py --strict` 通过且 Checks 全 OK；草稿期审稿已执行；`check_draft` 无未匹配数字；`publish_report` 的服务端审稿为 `pass` 并跑通到 PM 审批卡片。

**验收标准** PS.18 的 P3-1、P3-2 条目：12 问 memo 加带公式的 Excel 模型，经审稿和 PM 审批正式发布；发布核验“无来源数字”为 0；PM 盲评“可用”或以上。

**风险与注意**
- 这是最贵的 workflow。上线前先在一家公司上测花费（`get_run_charge_usd`），写入试跑记录，由 owner 决定是否设 task cap（P0.8；日额度金额由 owner 定）。
- 三段 memo 由三个不同子智能体顺序写，风格可能不一致；第三段子智能体的提示词要求先通读前两段。若一致性仍差，改为一个子智能体写完全文（单个子智能体 30 分钟上限内能否写完【待验证】）。
- 模型和 memo 的数字必须一致：memo 第六、九章的数字只能从模型读，不能另算。
- 修订阶段改模型必须另存版本，保持 v1 可回溯。

**预估工时** 6 天。

---

## PS.7 财报日历维护：`dalton-earnings-calendar` + 每周 cron

**目标**
每周一次，对覆盖名单逐家确认下一次财报的**官方**日期，维护 pending / confirmed / reported 三态台账；日期确认后，为该公司创建（或改期）三条 `once` 自动任务：T-3 交易日的前瞻（`tm3`）、T0+N 小时的快讯（`t0`）、T+1 交易日的深度链（`tp1`），并同步到该公司议程的 `watch.events`。这是财报链（PS.8、PS.9）的入口，也是 PK.19 研究总监“每周六刷新财报日期”所调用的方法。日期只认公司官方来源；FMP、yfinance 和其他聚合日历只作候选，标“待确认”（`date_status: estimated`），在官方公布前不建自动任务（与 PK.19 一致）。

**前置依赖** PS.0（自动任务预加载结论）；P0D P1.2 的覆盖名单 `Home/dalton/coverage.yaml`（tier、market、`earnings.group`、`earnings.ir_url`、`workspace.status`；财年说明写在 `note`）；PK.15 命名约定与 REST helper `PUT /api/v1/dalton/automations/{name}`；PK.19 的议程工具（`dalton_agenda_note`）。本 skill 在 Home 运行（研究总监）。

**来源（只借原理）** `~/.openclaw/agents/main/agent/workshop-skills/richard-earnings-tracker/`（SKILL.md、`references/calendar-reconciliation.md`、CLI 的 check/mark 语义）。剥掉：Richard 的日历账号、Google Calendar 写入（`gws`）、105 家写死名单、Discord 通告、7200 秒超时。日历事件换成 LangAlpha 自动任务。

**新增/改动文件**（新建）
- `plugins/dalton/skills/dalton-earnings-calendar/SKILL.md`
- `plugins/dalton/skills/dalton-earnings-calendar/references/classification.md`（日期分类与来源规则）
- `plugins/dalton/skills/dalton-earnings-calendar/references/reconciliation.md`（对账规则）
- `plugins/dalton/skills/dalton-earnings-calendar/scripts/calendar_state.py`（台账读写：`list`、`check --ticker --date`、`mark`、`roll`、`diff --scan <jsonl>`，原子写入）
- `plugins/dalton/skills/dalton-earnings-calendar/scripts/trading_days.py`（`--date --minus 3 --exchange XNYS`，返回 T-3 交易日；优先用沙箱里的交易日历库，没有则用内置节假日表，【待验证沙箱可用库】；`XHKG`、`XSHG` 在 P6 加）
- `docs/dalton/dev-guide/automation-templates/dalton-earnings-calendar.json`
- `docs/dalton/dev-guide/automation-templates/dalton-ticker-earn-period-tm3.json`、`-t0.json`、`-tp1.json`（三个模板）
- 运行时状态文件（不进仓库）：`Home/dalton/earnings-calendar.json`、扫描文件 `Home/dalton/research/earnings-scan-<YYYY-MM-DD>.jsonl`

**台账格式**（`earnings-calendar.json`）
```json
{
  "updated": "2026-10-18",
  "entries": {
    "META": {
      "name": "Meta Platforms", "tier": "Core", "workspace_id": "<uuid|null>",
      "fiscal_note": "FY=CY",
      "status": "confirmed",
      "date_status": "confirmed",
      "quarter": "FY26Q3",
      "date": "2026-10-28", "timing": "AMC", "release_time_et": "16:05",
      "call_time_et": "16:30",
      "confidence": "high", "source_url": "<IR 新闻稿链接>", "source_type": "company_ir",
      "automations": { "tm3": "dalton-meta-earn-fy26q3-tm3.json", "t0": "dalton-meta-earn-fy26q3-t0.json", "tp1": "dalton-meta-earn-fy26q3-tp1.json" },
      "checked_at": "2026-10-18T07:12:00-04:00",
      "history": [ { "at": "...", "from": {"date": "..."}, "to": {"date": "..."}, "reason": "公司改期公告" } ]
    }
  },
  "reported": [ { "ticker": "...", "quarter": "...", "date": "...", "timing": "...", "automations": {...} } ]
}
```
三态：`pending`（下一期未官宣；可带 `tentative_date` 和 `tentative_sources`，`date_status: "estimated"`，界面和总结里显示“待确认”，不建自动任务）、`confirmed`（官方日期已确认，自动任务已建）、`reported`（发布日已过，整条移入 `reported` 数组，并以下一期重新登记为 `pending`）。

**分类规则**（`classification.md`，移植并收紧为“只认官方”）
- `confirmed`（`date_status: confirmed`）：公司官方来源明确写出未来发布日期——公司 IR 新闻稿或 IR events 页、SEC 8-K/6-K（交易所公告类来源在 P6 接港股/A 股时加）。
- 只有 FMP、yfinance 或聚合站（Nasdaq、Zacks、Earnings Whispers、WSJ、MarketWatch、`get_earnings_calendar`）给出日期：记 `pending` + `tentative_date`，`date_status: estimated`，标“待确认”，**不建自动任务**；若 tentative 日期在 10 天内，建一条 T-5 的 `once` 复查任务（只复查日期，不做研究）。
- `already_reported`：本期已发布，下期未披露。
- 其他：`not_announced`。不确定就是 `not_announced`，不推测日期。
- timing：BMO / AMC / unknown。季度经营数据不是业绩。财年特殊的公司（6 月、9 月、3 月、1 月、10 月财年）在名单的 `fiscal_note` 里写明，季度标签按公司财季。

**流程（SKILL.md）**
1. 预检：读覆盖名单和台账，核对唯一代码数；只读台账不算扫描。
2. 滚动：`calendar_state.py roll --today`：发布日（不是电话会日）已过的 `confirmed` 整条移入 `reported`，并以下一期登记 `pending`。
3. 扫描：逐家（先 pending 后 confirmed，Core 优先），每家用两条检索词：`"{公司}" "to report" "{季度}" results`、`"{公司}" earnings date announcement`，限 180 天内；然后读 IR 页或 8-K 确认（只有官方来源能把日期变成 confirmed）。每家扫描结果用 `json.dumps` 追加一行到 `Home/dalton/research/earnings-scan-<日期>.jsonl`，字段固定：`ticker,name,status,earnings_date,timing,quarter_label,confidence,source_url,evidence,notes`。工具失败写 `status:"scan_failed"`，不能写成 `not_announced`。
   - 名单大于 30 家时，用 workflow 内联脚本按 tier 分 4 组 `parallel` 扫描（每组一个 `dalton-evidence`，每组写自己的 jsonl），主智能体合并。
4. 合并：逐行 `json.loads`，坏行能读出代码和日期的重查，否则记 `not_announced`，所有坏行进总结。
5. 逐家处理（不批量）：`check` → 已记录同日期则跳过；日期变了，且三条 once 都还没跑，则**原地改**它们的 `next_run_at`（不是删了重建），并在 `history` 记一条；已经跑过的 once 不能再改（`file.py`），只能为剩下的窗口写新文件；新确认则按模板创建三条自动任务并 `mark`。
   - 前瞻：`next_run_at = trading_days(date, minus=3)` 当天 08:00（公司所在市场时区）。
   - 快讯：AMC：`release_time + N`（N 默认 2.5 小时，美东 18:35 左右）；BMO：`release_time + N`（N 默认 2 小时）；时间未知：发布日当地 20:00。N 写在覆盖名单配置里，可按公司调整。
   - 深度链：T+1 交易日 20:00（市场时区）。
   - 只为 `workspace.status=active` 的公司建链；没有工作区的 Core 公司列入总结“缺工作区”。Watch 公司默认只建快讯（`coverage.yaml` 的 tier 含义：Watch“财报只做快讯”；配置项 `chain_by_tier: {"core": ["tm3","t0","tp1"], "watch": ["t0"]}`）。
6. 对账（必须跑，不过不算完成）：`calendar_state.py diff` 比对扫描文件与台账，得到 `missing`、`date_mismatch`；再逐个读回三条自动任务文件的 `state.next_run_at`（文件挂载可读），检查状态为 active、时间与台账一致。时区：带偏移的时间先换成市场时区再比日期，不能截取字符串前 10 位；无偏移看 `timezone` 字段，两者都没有就标“待人工核对”。只读回不了时只能说“本地已记录”，不能说“对账通过”。
7. 总结（顺序固定）：新确认（公司 / 日期 / BMO·AMC / 来源）→ **未落库**（对账没过的公司 + 原因；没有写“无”）→ **解析失败行**（没有写“无”）→ 改期 → 本周已发布、仍待官宣数量、缺工作区 → 扫描失败的公司。第二、三项永远出现；两项都为“无”时可以只回一行状态。

**自动任务模板**（文件名遵守 PK.15；ticker 小写、`.` 换 `-`）

日历维护本身（Home）：单独一条 `dalton-earnings-calendar.json`，每周六 09:00（Asia/Shanghai，即美东周五晚间收盘后），覆盖周一到周五的官宣（OpenClaw 原来从周一改到周六，就是因为周一跑会漏掉上周后半周的官宣）。PK.19 的研究总监用同一个时间和同一份规则：
```json
{
  "name": "财报日历每周维护",
  "status": "active",
  "trigger_type": "cron",
  "cron_expression": "0 9 * * 6",
  "timezone": "Asia/Shanghai",
  "instruction": "/dalton-earnings-calendar 全名单 —— 按 skill 流程滚动、扫描（只认官方来源，候选日期标待确认）、经 PUT /api/v1/dalton/automations/{name} 建/改 tm3/t0/tp1 自动任务、用 dalton_agenda_note 同步各公司 watch.events、对账，最后按固定顺序总结。",
  "workspace_id": "home",
  "thread": "new"
}
```
每家公司三条 once（属于**公司工作区**，`workspace_id` 指向该公司）。Home 的研究总监不跨工作区写文件，而是调后端 REST helper `PUT /api/v1/dalton/automations/{name}`（PK.15，`src/dalton/app/router.py`），请求体就是下面的文件内容；改期也调同一接口（同名覆盖 `next_run_at`）。分析师在自己的工作区里补建时，可以直接写文件（常设授权）：

`dalton-meta-earn-fy26q3-tm3.json`（T-3 交易日 08:00，市场时区）：
```json
{
  "name": "META FY26Q3 财报前瞻（T-3）",
  "status": "active",
  "trigger_type": "once",
  "next_run_at": "2026-10-23T08:00:00-04:00",
  "timezone": "America/New_York",
  "instruction": "/dalton-earnings-preview META FY26Q3 发布日 2026-10-28 AMC（来源：<IR 链接>）。先读 .agents/skills/dalton-earnings-preview/SKILL.md；收尾按 dalton-agenda 三步。",
  "workspace_id": "<META 工作区 id>",
  "thread": "new"
}
```
`dalton-meta-earn-fy26q3-t0.json`（发布 + N 小时）：
```json
{
  "name": "META FY26Q3 财报快讯（T0）",
  "status": "active",
  "trigger_type": "once",
  "next_run_at": "2026-10-28T18:35:00-04:00",
  "timezone": "America/New_York",
  "instruction": "/dalton-earnings-flash META FY26Q3 发布时间 2026-10-28 16:05 ET。先读 .agents/skills/dalton-earnings-flash/SKILL.md。前瞻稿与 predictions.json 在 drafts/、research/ 下最新的 preview-FY26Q3。",
  "workspace_id": "<META 工作区 id>",
  "thread": "new",
  "delivery": ["<PM 的通知渠道，按工作区配置>"]
}
```
`dalton-meta-earn-fy26q3-tp1.json`（T+1 交易日 20:00，卖方报告基本到齐后）：
```json
{
  "name": "META FY26Q3 财报深度链（T+1）",
  "status": "active",
  "trigger_type": "once",
  "next_run_at": "2026-10-29T20:00:00-04:00",
  "timezone": "America/New_York",
  "instruction": "/dalton-earnings-deep META FY26Q3 发布日 2026-10-28。跑 earnings-deep-update workflow（模型：data/models.json 登记的当前版本；前瞻预测：research/ 下 preview-FY26Q3/predictions.json），结束后按 knowledge-revision.json 写知识层。",
  "workspace_id": "<META 工作区 id>",
  "thread": "new",
  "delivery": ["<PM 的通知渠道>"]
}
```
期间标签 `<period>` 用公司财季（`fy26q3`），与 `<dalton_wake>` 的 `FY26Q3:t0` 引用一致。若 PS.0 结论是斜杠命令不能从 instruction 识别，以上改走 REST 带 `additional_context`。示例日期仅为格式示意，真实日期以官方公告为准。PK 的 earnings 探针也会按 `agenda.watch.events` 在 T-3、T0、T+1 窗口发信号；once 文件和 watch 信号重复时，PK.16 的去重规则只放行一次。

**知识层写入** 研究总监用 `dalton_agenda_note` / PK.19 的议程工具把每家的财报事件写进该公司 `agenda.watch.events`：已确认的写日期、timing、`date_status=confirmed`、官方来源、三条自动任务文件名；只有候选日期的写 `date_status=estimated`（显示“待确认”），不带自动任务。改期时改同一事件并留原因。台账 `Home/dalton/earnings-calendar.json` 是 Home 级状态，不进公司知识层。

**发布** 不发布。

**与 LangAlpha 自带的关系** 扩展自带 `catalyst-calendar`：沿用它的 `event_id` 稳定匹配、“只追加、改期留历史”和 `hard | window` 日期类型、`confirmed | guided | expected | inferred | rumoured | undated` 置信度词汇（Dalton 只有 `confirmed` 对应官方日期才建自动任务）；新增三态滚动、官方来源门槛、自动任务生成与读回对账。自带 skill 的 Excel 日历输出保留为可选附件。

**验证方式（真实运行）**
- 手动跑一次 `/dalton-earnings-calendar 全名单`（今天 2026-10-18，正值美股 Q3 财报季，META、MSFT、GOOGL、AMZN 预计在 10 月下旬发布，以官方公告为准）。检查四家都进入 `confirmed`（来源都是 IR 或 8-K）、各有三条自动任务文件（经 REST helper 建立）、`agenda.watch.events` 已同步、读回对账通过；只有 FMP/聚合站日期的公司显示“待确认”且没有自动任务。
- 人为把一家的台账日期改错一天，再跑一次，检查走“改期”路径（原地改 `next_run_at`，history 有记录），没有出现重复自动任务。
- 建好每周 cron，等第一次自动运行。

**验收标准** PS.18 的 P4-1 条目：四家公司的日期全部来自公司官方来源；候选日期都标“待确认”；自动任务时间正确（T-3 交易日按交易日历算，跳过节假日）；总结的“未落库”“解析失败行”两项存在。

**风险与注意**
- 自动任务的 `next_run_at` 必须带时区偏移；夏令时切换（美东 11 月第一个周日）前后的时间要单独检查。
- 公司改期是常见情况，必须原地改，不能出现“删了旧的、新的没建上”的空窗。
- 名单里没有工作区的公司不建链，否则自动任务会在 Home 里跑出没有知识层支撑的前瞻。
- 用户自动任务数量上限【待验证】（PK.15 同样提示）：27 家 Core × 3 条财报 once + 常驻 watch/move/delta，财报季会叠加；研究总监经 REST helper 清理 14 天前已完成的 `dalton-*-earn-*` 文件。

**预估工时** 2 天。

---

## PS.8 `dalton-earnings-preview`：财报前瞻（T-3 交易日自动触发）

**目标**
财报前三个交易日，产出一份前瞻：真正的预期门槛是多少 → 对业绩和指引看什么 → 核心分歧共享哪些 narrative；并把“本次要验证的判断”写成结构化预测，供财报后对账（蓝图 §2：前瞻与实际对账从未做过）。

**前置依赖** PS.7（自动任务）；PS.4（debate 方法）；`researchdata.research` 的 `alphaengine`、`sales-notes`、`tmtb`、`funda-ai` 路由与 `researchdata.finance(route="estimates")`。

**来源（只借原理）** `~/.openclaw/agents/main/agent/workshop-skills/company-preview/`（三个优先级、真实门槛五分、高频数据记录字段、debate 按机制聚类、X 一轮、停止与降级规则、`references/retrieval.md`）；LangAlpha `earnings-preview`（冻结时钟、EPS 质量观察、指引到隐含门槛、情景与历史反应、期权隐含波动、电话会问题）。

**新增/改动文件**（新建）
- `plugins/dalton/skills/dalton-earnings-preview/SKILL.md`
- `plugins/dalton/skills/dalton-earnings-preview/references/retrieval.md`（证据记录字段、四个入口、分阶段检索、图片核对、X 一轮、停止规则）
- `plugins/dalton/skills/dalton-earnings-preview/templates/preview.md`
- `plugins/dalton/skills/dalton-earnings-preview/scripts/predictions.py`（写/读 `predictions.json`，字段校验）

**流程（SKILL.md）**
1. **锁定标的与时点**：公司、代码、别名、财季（FY 还是 CY）、研究截止时间（自动任务触发时刻）、发布日期（来自台账，写来源；未确认写“未确认”）。窗口：预期 30 天（不够可延到上次财报），debate 90 天。截止时间之后的信息一律不用。取最新公司指引作为基线。冻结一个 as-of，所有时间敏感数据用同一个。
2. **优先级 1：buyside bogey 与高频数字**（花大部分精力在这里）。必须查 AlphaEngine、sales-notes、TMTB、FUNDA AI，每个都按公司名和代码分别查；检索词：bogey、whisper、hurdle、investor expectations、into the print、买方预期、业绩门槛、渠道跟踪，以及该公司的高频数据（sell-through、bookings、shipments、traffic、价格、排产）。
   - **真实门槛五分**：公司指引 / 卖方一致预期（快照日期）/ 具名机构预测 / 明确的买方门槛 / 本稿判断，分开写。只有来源明确引用投资者调研或买方预期时才叫 buyside bogey；sales 自己的预测不是买方共识；sales 转述写“某机构 sales 转述买方预期”；“大家都想看 beat”只作定性。没找到写“本轮未找到有来源的买方数字门槛”。
   - 每个数字记录：指标、期间、币种/单位、GAAP/non-GAAP 或业务口径、原始日期、说话人、原文。每个高频数据记录：最新值、较上次变化、样本覆盖、传导到收入还是利润、为什么对这次财报重要。不比较不对等的 MTD 窗口、毛额与净额、累计与单季。数字冲突先对齐期间和口径，真分歧并排列出，不取平均。
3. **优先级 2：业绩看法与关注点**：单公司前瞻、行业前瞻（标题不必含公司名）、sales 邮件、TMTB、FUNDA AI。区分“这季 beat 不 beat”和“指引/长期叙事的门槛”；区分“市场最关注什么”和“本稿认为最重要什么”。分析师提问里的数字管理层没确认的不是指引。
4. **EPS 质量与指引桥**（借自带 `earnings-preview`）：一次性项目、税率、股本、利息等会扭曲 EPS 的项目列表（项目｜为什么扭曲｜财报前风险｜财报后怎么判断｜影响模型哪一行）；过去 8 个季度 beat 频率与幅度（`get_earnings_history`），管理层指引保守程度（读 `management.md`），推出“指引隐含的真实门槛”。
5. **优先级 3：核心 debate 与 narrative 共性 + 一轮 X**：先合并同义观点，再做一轮公开 X 检索。**按机制聚类**：共同前提 → 因果机制 → 关键变量 → 对盈利/估值的含义；同样谈订单，可能一个是需求加速、一个是供给约束，不能因为关键词相同就合并。共享 narrative 需要 ≥2 个独立原始来源；单一来源标“单源观点”；点赞转发不证明机构共识。每个 debate 要有一个这次财报能观察到的验证点；只能长期验证的单独列。读 `debate.md`，写明与现有 debate 的对应关系。
6. **情景与反应**：Bull/Base/Bear 三情景（收入、EPS、关键驱动、管理层会怎么说、证伪点、可能的股价反应）；过去 4–8 次财报次日涨跌；期权隐含波动（`get_options_chain`/快照，跨式中价/现价，注明期限）。
7. **写结构化预测**：`predictions.py write` 生成 `research/<D>-preview/predictions.json`，每条：`id, metric, period, basis, our_expectation（数值或区间或方向）, street（值与快照日期）, bogey（若有）, why, verify_how（财报里看哪一行/哪句话）, linked_debate, linked_pillar`。至少包括：收入、EPS（或公司最关键的利润指标）、下季/全年指引、2–4 个 KPI、每个核心 debate 一条。
8. **写稿并自检**：开头三句（真实门槛、关键预期差、最需验证的判断）；每个数字第一次出现带期间和来源身份；自检：四个来源和行业前瞻都查过；关键引用读了全文；图片里的数字核对过原图；身份、期间、口径保留；同源转述没重复计数；缺数据与缺渠道分开写。

**输出模板**（`templates/preview.md` → `drafts/<D>-preview-<季度>.md`）
```
# {公司}（{TICKER}）财报前瞻｜{财季}
**核心判断**：真实门槛是什么；哪项高频变化最影响预期差；本次最可能验证或推翻哪条判断。（≤3 句）
研究截至：{日期 时区}。财报日期：{日期 BMO/AMC，来源} 或“未确认”。FY/CY 与关键口径：……
## 一、Buyside bogey 与高频数字 color
### 真正的预期门槛
- **{指标与期间}**：单位／口径
  - 公司指引；卖方一致预期（快照日期）；具名机构预测
  - 明确的买方门槛／范围：谁直接说或转述、日期、原话适用范围
  - 本稿判断：达到一致预期是否足够、什么构成预期差；区分证据与推断
### 高频跟踪
- **{指标与观察窗口}**：最新值与较上次变化；来源与样本覆盖
  - 对应的收入／利润变量；相对门槛意味着什么；口径限制；本次最需验证的条件
## 二、业绩看法与本次关注点
- **本季业绩** / **下季／全年指引** / **行业前瞻中的个股信息** / **本稿最重视的变量**
- EPS 质量观察表；指引隐含门槛（过去 8 季 beat 记录、管理层指引风格）
## 三、核心 debate 与 narrative 共性
### {分歧短名}：{具体问题}
- **共同叙事／前提** / **Bull 与 Bear 分歧在哪** / **传播与独立性** / **我的判断**（倾向、理由、改判条件）
## 四、情景与反应
三情景表；历史财报反应；期权隐含波动
## 五、财报公布时的核对清单
（与 predictions.json 一一对应：看什么、在哪看、怎样算超预期/低于预期）
## 来源与覆盖
各来源：查询词与窗口、读了几篇、最新日期、结果（找到 / 无相关结果 / 渠道失败）；引用清单
```

**知识层写入** `knowledge_update(doc="drivers")` 追加高频指标最新读数；`debate` 标注本次财报能检验的 debate；`knowledge_append_verdict`（trigger.kind=`earnings`，`trigger.ref` 为 `<dalton_wake>` 的 tm3 信号 id；通常 `NO_CHANGE`，若前瞻研究本身改变了判断则如实写），`agenda_ops` 加“财报当天核对 `predictions.json`”的验证项。

**发布** 草稿（`drafts/`）；PM 要求时 `publish_report(kind="earnings_preview")`。通过自动任务 `delivery` 把“核心判断”三句和文件路径推给 PM。

**与 LangAlpha 自带的关系** 扩展自带 `earnings-preview`：保留冻结时钟、EPS 质量表、指引桥、情景表、期权隐含波动、电话会问题；增加基金的三个优先级、真实门槛五分、内部研究库四入口、按机制聚类的 debate、结构化预测和对账。自带的 10 节 DOCX 结构不用，改为上面的中文模板。

**验证方式（真实运行）** 由 PS.7 生成的自动任务在 T-3 交易日自动触发（不是手动），在四家公司中至少一家的真实财报前跑通。另外手动对一家已发布财报的公司做“历史前瞻”（截止时间设在其财报前 3 天），检查没有用到截止时间之后的信息。

**验收标准** PS.18 的 P4-2 条目。硬性：真实门槛五分写清，buyside bogey 必须有明确来源或写“本轮未找到”；`predictions.json` 至少覆盖收入、EPS、指引、2 个 KPI 和每个核心 debate。

**风险与注意**
- 历史前瞻（回测）最容易泄漏未来信息，检索时必须带 `date_to`，并在自检里逐条确认来源日期。
- 内部研究库里的图片表格要读原图核对正负号、小数点、单位和期间；`description_verified=false` 的图片描述不能当数字证据。

**预估工时** 2.5 天。

---

## PS.9 财报点评链：`dalton-earnings-flash`（T0+N 小时）→ workflow `earnings-deep-update`（深度点评 → 模型更新 → thesis 校准 → 前瞻对账）

**目标**
财报发布后几个小时内给 PM 一份快讯（头条数字、指引、Q&A 要点、卖方初评、一句话结论和事件五词）；次日在卖方报告基本到齐后，自动跑深度链：八节深度点评、模型更新、thesis 财报后校准、和前瞻预测逐条对账、管理层指引兑现记录更新。

**前置依赖** PS.7、PS.8（预测文件）；PS.6 的模型（没有模型的公司跳过模型更新，只出点评与校准）；自带 `model-update`、`thesis-tracker`、`check-model`。

**来源（只借原理）** `~/.openclaw/workspace/skills/earnings-update/`（简版结构、14 项数据质量门、beat/miss 公式、估值口径、卖方窗口、sales 反馈 24 小时窗口）、`company-earnings-update/`（八节结构、四类信息标注、写作硬规则）、`hardware-earnings-update/`（半导体/硬件叠加层与 A–G 子模板）。剥掉：Gmail `gws` 检索（改用 `researchdata.research(route="sales-notes")`，按时间窗过滤）、Discord 不用表格、roic.ai。

**新增/改动文件**（新建）
- `plugins/dalton/skills/dalton-earnings-flash/SKILL.md`、`templates/flash.md`
- `plugins/dalton/skills/dalton-earnings-flash/references/kpi-packs.md`（按行业的 3–6 个 KPI 清单，原文移植）
- `plugins/dalton/skills/dalton-earnings-deep/SKILL.md`（启动 `earnings-deep-update` 的 skill，也可单独写八节点评）
- `plugins/dalton/skills/dalton-earnings-deep/references/eight-sections.md`
- `plugins/dalton/skills/dalton-earnings-deep/references/hardware-overlay.md`
- `plugins/dalton/skills/dalton-earnings-deep/references/calibration.md`（thesis 财报后校准 + 前瞻对账规则）
- `plugins/dalton/skills/dalton-earnings-deep/scripts/reconcile.py`（读 `predictions.json` 与实际值，输出对账表）
- `plugins/dalton/skills/dalton-earnings-deep/scripts/price_reaction.py`（盘后、次日、一周涨跌，绝对与相对 QQQ/SPY/板块 ETF；真正的 52 周高低，修掉原脚本用 6 个月数据冒充 52 周的问题）
- `workflows/earnings-deep-update/workflow.js`

**快讯流程（`dalton-earnings-flash`）**
1. 确认期间：用公司财季标签；当期、上一季、去年同期三组对比。10-Q 未出时用 8-K EX-99.1（或 6-K、HKEX 公告）并注明。
2. 一手先行：财报新闻稿、补充材料；上一季新闻稿取“上一季的同比增速”和上次指引（不能用环比代替）。
3. 电话会：`researchdata.research(route="transcript", ticker=T, ...)` 取本次电话会全文（分页读完），Q&A 只取 Q&A 部分；取不到时依次退到 IR 网播、8-K 预备发言，写明。
4. 卖方初评：AlphaEngine 财报后类别（sell_side_comment、foreign_report 等），窗口从发布到触发时刻，排除前瞻；记 doc_id、标题、机构、日期。FUNDA AI、TMTB 同窗口，不当一致预期。
5. sales 与投资者反馈：`sales-notes` 路由，窗口 `[T0, 触发时刻)`，T0 为官方发布时间戳；只要反应、反复被问的问题、持仓/资金流、多空交锋、盈利质量观点；去重，不点名终端客户，标“anecdotal”。没有合格内容就整条不写；访问失败写进来源，不能写“无反馈”。
6. KPI：按 `kpi-packs.md` 选 3–6 个；标可比性断点。
7. 公式：beat/miss = 实际/一致预期 − 1，只在同口径同币种下算；亏损、负 EPS、分母接近 0 时给绝对差；指引 beat 用中点对一致预期并保留区间；指引修正用新指引对上一季指引。来源冲突时实际值以 filing 为准。
8. 估值：最近一个完整常规交易日收盘价（未复权），远期 P/E = 价格 / 卖方修正后 EPS（同财年同口径）；多家卖方给区间和中位数；不混 GAAP 与 adjusted；负 EPS 写 NM。快讯时点卖方修正可能还没出，没有就写“卖方修正未出，次日深度点评补”。
9. 读 `predictions.json`，在快讯末尾给“前瞻要点初步对照”（3–5 条，完整对账在深度链）。
10. 给事件五词（快讯阶段允许写“初判”，深度链确认）：`knowledge_append_verdict`（trigger.kind=`earnings`，`trigger.ref` 为 t0 信号 id）。
11. 确认 PS.7 已建的 `dalton-<t>-earn-<period>-tp1.json`（深度链，T+1 交易日 20:00 当地时区）存在且时间正确；不存在就按模板在本工作区补建（常设授权，不需要问 PM）。
12. 自检 14 项（移植）：季度标签与 filing、电话会一致；时间敏感数字都来自本轮；上一季同比已列；beat/miss 同口径；指引变化对的是真实的上次指引；Q&A 来自 Q&A 部分；没有把前瞻当点评；sales 窗口准确；反馈可追溯、匿名、不当共识；没有空的“未找到”行；修正不混财年/币种/口径；价格口径正确；缺数据有标注；来源带链接或 doc_id。

**快讯模板**（`research/<D>-flash-<季度>.md`；对话/推送里发同样内容）
```
**{公司} {财季}：{一句话总结，以卖方主流观点为锚；若无卖方初评，用可靠新闻标题并注明}**
事件结论（初判）：{无变化/强化/削弱/证伪/新论点}——{受影响的 thesis 支柱或 debate}（JSON 里写 `NO_CHANGE` / `THESIS_STRENGTHENED` / `THESIS_WEAKENED` / `THESIS_BROKEN` / `NEW_THESIS`）
**业绩 summary：**
- **Key headline numbers：** 绝对值、本季同比、上一季同比、beat/miss（口径）
- **Key operating numbers：** KPI、同比、环比、上一季同比；1–2 个质量驱动
- **Guidance：** 下季/全年，对上次指引的变化，对一致预期；“未提供”
**Q&A takeaways：**
- **{话题}：** 1–2 句（5–10 个话题，只来自 Q&A）
**卖方初评：**
- **{机构}：** 定性 + 理由
- **Sales/Investor feedback（仅检索到时）：** 1–3 条，anecdotal
- **独立研究点评（仅有增量时）：** FUNDA AI／TMTB
- **预期调整：** 本财年与下一财年收入/EPS 修正前后、幅度（未出则注明）
- **估值：** 最新价与日期、修正后 EPS 的 P/E、区间/中位数、GAAP 与 adjusted
- （一句综合：一致之处 + 主要争议）
**前瞻要点初步对照：** 3–5 条
**Sources：** AlphaEngine / sales notes / FUNDA AI、TMTB / Filings / Company IR / 行情
```
多公司同日发布时，每家一块。

**深度链 workflow `earnings-deep-update`**
参数：`{ticker, company, quarter, workspace_dir, release_date, model_path?: string, preview_predictions?: path, sector_overlay?: "hardware"|null}`。
```js
export const meta = { name: 'earnings-deep-update', description: 'Dalton 财报深度链：八节点评 ∥ 模型更新 → thesis 校准 + 前瞻对账 → 审稿 → 知识层修订草案' }
const { ticker: T, company: C, quarter: Q, workspace_dir: W } = args
const D = args.release_date, R = `${W}/research/${D}-deep-${Q}`, S = '.agents/skills/dalton-earnings-deep'
const base = `先读 .agents/skills/dalton-conventions/SKILL.md 与 ${S}/SKILL.md；${C}（${T}）${Q}，发布日 ${D}。快讯稿在 ${W}/drafts/ 下 flash-${Q}。`
const OUT = { type: 'object', properties: { files: { type: 'array', items: { type: 'string' } }, summary: { type: 'string' }, gaps: { type: 'array', items: { type: 'string' } } }, required: ['files', 'summary'] }

phase('深度点评与模型')
const [report, model] = await parallel([
  () => agent(`${base} 按 ${S}/references/eight-sections.md${args.sector_overlay === 'hardware' ? ` 与 ${S}/references/hardware-overlay.md` : ''} 写八节深度点评到 ${W}/drafts/${D}-deep-${Q}.md；价格反应用 ${S}/scripts/price_reaction.py；卖方窗口为发布到发布后 3 个交易日。`, { agentType: 'general-purpose', label: '八节点评', schema: OUT }),
  () => args.model_path ? agent(`${base} 按 .agents/skills/model-update/SKILL.md 更新模型 ${args.model_path}：触发类型 Results；先出映射表到 ${R}/mapping.md，再写入另存的新版本（不覆盖），Change Log 完整；recalc 与 audit --strict 通过；输出 EPS walk 与估值变化到 ${R}/model-delta.md。`, { agentType: 'general-purpose', label: '模型更新', schema: OUT }) : null,
])

phase('校准与对账')
const calib = report === null ? null : await agent(`${base} 按 ${S}/references/calibration.md：
1) 用 ${S}/scripts/reconcile.py 对 ${args.preview_predictions || '（无前瞻预测文件，跳过对账并说明）'} 逐条对账，输出 ${R}/reconcile.md；
2) 按 .agents/skills/thesis-tracker/SKILL.md 的 post-earnings 模式，对 ${W}/thesis.md 每个支柱给信号与状态，写 ${R}/thesis-calibration.md；
3) 更新指引兑现记录：本季实际 vs 上季给的指引，判断管理层指引风格是否延续，写 ${R}/management-delta.md。
材料：${W}/drafts/${D}-deep-${Q}.md${model ? `，${R}/model-delta.md` : ''}。`, { agentType: 'general-purpose', label: '校准与对账', schema: OUT })

phase('审稿')
const review = calib === null ? null : await agent(`审阅 ${W}/drafts/${D}-deep-${Q}.md 及 ${R}/ 下的对账与校准文件。重点：beat/miss 口径；“综合判断”是否有判断；thesis 信号是否被价格走势替代；对账是否回避了错判。`, { agentType: 'dalton-reviewer', label: '审稿' })
const kp = await agent(`${base} 汇总 ${R}/ 全部文件与审稿意见（${review ? '见下' : '审稿缺席'}），按 PK.2 的 JSON Schema 写知识层修订草案 ${R}/knowledge-revision.json（{doc: {ops, reason, evidence_refs}}）：thesis 支柱状态与证据行、debate 变化、drivers 新读数、management 指引兑现行、lessons（前瞻错判的原因）、事件五词终判。\n${review || ''}`, { agentType: 'general-purpose', label: '知识层修订草案', schema: OUT })
return { report: report && `${W}/drafts/${D}-deep-${Q}.md`, model_delta: model && `${R}/model-delta.md`, reconcile: calib && `${R}/reconcile.md`, review_done: review !== null, knowledge_revision: kp && `${R}/knowledge-revision.json` }
```
派发数 5–6。启动 skill 收尾：读结果 → 用 `knowledge_update` 把修订逐条写入（深度链是自动运行，没有 PM 在场，只改工作版本，`thesis.official` 不动）；`THESIS_BROKEN` 或核心支柱 `impaired` 时推送 PM，并在 `agenda_ops` 里写一条 `pm_handoffs`（PK.14）→ `knowledge_append_verdict` 写终判 → 推送摘要。

**八节结构**（`references/eight-sections.md`，标题逐字移植）
```
### 一、核心判断        开头一词：温和 Beat / 强劲 Beat / 强劲超预期 / 指引上修 / Inline / 低于预期 / Miss & Lower；一个主框架；3–5 个量化关键词；“综合判断 + 核心风险”
### 二、本季度业绩 vs 预期
#### 2.1 关键财务指标表   | 指标 | 当季实际 | 指引中点 | 共识预期 | YoY | QoQ |（营收、GAAP/Non-GAAP 毛利率、营业利润率、EPS、经营现金流；SaaS 加 ARR、NRR、Net Adds、cRPO、Bookings；消费加同店、订单、AOV、付费用户）
#### 2.2 下一季度指引表   | 指标 | 下一季度指引 | 共识 | 隐含 YoY/QoQ |
#### 2.3 全年/下一财年展望上修表（如有） | 维度 | 上次业绩会指引 | 本次更新 | 上修幅度 |
点评（必写）
### 三、业务结构拆解     3.1 业务线收入分布表；3.2 主要增长引擎（本季变化/管理层指引/产品技术进展/客户或中标/投资点评）；3.3 非核心业务定位
### 四、三大财务报表关键变化 4.1 利润表（去年同期｜上季｜当季｜YoY｜QoQ + 值得关注的变化）；4.2 资产负债表；4.3 现金流量表（OCF、Capex、FCF、并购、回购、股息、融资/偿债；FCF 转换、capex 强度、SBC/FCF）
### 五、电话会议要点总结  5.1 CEO 开场 2–4 点；5.2 Q&A 精选表 | 议题 | 提问者 | 公司回答要点 | 投资含义 |（5–10 行）；5.3 CEO 闭幕陈述 1–2 句
### 六、市场观点与股价表现 盘后、次日、一周，绝对与相对（Nasdaq/SPY/板块 ETF）；为什么动；多头 3–5、空头 3–5；卖方目标价与评级变化
### 七、最大看点与持续追踪点 7.1 后续 5 大看点（正好 5 个）；7.2 跟踪指标表 | 跟踪维度 | 跟踪指标 | 更新频率 |；7.3 后续可能风险（必须绑定业务、客户、技术路径、估值或宏观变量，不写“宏观不确定”“地缘政治”这类泛泛风险）
### 八、综合投资观点     评级倾向（积极/中性/谨慎）；短期催化剂；估值参考（P/S、P/E、EV/EBITDA、EV/Sales，bull/base/bear）；核心持有逻辑 3–5；破坏 thesis 的条件
### 前瞻对账            （Dalton 增补）predictions.json 逐条：我们的预期｜一致预期｜实际｜对/错/部分｜错因
### thesis 校准         （Dalton 增补）支柱｜信号｜状态｜证据｜下一个验证点
### 资料来源
```
四类信息标注：事实 / 管理层指引 / 市场预期 / 研究判断（研究判断放在“点评”“综合判断”“关键观察”“值得关注的变化”里）。硬件叠加层（`hardware-overlay.md`）对 NVDA、AVGO、AMD、TSM、ASML、MU 等加载：额外检查超大规模云厂商 capex、代工产能（CoWoS、N3/N2、HBM 节奏）、出口管制、客户公告、竞争路线图；第二节加毛利结构、库存天数、晶圆结构、新品爬坡成本；第三节按公司自己的分部和 A–G 子模板（AI 加速器、定制 ASIC/网络、代工、设备、存储、模拟/功率、网络/光通信）；第六节基准加 SOX 和直接对标；第八节周期股加 mid-cycle P/E。

**校准与对账规则**（`references/calibration.md`）
- 对账：每条预测判“对 / 错 / 部分”，错的必须写错因（数据错、模型错、判断错、意外事件），并归纳成一条 `lessons` 候选（适用条件、反例）。连续两个季度在同一类指标上错同一个方向的，写进 `management` 文档的 `guidance_style`/`credibility_notes`（服务端会自动算 `beat_rate`、`median_surprise_pct`）或 `drivers` 文档（例如“该公司收入指引一贯保守约 2%”）。
- thesis 校准：用 `thesis-tracker` 的信号词（strongly confirming … strongly weakening、mixed、invalidating、untested）和状态词（strengthening / intact / watch / impaired / broken / changed / untested / retired）；价格变动先分解（PS.10 的 beta/alpha 方法）再当证据；五种漂移模式（理由变了、催化剂洗白、估值替代、拿期限搪塞、拿保护当理由）逐条判断有无。
- 指引兑现：本季实际对上季指引中点的偏差、方向，追加到 `management.md` 的指引记录表。

**知识层写入** 快讯：`knowledge_append_verdict` 初判一条。深度链（启动 skill 的主智能体读 `knowledge-revision.json` 后执行）：`knowledge_update` 改 `thesis`（支柱信号与证据行；状态变化由服务端记 `status_history`）、`debate`、`drivers`（KPI 新读数）、`management`（补填 `guidance_history[].actual` 与 `outcome`）、`lessons`（前瞻错判）、`models`（登记模型新版本）；`knowledge_append_verdict` 终判，`agenda_ops` 写下一季要验证的事。

**发布** 快讯：`research/` 事件笔记 + 推送（不进 `drafts/`）。深度点评：`drafts/`；PM 要求时走 `publish_report(kind="earnings_review", attachments=[模型新版本])`。

**与 LangAlpha 自带的关系** 扩展自带 `earnings-analysis`：保留证据契约（filing > 8-K 附件 > 新闻稿 > 演示稿 > 电话会；“call-only guidance”标注；non-GAAP 配 GAAP；缺失用 not guided / not disclosed / not provided / source not found）、EPS 质量与经常性 EPS 桥、估计修正包（packet）；把一次性的深度报告拆成“快讯 + 次日深度链”，增加内部研究库、sales 反馈窗口、前瞻对账、thesis 校准和管理层指引记录；模型更新直接调用自带 `model-update`，thesis 校准直接调用自带 `thesis-tracker` 的 post-earnings 模式。

**验证方式（真实运行）** 在本财报季的四家公司中至少一家完整跑通：PS.7 建的快讯自动任务按时触发 → 快讯推送 → 深度链自动任务次日触发 → 模型新版本、对账表、校准文件、知识层修订全部落盘。另对一家没有模型的公司跑一次，确认模型阶段被跳过且其余正常。

**验收标准** PS.18 的 P4-3、P4-4 条目。蓝图 P4 完成标准之一：“完成一次完整的前瞻、点评、模型更新、thesis 校准”。

**风险与注意**
- 快讯触发时卖方报告和电话会纪要可能还没进库，快讯要写明“截至触发时刻”的覆盖情况；N 小时的取值在第一次财报季后按实际入库延迟调整。
- 深度链在无人值守时修改知识层，只改工作版本，并且每处改动可回溯；重大改动（`THESIS_BROKEN`、核心支柱 `impaired`）必须推送 PM。
- 模型更新失败（Checks 不过）时不能静默跳过，要在知识层修订草案里写“模型未更新”，并在议程里排一次人工跟进（`agenda_ops`）。

**预估工时** 4.5 天（快讯 1.5 天，深度链 3 天）。

---

## PS.10 `dalton-stock-move`：股价异动归因 + 每只 Core 的价格触发

**目标**
股价异动时，先量化（同组与板块 ETF 对比，分清是板块 beta 还是个股 alpha，放量程度），再按 filing → 新闻 → 卖方 → 社媒的顺序找原因，给出归因和对 thesis 的含义（事件五词）。价格触发自动跑，PM 也可以问“X 今天为什么跌”。

**前置依赖** PS.0；覆盖名单里的同组映射（peer group）；行情接口。

**来源（只借原理）** `~/.openclaw/workspace/skills/stock-move-analyzer/`（六步、窗口映射、价量与同组计算、来源顺序、输出结构）；`coverage-update-richard/scripts/screen_movers.py` 的同组中位数、60 日超额收益标准差和 z 值（比 stock-move-analyzer 的“同组均值、简单差值”更稳健，两者统一成一种算法，见下）。

**新增/改动文件**（新建）
- `plugins/dalton/skills/dalton-stock-move/SKILL.md`、`templates/stock-move.md`
- `plugins/dalton/skills/dalton-stock-move/scripts/move_quant.py`（价量与同组：`analyze --ticker --window [--peers A,B] [--etf XLK]`）
- `plugins/dalton/skills/dalton-stock-move/references/peer-groups.md`（同组和板块 ETF 的来源说明：优先读覆盖名单的 peer group；没有就用 `company-wiki` 路由的行业页；再没有用板块 ETF）
- `docs/dalton/dev-guide/automation-templates/dalton-ticker-move-up.json`、`dalton-ticker-move-down.json`（模板；实际文件名按 PK.15：`dalton-<t>-move-up.json`、`dalton-<t>-move-down.json`，PK 预检按这两个名字识别）
- `plugins/dalton/skills/dalton-stock-move/scripts/make_price_triggers.py`（为覆盖名单里的 Core 公司批量生成价格触发文件，供 PS.0/P4 上线时用）

**统一的量化算法**（`move_quant.py`）
- 价格用复权收盘价（Adj Close）算收益；展示用未复权收盘价。
- 窗口收益 = P(窗口末最后一个收盘) / P(窗口起点前最后一个收盘) − 1。
- 同组收益 = 同组（不含目标）窗口收益的**中位数**；超额 vs 同组 = 目标 − 同组中位数；超额 vs 板块 ETF = 目标 − ETF。
- 日超额 = 目标日收益 − 同组日收益中位数；σ = 窗口起点前 60 个交易日日超额的标准差（样本 > 20 才算）；日 z = 日超额 / σ；窗口 z = 窗口超额 / (σ·√窗口交易日数)。
- 量比 = 当日成交量 / 前 20 日均量（不含当日）。
- 判定：`sector_beta` 当且仅当 |窗口超额| < 3 个百分点 且 |窗口 z| < 1.5；`stock_specific` 当 |窗口超额| ≥ 5 个百分点 或 |窗口 z| ≥ 2；中间为 `mixed`（两者都有）。阈值写在脚本常量里，第一次周会后按实际误报率调（原 skill 的阈值是“|目标/同组| < 2 且差 < 5pp 或两者都 < 3%”，代码注释与实现不一致，这里重定）。
- 输出 JSON：`close, change_pct, window, daily[], volume_vs_adtv, peers[{ticker, change_pct}], peer_median, sector_etf, etf_change, alpha_vs_peers, alpha_vs_sector, day_z, window_z, assessment, data_ok, warnings`。数据最新一根日线没出时 `data_ok=false`，不能下结论。

**流程（SKILL.md）**
1. 解析目标、窗口、方向（“今天”→1d，定性检索过去 2 天；“这周”→1w；“这个月”→1m；“最近”→1w，定性检索过去 2 周）。定性窗口比量化窗口向前多 2–3 天。
2. 第 1、2 步（必须成功）：`move_quant.py`。失败就停止归因，写明原因。
3. 第 3 步 filing：最近 8-K（美股）、6-K（在美上市的外国发行人）；列类型、日期、链接。HKEX/巨潮公告在 P6。
4. 第 4 步新闻：`web_search` 带日期范围，逐条核对日期在窗口内，标一手还是转载。
5. 第 5 步卖方：AlphaEngine（sell_side_report、sell_side_comment，`sort=relevance`，窗口日期）读最相关的全文；`sales-notes`、`tmtb` 同窗口。卖方观点是“卖方说法”，不是事实。三种状态（未登录 / 工具失败 / 窗口内无文档）分别写。
6. 第 6 步社媒：X（`x_search` 或另类数据插件）；只保留有实质内容的帖子，区分有信息的观点和散户情绪；雪球在 P6。
7. 第 3–6 步并行（可派 2–4 个 `dalton-evidence`，或主智能体顺序做；单只股票通常主智能体自己做更省）。
8. 归因：主要驱动（个股事件 / 板块共振 / 宏观 / 资金面 / 原因暂缺）、催化剂排序；**对 thesis 的含义**（读 `thesis.md`、`debate.md`：这个变动是否触及某个支柱或 debate，市场定价对还是错）；事件五词。
9. 收尾三步：`knowledge_append_verdict`（trigger.kind=`price_move`）；必要时 `knowledge_update(doc="debate")`（debate 倾向被价格证实或证伪）；`agenda_ops` 写下一项验证。

**输出模板**（`research/<D>-stock-move.md`；对话里同样结构）
```
## {TICKER} {窗口}涨跌分析
事件结论：{五词}——{一句话：对哪个支柱/debate 有什么含义；市场定价对还是错}
### 价量数据       收盘价｜区间涨跌｜成交量（ADTV 的 x.x 倍）｜逐日
### 同组对比       同组中位数 {x}%（成员：…）｜板块 ETF {ETF} {y}%｜超额 {z}pp（窗口 z={w}）｜判断：个股 alpha / 板块 beta / 两者都有
### Filing / 公告  （或“窗口内无新 filing”）
### 新闻催化剂     1. 标题 — 来源 日期 链接 + 摘要（一手/转载）
### 卖方观点       （或“窗口内无新研报”；或“未登录/工具失败”）
### 社交媒体       （或“讨论量不大”）
### 综合归因       主要驱动、个股事件还是板块共振、催化剂排序、对 thesis 的含义、下一项验证
```

**价格触发自动任务**（每只 Core 两条：跌 `dalton-<t>-move-down.json`、涨 `dalton-<t>-move-up.json`；下面是 `dalton-meta-move-down.json` 的内容）
```json
{
  "name": "META 异动（跌）",
  "status": "active",
  "trigger_type": "price",
  "trigger_config": {
    "symbol": "META",
    "market": "stock",
    "conditions": [ { "type": "pct_change_below", "value": 4.0, "reference": "previous_close" } ],
    "retrigger": { "mode": "recurring", "cooldown_seconds": null }
  },
  "timezone": "America/New_York",
  "instruction": "/dalton-stock-move META 今天 —— 价格触发：较昨收跌超 4%。先量化同组与板块，再找原因；若判定为板块 beta 且无个股事件，只用 knowledge_append_verdict 写一条 NO_CHANGE，不推送。",
  "workspace_id": "<META 工作区 id>",
  "thread": "new",
  "delivery": ["<PM 通知渠道>"]
}
```
（“涨”那条用 `pct_change_above`。`cooldown_seconds: null` = 当日触发一次后到下一个交易日再武装；`pct_change_*` 的 `value` 是百分数。阈值默认 4%，取自 `stock_tracker` 配置的 `price_1d_pct: 4.0`；高波动名字可按 2 倍 60 日日波动率单独设，在 `make_price_triggers.py` 里算好写入。）
- `make_price_triggers.py --coverage <coverage.yaml> --tier Core --out-dir <...>` 生成全部文件；分析师在本工作区直接写进 `.agents/user/automations/`（常设授权；每个文件写入后检查工具结果里没有 `NOT SAVED`）；研究总监批量上线时从 Home 调 REST helper `PUT /api/v1/dalton/automations/{name}`，不跨工作区写文件。
- 推送规则写在 instruction 里：板块 beta 且无个股事件的不推送，只记结论（“无重要变化不打扰 PM”，与 PS.11 一致）。

**知识层写入** 每次一条结论（`knowledge_append_verdict`）；`debate`、`drivers`、`agenda` 视归因结果用 `knowledge_update` 更新。

**发布** 不发布；推送摘要。

**与 LangAlpha 自带的关系** 新建（自带没有异动归因 skill）。自带 `thesis-tracker` 的“价格变动先分解再当证据”原则在第 8 步落地；价格触发直接用 LangAlpha 自带的价格监控。

**验证方式（真实运行）**
- 手动：对最近一次四家公司里涨跌最大的一天跑 `/dalton-stock-move {TICKER} {日期}`。
- 自动：上线四家公司的 8 条价格触发，持续一周，记录触发次数、推送次数、误报（判 beta 却推送，或判 alpha 却无原因）。

**验收标准** PS.18 的 P4-5 条目。硬性：量化两步必须成功才下结论；每条新闻、filing、卖方观点带链接或 doc_id 和日期且在窗口内；归因给出对 thesis 的含义。

**风险与注意**
- 价格触发的 `symbol` 最多 10 个字符、不带前缀；本阶段只上美股，港股代码写法在 P6 验证。
- 同一天大盘普跌时，多只 Core 会同时触发，各自独立跑会重复做宏观检索；可在 instruction 里要求“先查 `verdicts.md` 今天是否已有同类宏观归因，有则引用”。

**预估工时** 3 天。

---

## PS.11 每晚 thesis 增量：`dalton-thesis-delta` + 每晚 cron（无重要变化不推送）

**目标**
每晚对每家覆盖公司收一次“增量包”（一致预期变化、新研报和 sales note、新 filing、新闻、X），判断有没有重要变化；有就更新知识层并推送 PM，没有就不打扰。是否值得唤醒由 PK.16 的后端预检决定（`consensus`、`sec`、`internal`、`price` 探针，经 researchdata 桥直接取数，零 LLM 调用）；无信号的夜晚记 `PRECHECK_SKIPPED`，模型花费为 0（蓝图 §6）。

**前置依赖** PK.4（知识层工具）、PK.16（预检与 `consensus` 探针）；`researchdata.finance(route="estimates")`；`dalton_sec.list_filings`。

**来源（只借原理）** `~/.openclaw/workspace/scripts/stock_tracker.py`（collect → 模型判断 → notify → commit；两层“重要变化”逻辑；状态文件）、`config/stock_tracker/<T>.json` 的阈值和 `<T>_cron_prompt.md` 的重要性标准。剥掉：飞书卡片、3000 字上限、Eve/Lumos、写死的 META 数字与 25%/30% 费用阈值、`xreach`、`gsearch25.py`。

**新增/改动文件**（新建）
- `plugins/dalton/skills/dalton-thesis-delta/SKILL.md`
- `plugins/dalton/skills/dalton-thesis-delta/references/materiality.md`（重要性标准）
- `plugins/dalton/skills/dalton-thesis-delta/scripts/collect.py`（增量包：`collect --ticker --workspace-dir [--since]`；写 `research/tracker/<run_id>/packet.json`、`packet.md`；`commit --run-id --material yes|no --summary`）
- `plugins/dalton/skills/dalton-thesis-delta/config/default-thresholds.json`（同一份阈值给 PK.16 的 `consensus` 探针用：后端从仓库读默认值，从工作区文件镜像读 `research/tracker/thresholds.json` 覆盖值）
- `docs/dalton/dev-guide/automation-templates/dalton-ticker-delta.json`（实际文件名按 PK.15：`dalton-<t>-delta.json`）
- 运行时状态（每家公司工作区内）：`data/consensus/<YYYY-MM-DD_HHMM>.json`（一致预期快照，即修正历史）、`research/tracker/seen.json`、`research/tracker/last_run.json`、`research/tracker/updates.log`

**增量包内容**（`collect.py`，纯代码，不调模型）
- 一致预期快照与变化：`eps_{0q,+1q,0y,+1y}_vs_7d_pct`、`_vs_30d_pct`、对上次运行的变化、7 天上调/下调次数、目标价均值变化、1 日/5 日股价（与 QQQ 或覆盖名单里的基准对比）。
- 自动提示（只是阈值提示，是否重要由模型判断）：默认阈值（`default-thresholds.json`，原样移植）：下一财年 EPS 对上次运行变化 ≥1.0%；下一财年 EPS 对 7 天前 ≥2.0%；下一财年或下一季收入对上次运行 ≥0.7%；目标价均值对上次运行 ≥2.0%；7 天内下一财年下调次数 ≥5；1 日涨跌 ≥4%、5 日 ≥8%；任何新 SEC filing。每家公司可在工作区 `research/tracker/thresholds.json` 覆盖。
- 新文档：`researchdata` 的 `sales-notes`、`tmtb`、`funda-ai`、`alphaengine`（窗口起点前推 3 天）、`company-wiki` 新增条目（公司目录下的，或命中别名 ≥2 次的）；已见过的 id 记在 `seen.json`。
- 新 filing：SEC（8-K、10-Q、10-K、S-3ASR、424B2/B5、FWP、Form 4、13D/G），窗口起点前推 2 天。
- 新闻、X：各一次带日期的检索（X 接口在 P6；本期用 web_search）。
- 窗口：`since` = 上次 `until` − 1 天（首次回看 3 天）；`until` = 今天。

**流程（SKILL.md）**
1. 被 `<dalton_wake>` 唤醒后（预检已命中至少一个信号），运行 `collect.py` 补齐完整增量包（把本轮一致预期快照写进 `data/consensus/`）。若 `auto_flags` 为空且新文档、新 filing 为 0（预检信号在完整包里不成立）：`commit --material no --summary "无新增"`，`knowledge_append_verdict` 写一条 `NO_CHANGE`（`progress=none`，trigger.kind 按唤醒信号，`trigger.ref` 填信号 id），结束。**这一分支不读任何文档全文**。
2. 否则读 `packet.md` 和相关原文，按 `materiality.md` 判断。重要变化（任一即是，移植并去掉写死数字）：
   - 自动提示命中下一财年 EPS、收入或目标价均值；
   - 主要券商评级变化、目标价变动 ≥10%、或下一财年 EPS/费用/capex 预测变化，且不在已有观点池里；
   - 公司事实：业绩、指引、8-K、融资、管理层新表态、金额重大的法律或监管事项或时间表变化；
   - 某个 debate 的倾向或置信度变化；
   - 某个 KPI 越过 thesis 阈值（读 `data/thesis.json` 的确认/警告/证伪阈值）；
   - 买方持仓或 whisper 变化（来自 sales note 或 TMTB）；
   - 催化剂新增、改期或取消；
   - 两个互不相关的专业 X 账号提出同一个新观点，或 X 上的数据点被第二个来源证实；
   - 价格提示命中并找到原因（若价格触发已经跑过 PS.10，引用其结论，不重复归因）。
   - 财报 8-K（Item 2.02）总是重要（若 PS.9 快讯已跑，引用）。
   - 不重要：重复信息、单条 X、泛泛的前瞻、小券商小幅调目标价。
3. 重要：写一段增量笔记 `research/<D>-delta.md`（变化是什么、证据、影响哪个支柱或 debate、事件五词、下一项验证），用 `knowledge_update` 改知识层，`knowledge_append_verdict` 写结论（trigger.kind 按变化来源：`internal_material`、`filing`、`indicator` 等），推送 PM（摘要 ≤10 行）。不重要：只用 `knowledge_append_verdict` 写一条 `NO_CHANGE`（`progress=none`），`commit --material no`。
4. 无论哪种，`commit` 把本轮所有新 id 记入 `seen.json`；议程中到期的验证项（`V-`）若本轮已有答案，在同一次 `knowledge_append_verdict` 的 `agenda_ops` 里关闭。

**自动任务**
沙箱里的智能体通过 PTC 调 `researchdata`；PK.16 的预检在后端运行，不经沙箱，而是由后端直接请求 researchdata 桥（P0-D.0 已验证后端容器可达桥）。已定方案：
- `dalton-<t>-delta.json` 每晚触发。预检跑 `consensus` 探针（后端请求桥的 `finance(route="estimates")`，与该公司最近一份快照按 `default-thresholds.json` 比对）以及 `sec`、`internal`、`price` 探针，全部零 LLM 调用。
- 无信号：`PRECHECK_SKIPPED`，花费 0，不建线程。`consensus` 探针取到的快照仍由后端存档（PK.16 负责写入该工作区 `data/consensus/<YYYY-MM-DD_HHMM>.json`；沙箱停着时先存在预检记录里，下次轮次开始时补写），保证 PS.13 的归因有逐日快照。
- 有信号：唤醒，线程第一条是 `<dalton_wake>`（含信号 id 和命中的阈值），分析师从第 1 步开始。
```json
{
  "name": "META 每晚 thesis 增量（dalton-meta-delta.json）",
  "status": "active",
  "trigger_type": "cron",
  "cron_expression": "30 21 * * 1-5",
  "timezone": "America/New_York",
  "instruction": "/dalton-thesis-delta META —— 已由预检唤醒：先读 <dalton_wake> 的信号，再跑 collect.py；按 materiality.md 判断，重要才推送。",
  "workspace_id": "<META 工作区 id>",
  "thread": "new",
  "delivery": ["<PM 通知渠道>"]
}
```
不写 `llm_model`：被唤醒的夜晚就是值得深读的夜晚，跟随 lead 模型。

**知识层写入** 见流程第 3、4 步。

**发布** 不发布；重要变化推送。

**与 LangAlpha 自带的关系** 状态格式借用自带 `thesis-tracker` 的证据台账行（日期、期间、来源、事实、我们的预期、市场预期、解读、支柱、信号与幅度、证据质量、影响、跟进）和信号词；新增增量采集脚本、阈值提示、重要性标准和“无事不推送”。

**验证方式（真实运行）** 四家公司连续一周每晚运行。记录：每晚每家的预检结果（`PRECHECK_SKIPPED` 或唤醒及信号）、唤醒后的 `material` 判断、推送次数、花费（`get_run_charge_usd`）；抽查 3 个 skip 和 `no`（是否漏了重要变化）和全部 `yes`（是否值得打扰 PM）。

**验收标准** PS.18 的 P4-6 条目。蓝图 P4：“连续一周，四家公司都有事件驱动的更新和事件五词结论。”“无事”夜晚花费为 0（预检记 `PRECHECK_SKIPPED`），且当晚的一致预期快照已存档。

**风险与注意**
- 一致预期快照来自 yfinance 类数据，周末和节假日可能不更新，`fetched_at` 与上次相同时跳过对比（原脚本的做法）。
- `seen.json` 必须在 `commit` 时才更新；collect 失败的轮次不能把文档标成已见。

**预估工时** 2 天（`consensus` 探针本身由 PK.16 交付，不计在这里）。

---

## PS.12 公告监控：PK watch 预检唤醒 + `dalton-filing-review`

**目标**
覆盖公司一有新公告，对应公司的分析师就被唤醒读公告、判断影响（事件五词），不靠人提醒（蓝图 §4.2“公告监控”）。轮询和唤醒由 PK 交付（`dalton-<t>-watch.json` + 预检 `sec` 探针，PK.15–PK.17）；本步只交付分析师被唤醒后**怎么读公告**。

**前置依赖** PK.16 的 `sec` 探针与 `<dalton_wake>` 注入；`dalton_sec.list_filings`；正文读取 `researchdata.finance(route="sec", ...)`（10-K 等大型 filing 由桥给较长超时）或自带 `get_sec_filing`；港股/A 股（`hkex`、`cninfo` 探针）在 P6。

**新增/改动文件**（新建）
- `plugins/dalton/skills/dalton-filing-review/SKILL.md`
- `plugins/dalton/skills/dalton-filing-review/references/form-guide.md`（各类公告看什么）
- `plugins/dalton/skills/dalton-filing-review/references/watch-forms.md`（建议写进各公司 `agenda.watch.filings.forms` 的表单清单与 Form 4 金额阈值，供 PK 的 sec 探针过滤；由 P1.3 初始化或本 skill 第一次运行时用 `knowledge_update(doc="agenda")` 写入）

**哪些公告需要读**（`watch-forms.md`）
- 读：8-K（全部 Item；Item 2.02 若该季快讯已跑则只做核对）、10-Q、10-K、6-K、20-F、S-1/S-3/424B（融资）、DEF 14A（薪酬与治理）、SC 13D/13G（大股东变化）、Form 4 中 CEO/CFO 或金额超过阈值的交易。
- 不读、只记录：FWP 等例行文件。

**审阅流程（`dalton-filing-review`；`<dalton_wake>` 里有 `kind="filing"` 信号时由 watch 线程调用，也可手动 `/dalton-filing-review META 10-Q <accession>`）**
1. 先看 `verdicts.md` 当天是否已有 `trigger.kind=earnings` 的结论（财报日 8-K 与快讯撞车），有则只核对快讯里的数字与 8-K 附件一致，写 `NO_CHANGE` 或修正。
2. 按 `form-guide.md` 读对应要点：
   - 10-Q/10-K：和上一期比 MD&A 措辞变化、风险因素逐条 diff、分部口径变化、会计政策变化、或有事项、回购与股本、RPO/递延收入；新数字用 `dalton_sec.get_company_facts` 取（Q 只配 3 个月口径，Q4 = FY − 9M），写进 `drivers` 的最新读数；公司已有模型时，在议程里加“模型待更新”的验证项（或直接按自带 `model-update` 出映射表，由 PM 决定是否写入）。
   - 8-K：按 Item 判断（1.01 重大协议、2.02 业绩、2.05/2.06 重组与减值、5.02 高管变动、7.01/8.01 其他）。
   - 融资类：规模、价格、稀释、用途；按 PS.2 的口径重算一次严格净现金。
   - 13D/13G、Form 4：谁、多少、方向、是否 10b5-1。
3. 对照 `thesis.md` 的支柱与阈值、`debate.md`，判断影响，给事件五词。
4. 输出事件笔记 `research/<D>-filing-<form>-<accession 短号>.md`：一句话结论（五词）、关键变化（带页码或 Item 号）、对 thesis/debate 的影响、需要跟进的事。
5. 收尾三步：`knowledge_append_verdict`（trigger.kind=`filing`，`trigger.ref` 为信号 id，`evidence` 里带 accession）；非 `NO_CHANGE` 时 `knowledge_update` 改 `thesis`/`debate`/`drivers`；管理层表述变化（例如风险因素新增条目）写 `management`；推送 PM。

**知识层写入** 见第 5 步。

**发布** 不发布；非 `NO_CHANGE` 推送。需要正式留档的（例如重大融资）可走 `publish_report(kind="event_note")`。

**与 LangAlpha 自带的关系** 新建。沿用自带 `earnings-analysis` 的来源阶梯（10-Q/10-K > 8-K 附件 > 新闻稿）。

**验证方式（真实运行）** 四家公司 Q3 财报季内，至少一家的 10-Q 被 sec 探针发现（`dalton_signals` 有记录）并在下一次 watch 触发时完成审阅；按 PK.16 的验证方法把游标回拨一份 filing，确认同一 accession 只处理一次。

**验收标准** PS.18 的 P4-7 条目：发现延迟 ≤ watch 间隔（PK 样例为 30 分钟）；10-Q 审阅写出风险因素 diff（或明确写“风险因素无变化”）；结论的 `trigger.ref` 与信号 id 一致。

**风险与注意**
- 风险因素 diff 需要上一期全文；`researchdata` 的 read-item 若取不到整节，写明“本轮未取得上一期全文”，不能写“无变化”。
- 本步依赖 PK.16 的 `sec` 探针，P4 按 PK 的顺序先交付探针，不另做临时轮询方案。

**预估工时** 1.5 天。

---

## PS.13 workflow `weekly-coverage-review`（P5 周会）+ 每周 cron

**目标**
每周一份周会稿：全覆盖名单对同组做异动筛选；Core 逐家并行分析（五源）；Watch 只看命中的；对每家有异动的公司做“EPS 预期修正 vs 估值倍数”归因；汇总一周观点变化（来自 `verdicts.md` 与知识层修订）；最后给出至少一条明确的 call，或明确写“暂无高 conviction”（蓝图 P5 完成标准）。

**前置依赖** PS.10（量化算法）、PS.11（一致预期快照与 `verdicts.md`）、PS.15（call）；覆盖名单（Core/Watch、同组、持仓标记）。

**来源（只借原理）** `~/.openclaw/workspace/skills/coverage-update-richard/`（`screen_movers.py` 的筛选公式与阈值、Core 五源、Watch 命中规则与检索顺序、周报结构、失败处理）、已归档 `~/.openclaw/workspace/references/archived-skills/2026-10-11/weekly_review/SKILL.md`（六维归因框架：价格、盈利预测、估值倍数、宏观政策、资金面、基本面情绪）。剥掉：Richard 名字与风险收益表、飞书文档与 folder token、写死的持仓、AlphaEngine 每日额度数字。

**新增/改动文件**（新建）
- `workflows/weekly-coverage-review/workflow.js`
- `plugins/dalton/skills/dalton-weekly-review/SKILL.md`（启动 skill）
- `plugins/dalton/skills/dalton-weekly-review/scripts/screen_movers.py`（移植；同组与基准从覆盖名单读，不写死）
- `plugins/dalton/skills/dalton-weekly-review/scripts/attribution.py`（EPS 修正 vs 倍数归因，新设计，见下）
- `plugins/dalton/skills/dalton-weekly-review/references/core-five-sources.md`、`watch-rules.md`、`attribution.md`
- `plugins/dalton/skills/dalton-weekly-review/templates/weekly.md`
- `docs/dalton/dev-guide/automation-templates/dalton-director-weekly.json`（PK.15 已定名；本文给出 instruction 内容）

**运行位置** 周会是跨公司的，在 Home 由研究总监运行（PK.15 已把 `dalton-director-weekly.json` 定在 home）。子智能体用相对路径读各公司工作区的知识层（`../<TICKER>/thesis.md` 等，PK.6 (4)；批量概览可先用 `dalton_coverage_board`），**只写周会自己的目录**：`Home/dalton/research/weekly-<end>/`。对公司知识层的修改写成提案 `Home/dalton/research/weekly-<end>/proposals/<TICKER>.json`，周会结束后由启动 skill 用 `delegate_to_analyst` 交给各公司分析师确认写入（或按 PK 的研究总监规则处理）。

**筛选（`screen_movers.py`，移植公式）**
- 窗口：上周同一工作日收盘 → 最近一个完整交易日收盘。名单里的港股/A 股本阶段只做价格筛选、不做深度检索（P6）；休市日在稿首说明，休市市场不参与比较。最新日线没出（超过一半美股代码的最后日期 < 窗口末）时 `ok:false`，不下结论。
- 每家：窗口收益（复权）、同组中位数、窗口超额、日超额与 z（60 日 σ，样本 > 20）、窗口 z（σ·√n）、日量比（对前 20 日均量）。同组中位数用覆盖名单该组全部名字（含未覆盖的同组公司）加组内额外 ETF。
- Watch 命中规则（原样）：窗口超额超过 ±6 个百分点或超过 2 倍标准差；单日超额超过 ±5 个百分点或超过 3 倍标准差；单日量比超过 2.5 倍。只有量能命中、或同一事件引起的一串命中，按一个事件写。
- Core 不看命中，全部分析。
- 输出 JSON：`base, end, ok, warnings, bench[], core[], flagged[], rows[]`。

**EPS 修正 vs 估值倍数归因**（`attribution.py`，**新设计**：归档的 weekly_review 只有定性框架，没有公式）
- 对每家：`ln(P_end/P_base) = ln(EPS_end/EPS_base) + ln(PE_end/PE_base)`，其中 EPS 用远期 12 个月一致预期（NTM），PE = 价格 / NTM EPS。
- NTM EPS 由本财年（0y）和下一财年（+1y）一致预期按剩余天数加权：`NTM = w·EPS_0y + (1−w)·EPS_+1y`，`w = 本财年剩余天数 / 365`。窗口起点的 EPS 用快照历史里离 `base` 最近的一份（PS.11 每晚存的 `data/consensus/`）；没有快照时用一致预期接口的 `7daysAgo` 值（窗口为一周时正好对齐），并标注“近似”。
- 输出表：公司｜股价变化｜EPS 预期贡献（pp）｜倍数贡献（pp）｜主导（盈利驱动 / 估值驱动 / 两者）｜盈利预测变化的来源（哪家券商、哪个数据点）｜倍数变化的解释（利率、行业风险溢价、远期增长预期、资金面；无明确逻辑写“原因暂缺”）。
- 主导判定：两项贡献同号时，绝对值大的为主导，占比 ≥70% 写“盈利驱动”或“估值驱动”，否则“两者”；异号时写“盈利 X、倍数 Y 相抵”。
- 负 EPS 或 EPS 接近 0 的公司不做分解，写“NM，改用 EV/Sales 定性”。
- 这一节回答蓝图 §2 的缺口：“历史上是 EPS 还是倍数驱动”。周度结果作为提案写进各公司 `drivers` 文档的股价驱动记录【字段以 PK.2 drivers schema 为准】，季度复盘时可以看出某家公司的股价主要被什么推动。

**Core 五源**（`core-five-sources.md`，每家一个子智能体，一次只查一家公司）
1. AlphaEngine：两次检索（卖方报告、卖方点评、会议纪要一类；新闻、公告一类），`sort=relevance`，窗口前后各放宽 1 天；某类相关结果不到 3 条时换另一种语言补一次；额度用尽时停止并列出未覆盖公司，不能写“无更新”。
2. 内部库：`sales-notes`（GS、Jefferies、BofA 等 sales 邮件）、`tmtb`、`funda-ai`、`company-wiki` 路由。别名太宽（一次返回满额）时按别名分开查。
3. 新闻：一次带日期的检索，再为每个异常日查一次“<公司> stock <日期> why”。
4. X：带 `since` 的检索，按浏览和点赞排序；与价格或其他来源对不上的不用。
5. 雪球：P6 接口；本期跳过并在缺口里写明。
每家必须回答：窗口涨跌对同组；哪天放量或背离；那天发生了什么；卖方观点（机构、日期、评级、目标价）；这周为什么动（含 EPS vs 倍数归因）；对 thesis 的影响（读该公司 `thesis.md`、`debate.md` 和本周 `verdicts.md`）；下一个检查点。

**Watch 规则**（`watch-rules.md`）只检索命中的公司。顺序：内部库结果 → 每个异常日一次新闻检索 → 必要时 X。AlphaEngine 放在 Core 全部完成之后，且仅在前面解释不了、或窗口超额 > ±10 个百分点、或当天有财报/指引/评级事件时才查。按事件分组写（政策、财报、评级、宏观）。结尾表列出未命中的 Watch（涨跌对同组、“未检索”），持仓的 Watch 排最前、即使未命中也列一行。

**workflow 脚本骨架**
```js
export const meta = { name: 'weekly-coverage-review', description: 'Dalton 周会：全名单筛选 → Core 逐家并行五源 → Watch 命中定向检索 → EPS vs 倍数归因 → 观点变化汇总 → call' }
const { home_dir: K, end: E, coverage_path: L } = args   // K = Home/dalton，L = Home/dalton/coverage.yaml
const R = `${K}/research/weekly-${E}`, S = '.agents/skills/dalton-weekly-review'
const base = '先读 .agents/skills/dalton-conventions/SKILL.md 与 ' + S + '/SKILL.md。'
const SCREEN = { type: 'object', properties: { ok: { type: 'boolean' }, base: { type: 'string' }, core: { type: 'array', items: { type: 'object', properties: { ticker: { type: 'string' }, workspace_dir: { type: 'string' }, held: { type: 'boolean' } } } }, flagged: { type: 'array', items: { type: 'object', properties: { ticker: { type: 'string' }, event_hint: { type: 'string' }, workspace_dir: { type: 'string' } } } }, warnings: { type: 'array', items: { type: 'string' } } }, required: ['ok', 'core', 'flagged'] }
const CO = { type: 'object', properties: { ticker: { type: 'string' }, file: { type: 'string' }, move_pp: { type: 'number' }, excess_pp: { type: 'number' }, driver: { type: 'string' }, thesis_impact: { type: 'string' }, verdict: { type: 'string' }, call_candidate: { type: 'boolean' } }, required: ['ticker', 'file'] }

phase('筛选与归因')
const scr = await agent(`${base} 用覆盖名单 ${L}，窗口终点 ${E}：跑 ${S}/scripts/screen_movers.py 与 ${S}/scripts/attribution.py，结果写 ${R}/screen.json、${R}/attribution.json，返回 Core 名单（含工作区路径、是否持仓）与 Watch 命中名单。`, { agentType: 'general-purpose', label: '筛选', schema: SCREEN })
if (scr === null || !scr.ok) return { ok: false, reason: '筛选失败或数据未更新，不下结论', warnings: scr && scr.warnings }
const core = [...scr.core].sort((a, b) => (b.held ? 1 : 0) - (a.held ? 1 : 0))
// 平台上限：每次运行最多派 64 个子智能体（config.yaml 的 workflow 段）；扣掉筛选 1、call 3、周会稿 1
const room = Math.max(0, 64 - 5 - core.length)
const watch = scr.flagged.slice(0, room)
if (scr.flagged.length > room) log(`Watch 命中 ${scr.flagged.length} 家，超出平台派发上限，余下 ${scr.flagged.length - room} 家写进“口径与缺口”，下次优先`)

phase('Core 逐家')
const coreRes = await parallel(core.map((c) => () => agent(`${base} 公司 ${c.ticker}${c.held ? '（持仓）' : ''}，知识层在 ${c.workspace_dir}。按 ${S}/references/core-five-sources.md 做本周（${scr.base} 至 ${E}）五源检索，归因数据见 ${R}/screen.json 与 ${R}/attribution.json。写 ${R}/core-${c.ticker}.md；若有知识层修改建议，写 ${R}/proposals/${c.ticker}.json。`, { agentType: 'dalton-evidence', label: c.ticker, phase: 'Core 逐家', schema: CO })))

phase('Watch 命中')
const watchRes = await parallel(watch.map((w) => () => agent(`${base} Watch 公司 ${w.ticker}（提示：${w.event_hint}）。按 ${S}/references/watch-rules.md 定向检索，写 ${R}/watch-${w.ticker}.md。`, { agentType: 'dalton-evidence', label: w.ticker, phase: 'Watch 命中', schema: CO })))

phase('汇总与 call')
const okCore = coreRes.filter((x) => x !== null)
const cands = okCore.filter((x) => x.call_candidate).map((x) => x.ticker)
const calls = cands.length === 0 ? [] : await parallel(cands.slice(0, 3).map((t) => () => {
  const c = core.find((y) => y.ticker === t)
  return agent(`按 .agents/skills/dalton-call/SKILL.md 对 ${t} 做 conviction 检查（知识层 ${c.workspace_dir}，本周材料 ${R}/core-${t}.md）。证据门槛不过就输出“暂无高 conviction”及缺什么。写 ${R}/call-${t}.md。`, { agentType: 'general-purpose', label: `call ${t}`, phase: '汇总与 call', schema: CO })
}))
const doc = await agent(`${base} 按 ${S}/templates/weekly.md 汇总 ${R}/ 下全部文件为周会稿 ${K}/drafts/${E}-weekly.md。观点变化部分读各 Core 工作区本周 verdicts.md 与 proposals/。Core 失败的公司：${core.filter((_, i) => coreRes[i] === null).map((c) => c.ticker).join(',') || '无'}，在“口径与缺口”写明。`, { agentType: 'general-purpose', label: '周会稿', schema: CO })
return { ok: doc !== null, draft: `${K}/drafts/${E}-weekly.md`, core_done: okCore.length, core_total: core.length, watch_done: watchRes.filter((x) => x !== null).length, calls: calls.filter((x) => x !== null).map((x) => x.ticker), proposals_dir: `${R}/proposals` }
```
派发数：1 + Core（≤27）+ Watch（≤15）+ call（≤3）+ 1 ≤ 47，低于 64。Core 超过约 40 家时拆成两次运行（按 tier 或行业），或者 Core 每家改为“两家一个子智能体”，不建议后者（一次只查一家公司是原方法的纪律）。

**周会稿模板**（`templates/weekly.md` → `Home/dalton/drafts/<end>-weekly.md`）
```
# 覆盖周报 {起点} 至 {终点}（Core 逐家 + Watch 异动筛选）
窗口与基准：{窗口}；名单版本 {日期}，Core {n} / Watch {m}；基准涨跌 SPY / QQQ / KWEB / XLF / XLY / IWM / IGV / 恒指；休市说明
## 本周判断（3–6 条，结论先行）
## 一、观点变化
| 公司 | 本周事件五词 | 变化的支柱或 debate | 旧 → 新 | 原因与证据 |
（无变化的公司合并一行：“以下公司本周 NO_CHANGE：……”）
## 二、Conviction call
- {TICKER}：{方向}｜{仓位级别 Core/Tactical/Small}｜预期差一句话｜三情景 IRR｜证伪条件｜对冲（详见 call 文件）
- 或：“本周暂无高 conviction”——在等什么（具体到事件或数据）
## 三、Core 逐家（持仓优先，标“持仓”）
### {公司}（{TICKER}）{持仓}｜本周 {x}%，对同组 {y}pp
- **表现**：涨跌对同组；放量或背离的日子；EPS 预期贡献 {a}pp / 倍数贡献 {b}pp → {盈利驱动/估值驱动/两者}
- **发生了什么**：事件（来源）
- **卖方观点**：机构、日期、评级、目标价、核心论点
- **社区**：X 有实质内容的观点（链接）
- **判断**：这周为什么动；对 thesis 的影响；下一个检查点
## 四、Watch 异动（按事件分组）
## 五、未命中的 Watch
| 公司 | 本周涨跌 | 对同组 | 状态 |（持仓排最前；状态“未检索”）
## 六、EPS 修正 vs 估值倍数归因表（全部有异动的公司）
## 口径与缺口
数据未更新、额度用尽、工具失败、未覆盖来源（雪球 P6 等），分别写明
```

**自动任务**
```json
{
  "name": "周会稿（dalton-director-weekly.json）",
  "status": "active",
  "trigger_type": "cron",
  "cron_expression": "0 10 * * 6",
  "timezone": "Asia/Shanghai",
  "instruction": "/weekly-review 本周 —— 窗口终点取最近一个完整交易日；跑 weekly-coverage-review workflow；完成后按 dalton-research-director 的周会规则：提案交给各公司分析师确认（delegate_to_analyst，已开 auto_approve_handoffs），统计各公司本周五词分布，把“本周判断”和“Conviction call”推送 PM。周会稿留在 Home/dalton/drafts/，不走 publish_report（Home 没有发布工具）。",
  "workspace_id": "home",
  "thread": "new",
  "delivery": ["<PM 通知渠道>"]
}
```
（默认周六 10:00 Asia/Shanghai，在 PS.7 的财报日历维护 09:00 之后；周会日由 owner 定，见文末待决问题，改的只是这一行 cron。）

**知识层写入** 周会在 Home 运行，Chief of Staff 没有 `knowledge_*` 工具（PK.4 (7)），所以周会不直接改公司知识层：各公司的修改写成提案 `Home/dalton/research/weekly-<end>/proposals/<TICKER>.json`；需要 PM 关注的问题用研究总监的 `dalton_agenda_note`（PK.19）追加到该公司议程；需要分析师确认写入的，用 `delegate_to_analyst` 交办（无人值守 run 里派单自动批准，不会停在确认卡上）。

**发布** 周会稿默认草稿（PM 周会阅读）；其中的 conviction call 若要成为正式建议，走 PS.15 的 `publish_report(kind="conviction_call")`。

**与 LangAlpha 自带的关系** 新建。自带 `morning-note`、`market-watch` 是单日市场视角；`thesis-tracker` 的 portfolio review 模式提供“多仓位逐个过”的结构参考，但没有同组筛选和归因。

**验证方式（真实运行）** 用真实一周（建议 2026-10-26 至 10-30 财报周，四家都有事件）跑一次，看 Core 是否全部完成、Watch 命中是否合理（抽查 3 个未命中的确实没有大事）、归因表数学是否闭合（两项贡献之和 = 股价对数变化，误差 < 0.1pp）。然后让 cron 自动跑下一周。

**验收标准** PS.18 的 P5-1 条目。蓝图 P5：“周会稿包含股价归因、观点变化和至少一条明确的 call 或暂无高 conviction。”

**风险与注意**
- Core 27 家并行检索内部库和 AlphaEngine，单次额度消耗大；数据接口返回额度错误时，后面的公司要写“额度用尽，未覆盖”，并在下次运行优先补。
- 覆盖名单里有非上市行（例如 OpenAI、Anthropic），筛选时跳过。
- 一周内知识层可能已被事件更新过多次，“观点变化”要以 `verdicts.md` 为准，不靠子智能体回忆。

**预估工时** 5 天。

---

## PS.14 workflow `topic-research`（专题研究）

**目标**
分析师按议程（或 PM 指示）发起的专题研究：一个聚焦问题（例如“超大规模云厂商 2027 年 capex 与折旧对 EPS 的影响”），多源取证、交叉验证、盲区检查、数字核验、不同家族审稿，产出一份逐条标注置信度的专题报告，并把关键发现写进相关公司的知识层。基金 wiki 只读，不回写。

**前置依赖** PS.1；PK.10（草稿期审稿）。

**来源（只借原理）** `~/.openclaw/workspace/skills/topic-deep-dive/SKILL.md`（v8：时效规则、逐条置信度、Phase 0–7、反走捷径检查、独立审稿）。剥掉：写死的模型（Opus、gemini、codex）、PDF 生成、Firecrawl 计费说明、miku_ai、小红书、`research-orchestrator` 旧路径、地缘政治的 C1–C6 默认簇（保留为可选模板）。行数下限（300/400/500）不作为门槛。

**新增/改动文件**（新建）
- `workflows/topic-research/workflow.js`
- `plugins/dalton/skills/dalton-topic-research/SKILL.md`（启动 skill：澄清并锁定范围 → 调 workflow → 收尾写知识层）
- `plugins/dalton/skills/dalton-topic-research/references/principles.md`（时效、置信度、矛盾处理、来源最低要求）
- `plugins/dalton/skills/dalton-topic-research/references/clusters-investment.md`（投资类默认簇：财务数据+估值 / 竞争格局+行业趋势 / 催化剂+风险 / 管理层+治理 / 牛熊观点+卖方研究）
- `plugins/dalton/skills/dalton-topic-research/scripts/validate_topic.py`（移植 `validate-process`/`validate-report` 的实质检查：置信度标签与来源数一致、低置信度占比、引用闭环、信息缺口节存在）
- `plugins/dalton/skills/dalton-topic-research/templates/topic.md`

**原则**（`principles.md`）
- 时效：用户给了时间范围，每次检索都带范围，范围外来源只作背景，报告头写“时间范围”；没给范围，优先近 30 天，新来源优先，超过 6 个月的数据标“数据截至 YYYY-MM”，超过 12 个月只作背景、不支撑核心结论。每个关键数字带日期。
- 逐条置信度（文字标签代替原来的彩色图标）：【高】3 个以上独立可信来源一致；【中】2 个；【低】单一来源或 AI 检索综合（写原因）；【冲突】保留各方原话，不合并（正确写法：“来源 A 称‘数千人’[S15]，来源 B 称‘数百人’[S16]”，错误写法：“数百至数千人”）；【更正】写错值、正值、依据。
- 关键结论至少 2 个来源；缺口写“目前缺乏数据”；人名写全名和职务。
- 低置信度条目占比低于 15% 时提示“可能存在置信度注水”（原 validate-process 规则），由审稿人复核。

**流程**
```
阶段 0  规划（1 个子智能体）：复杂度；3–6 个检索簇，每簇 5–8 条中英文检索词、预期信息类型、关键来源、盲区；
        报告框架与 5–8 个核心问题；预期矛盾；核验清单 → plan.md
阶段 0.5 一手材料（1）：涉及上市公司时读最近 filing 附件、1–2 次电话会、近 6 个月卖方；行业/宏观题读 AlphaEngine 行业检索与内部库 → primary.md
阶段 2  分簇取证（parallel，3–6 个 dalton-evidence）：每簇跑完全部检索词，读 3–5 个全文；输出 Facts / Direct Quotes / Timeline / Contradictions / 全文摘录 / Sources → cluster-<k>.md
        并行：内部记忆（sales-notes、tmtb、funda-ai，至少 1 次，英文检索效果好）；Guidepoint（1–2 次英文检索，引用 ≤20 个英文词）
阶段 3  交叉验证 + 3.5 盲区检查（1）：跨簇互证升级置信度；矛盾保留原话；必须写“盲区检查完成”或补派最多 2 个子智能体
阶段 4  写作（1）→ drafts/<D>-topic-<slug>.md；跑 validate_topic.py
阶段 5  数字核验（1）：报告里每个关键数字对 SEC filing / 行情接口（价格、市值、PE、净现金、营收、净利、分红回购），PASS/FAIL 表
阶段 6  独立审稿（dalton-reviewer，1）：无据断言、逻辑漏洞、盲点、来源偏差、矛盾处理、引用完整性 → 可发布 / 需修订 / 不得发布
阶段 7  修订（1）：P0 全改、并入阶段 5 的 FAIL（标【更正】）→ 知识层修订草案
```
派发数 9–14。

**workflow 脚本骨架**（结构同 PS.5/PS.6，此处只列关键差异）
```js
export const meta = { name: 'topic-research', description: 'Dalton 专题研究：规划 → 一手 → 分簇取证∥内部记忆∥专家 → 交叉验证与盲区 → 写作 → 数字核验 → 独立审稿 → 修订' }
// args: { topic, slug, horizon, workspace_dir, related_tickers: [...], core_questions?: [...] }
// 1) plan = agent(..., {schema: {clusters:[{name, queries:[...]}], questions:[...]}})  ← 小 schema，簇最多 6 个
// 2) parallel([...plan.clusters.map(c => () => agent(簇取证)), () => agent(内部记忆), () => agent(Guidepoint)])
// 3) 交叉验证与盲区：若返回 gaps.length>0，再 parallel 最多 2 个补充簇
// 4) 写作 → 5) 数字核验 → 6) dalton-reviewer → 7) 修订 + 知识层修订草案
```

**输出模板**（`templates/topic.md`）
```
# {报告标题}
- 研究主题｜日期｜时间范围｜整体置信度（高/中/低，附理由）｜检索覆盖（簇数、全文数、内部库与专家纪要条数）｜版本
## 置信度标注说明
【高】3+ 独立来源｜【中】2 个来源｜【低】单一来源｜【冲突】｜【更正】
## 执行摘要（核心发现，每条带标签与 [Sx]）
## 一、{维度 1}
### 1.1 …（逐条标签 + [Sx]）
## 对覆盖公司的含义
| 公司 | 影响的驱动变量 | 方向与量级 | 相关 debate/支柱 | 置信度 |
## 情景推演
| 情景 | 概率 | 描述 | 关键假设 |
## 风险与触发条件
## 信息缺口与局限
## 数字核验表
| 数据项 | 报告值 | 核验值 | 来源 | 结果 |
## Sources
- [S1] 标题｜媒体/机构｜日期｜链接或文档 id
```
“对覆盖公司的含义”是 Dalton 增补：专题研究必须落到具体公司的驱动变量上，否则不进入知识层。

**知识层写入**（启动 skill 收尾）
- 各相关公司（专题在某公司工作区发起时，由该工作区主智能体执行；跨公司的其余公司按 PS.13 的提案方式）：`knowledge_update` 改 `drivers`（受影响驱动的新认知、敏感度）、`debate`（若改变判断）、`lessons`（可复用的规律，带适用条件和 `scope`）；`knowledge_append_verdict` 每家一条（trigger.kind=`agenda`），`agenda_ops` 关闭发起该专题的问题、新问题入列。
- 基金 wiki：只读，任何阶段都不回写。专题成果只留在工作区知识层和（经 PM 批准后的）`published/`。

**发布** 默认草稿；审稿“可发布”且 PM 要求时走 `publish_report(kind="topic")`。

**与 LangAlpha 自带的关系** 新建。借用自带 `sector-overview` 的行业透镜（`references/sectors/`）和 `impact-analysis` 的“影响传导到具体公司”模板（蓝图 §4.5）；自带 skill 没有多簇取证、置信度标注和独立审稿。

**验证方式（真实运行）** 以 PS.18 的 P5-2 题目跑一次。检查：规划的簇和检索词合理；每个【高】确有 3 个独立来源（`validate_topic.py` 自动查同行引用数）；数字核验表全部 PASS 或已更正；草稿期审稿已执行；`check_draft` 无未匹配数字。

**验收标准** PS.18 的 P5-2 条目。

**风险与注意**
- 专题最容易“题目太大”。启动 skill 的第一步必须锁定范围（主题、时间范围、维度、输出、对比基准），PM 不在场时按议程原文锁定并在稿首写明。
- 规划子智能体给出的簇数受 schema 约束（≤6），让派发数留在平台 64 个子智能体的上限之内。

**预估工时** 3.25 天。

---

## PS.15 `dalton-call`：高 conviction call

**目标**
在证据足够时主动提出可执行的研究观点：预期差三问、三情景 IRR、仓位级别（Core / Tactical / Small）、对冲；证据不够时明确说“暂无高 conviction”并写在等什么（蓝图 §3.1 第 8 条）。正式的 call 必须经 PM 审批（蓝图 §3.1 第 4 条：Dalton 只提研究观点和仓位建议）。

**前置依赖** PS.4（debate）、PS.6（完整覆盖与模型，或至少初次覆盖 + 最近一次财报校准）；PK 的 `publish_report` 和 conviction call 卡片（蓝图 §4.10）。

**来源（只借原理）** bull/bear v4.2 的 §7 Variant perception；deep dive 的 Q8、Q10、Q11 与“差异化观点三问”、三情景估值与回报；LangAlpha `trade-pitch`（可证伪主张表、情景树、表达方式、两种损失上限、监控表、交给 thesis-tracker）。

**新增/改动文件**（新建）
- `plugins/dalton/skills/dalton-call/SKILL.md`
- `plugins/dalton/skills/dalton-call/references/evidence-gate.md`（证据门槛）
- `plugins/dalton/skills/dalton-call/references/sizing.md`（仓位级别定义；具体比例与门槛收益率由 owner 定，见文末待决问题，未定前只输出级别）
- `plugins/dalton/skills/dalton-call/scripts/scenario_irr.py`（三情景 IRR：输入现价、持有期、每情景的目标价或终值 EPS×倍数、期间分红回购收益率、概率；输出每情景 IRR、概率加权 IRR、下行情景最大回撤、对门槛收益率的超额）
- `plugins/dalton/skills/dalton-call/templates/call.md`

**证据门槛**（`evidence-gate.md`；不过门槛就只能输出“暂无高 conviction”）
1. 该公司已完成完整覆盖（PS.6），或者初次覆盖 + 最近一次财报校准（PS.9）都在 120 天内。
2. `data/thesis.json` 里支撑这个 call 的核心支柱，每条至少 2 个独立来源（同源转述算一条），证据质量不低于“中”，且有明确的确认/警告/证伪阈值。
3. 预期差可量化：在至少一个驱动变量上，house case 与一致预期的差异有数字（来自模型 `Street vs House` 页或 bull/bear §4），并能说明错误定价为什么还存在（惯性 / 抱团 / 旧叙事拖累 / 近因偏差 / 覆盖不足）。
4. 有可观察、带时间的验证路径（催化剂）。
5. 三情景的概率加权 IRR 超过门槛收益率（默认 15%，owner 确认见文末），且下行情景可承受（下行幅度与仓位级别匹配）。
6. 最近 30 天没有 `THESIS_BROKEN` 或核心支柱 `impaired` 的事件结论未处理。
任一不满足：输出“暂无高 conviction”，列出缺的是哪一条、补齐需要什么（并用 `agenda_ops` 写进议程）。

**流程（SKILL.md）**
1. 读知识层（thesis、debate、drivers、management、最近 `verdicts.md`）、最新模型、最近的 bull/bear 和财报点评。
2. 过证据门槛；不过就按上面输出并结束。
3. **预期差三问**：市场认为 ___（用驱动变量的单位写，例如“2027 年 capex 2,000 亿美元、折旧拖累 EPS 8%”）；但实际上 ___，因为 ___（机制）；错误定价为什么还存在，什么时候、被什么事件纠正（带日期与置信度）。
4. **可证伪主张** 2–5 条：主张｜今天的证据｜标签（事实/公司表态/一致预期/模型推导/判断/假设）｜若为真的含义｜检验指标｜证伪条件｜何时可知。
5. **三情景**：Bear / Base / Bull 各写驱动与水平、到价格的算术（EPS × 倍数，或 DCF 情景值，引用模型单元格）、概率（三者和为 1）；`scenario_irr.py` 算每情景 IRR、加权 IRR、对门槛的超额。概率给不出来时标“示意性偏斜”，不给加权值（自带 `trade-pitch` 的规则）。
6. **仓位级别**（`sizing.md`）：
   - Core：高 conviction、多季度兑现、核心支柱证据质量高、下行可承受；
   - Tactical：事件驱动、1–2 个季度内由明确催化剂兑现；
   - Small：起始仓位或期权式不对称，证据还在积累。
   具体占比、止损、损失预算由 PM 在 `sizing.md` 里定义；Dalton 只给级别和理由，不给股数或金额。
7. **对冲**（deep dive Q11）：要对冲的风险（宏观、行业、汇率、组合敞口），工具或配对（板块 ETF、同业配对、期权），成本、基差、剩余风险；每个对冲写它失效的情形和“缩小仓位”这个替代方案。
8. **退出与监控**：止损位、重新评估位、时间止损；监控表（触发｜指标｜阈值｜来源｜日期或窗口｜两边各怎么做）；“什么会让我们改变主意”。
9. 写 `drafts/<D>-call-<TICKER>.md`，先 `check_draft`，再 `publish_report(draft_path=..., kind="conviction_call", slug=..., updates_thesis=False)` 提交 PM 审批（卡片显示：方向、级别、预期差一句话、加权 IRR、证伪条件、对冲）。PM 批准后，主张变成 thesis 支柱的证伪阈值，监控行进入 `data/drivers.json` 和 `data/agenda.json`（渲染为 `drivers.md`、`agenda.md`）（同自带 `trade-pitch` 交给 `thesis-tracker` 的做法）。

**输出模板**（`templates/call.md`）
```
# {公司}（{TICKER}）Conviction Call｜{日期}
**建议**：{做多/做空/观察}｜仓位级别 {Core/Tactical/Small}｜入场 {规则}｜目标 {区间}｜退出 {止损/重估/时间}｜就绪度 {decision-grade/review-ready/…}
**预期差一句话**：……
## 一、预期差三问
## 二、可证伪主张
## 三、三情景与 IRR
| 情景 | 驱动与水平 | 到价格的算术 | 从现价回报 | 年化 IRR | 概率 | 出处 |
加权 IRR {x}%；门槛 {h}%；下行情景最大回撤 {d}%
## 四、仓位级别与理由
## 五、对冲
## 六、退出与监控
## 七、证据门槛核对（逐条：满足/不满足 + 证据）
## 引用清单
```
不过门槛时的输出：
```
# {公司}（{TICKER}）：暂无高 conviction｜{日期}
当前倾向：{偏多/偏空/中性}，置信度 {低/中}
缺什么：{证据门槛第 n 条}——{具体缺口}
在等什么：{事件/数据}（{日期或窗口}）；到时重新评估（已写进议程）
```

**知识层写入** 批准后：`knowledge_update(doc="thesis")`（证伪阈值、监控行、`house_view`）、`drivers`；`knowledge_append_verdict`（`NEW_THESIS` 或 `THESIS_STRENGTHENED`），`agenda_ops` 写监控项。未批准或“暂无高 conviction”：`knowledge_append_verdict`（`NO_CHANGE`）+ `agenda_ops` 写等待条件；需要 PM 决定是否升级 conviction 的写 `pm_handoffs`。

**发布** 正式 call 必须走 `publish_report(kind="conviction_call")`；“暂无高 conviction”作为草稿留存，并出现在周会稿里。

**与 LangAlpha 自带的关系** 基于 `trade-pitch` 另起一版（fork）：保留可证伪主张表、情景树与概率规则、表达方式（入场/目标/三种退出）、短仓门槛、对冲失效情形和交给 thesis-tracker；改动：明确使用 IRR（自带刻意不用 IRR 一词，基金要 IRR）、仓位用 Core/Tactical/Small 三级（自带用损失预算的分数）、增加证据门槛和“暂无高 conviction”出口、正式 call 走 PM 审批。

**验证方式（真实运行）** 对四家公司各跑一次 `/dalton-call {TICKER}`（财报校准之后），预期多数输出“暂无高 conviction”，至少检查一家能通过门槛或给出具体缺口；对通过的，走到 PM 审批卡片。`scenario_irr.py` 用手算样例核对（例如现价 100、持有 2 年、三情景 80/130/170、概率 0.25/0.5/0.25、无分红，Base IRR = (130/100)^(1/2) − 1 ≈ 14.0%）。

**验收标准** PS.18 的 P5-3 条目。

**风险与注意**
- 最大的风险是“为了 call 而 call”。证据门槛是硬门槛，不能因为周会需要一条 call 而放宽。
- 仓位级别的比例和止损规则属于 PM 的权限，`sizing.md` 里没有 PM 填写的数字时，只输出级别不输出比例。

**预估工时** 2.5 天。

---

## PS.16 PM 问答行为：`dalton-pm-qa`

**目标**
PM 随时提问（在公司工作区问分析师，或在 Home 问研究总监），回答先基于知识层里的最新观点，必要时再补检索；回答结构固定、判断先行、说明和现有观点的关系（蓝图 §4.2“回答 PM”）。读序沿用 PK.6 的 `dalton-knowledge`，本 skill 只补“回答结构、何时补检索、何时交办、回答后怎么收尾”。

**前置依赖** PK.6（`agent.md` 托管块、`dalton-knowledge` 读序）、PK.14（收尾三步）、PK.19（`dalton_coverage_board`、`dalton_agenda_note`）；P1.1 的入职对话（用户层记忆 `fund_mandate.md`）；P1.3 的 `agent.md` 模板（“工作约定”一节）。

**新增/改动文件**
- `plugins/dalton/skills/dalton-pm-qa/SKILL.md`（新建）
- P1.3 的 `agent.md` 模板“工作约定”一节加一行（改动，与 P1 协调）：`回答 PM 的问题：先读 .agents/skills/dalton-pm-qa/SKILL.md。`
- P1.1 的入职对话脚本加一句（改动，与 P1 协调；记忆没有写接口，只能靠对话写入）：在 `fund_mandate.md` 里写“回答覆盖公司的问题之前，先读 `.agents/skills/dalton-pm-qa/SKILL.md`；观点只在知识层，不在记忆里”。
- `plugins/dalton/skills/dalton-research-director/SKILL.md`（PK.19 新建；本步提供一段“回答 PM”的规则，由 PK 合入）

**回答规则（`dalton-pm-qa`）**
1. **先读知识层**：按 `dalton-knowledge` 的“回答 PM 问题”读序：`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`（最近 14 天）→ `published/INDEX.md` 最新成果 → `agenda.md`（是否已有相关待办或 `pm_handoffs`）。
2. **判断问题类型**：
   - 知识层能直接回答的（“你对 X 的看法”“主要 debate”“管理层指引风格”）：直接回答，引用知识层的版本号和日期（例如“thesis v13，2026-10-19”）。
   - 需要最新信息的（“今天为什么跌”“财报怎么样”“某券商刚上调了”）：先看 `verdicts.md` 今天是否已有结论，有则引用；没有就跑对应 skill（`/dalton-stock-move`、`/dalton-earnings-flash`、`/bullbear` 快速版）或定向检索 `researchdata`。
   - 需要新研究的（新问题、跨公司专题）：先给基于现有认知的初步回答，再提议专项研究（`/topic-research`），PM 同意后执行，或写进议程（`open_questions`，`origin=pm`）。
3. **回答结构**：
   ```
   **直接回答**：立场 + 置信度（一到两句）
   **依据**：知识层里的观点（版本/日期）+ 本次新检索的来源（身份、日期）
   **和现有观点的关系**：不变 / 加强 / 削弱 / 推翻——哪个支柱或 debate；若改变，已用 knowledge_update 更新（或等 PM 确认）
   **不确定性与改判条件**
   **下一步**：是否需要补研究；已排入议程的事项与时间
   ```
4. **收尾**：回答用到了新证据或改变了观点时，按 `dalton-agenda` 收尾三步，`knowledge_append_verdict`（trigger.kind=`pm_question`）；PM 的问题本身作为 `open_questions`（`origin=pm`）写进议程，供研究总监排序。只复述已有观点、没有新证据的，不写结论。
5. **Home（研究总监）**：单家公司的问题，直接读 `../<TICKER>/` 下的知识层作答（Chief of Staff 能读兄弟工作区）；跨公司问题先用 `dalton_coverage_board` 看概览，再逐家读；需要新分析时用 `delegate_to_analyst` 交给该公司分析师，告诉 PM 已交办和预计时间；PM 调整优先级（“这周重点看 AMZN 的 AWS 利润率”）用 `dalton_agenda_note` 追加到该公司议程。不在 Home 做公司级深度研究（Chief of Staff 没有 `equity-analyst` 子智能体，也没有 `knowledge_*` 工具）。
6. **禁止**：知识层有观点时不读就凭检索作答；把一致预期当成我们的判断；只列信息不给立场；在记忆里存观点。

**知识层写入** 见规则第 4 条。

**发布** 不发布。

**与 LangAlpha 自带的关系** 新建行为层。复用 LangAlpha 的 Analyst / Chief of Staff 双角色和 `delegate_to_analyst`；不改角色提示词模板（`components/chief_of_staff.md.j2` 不动），规则放在 skill、`agent.md` 模板和用户记忆里，便于跟上游合并。

**验证方式（真实运行）** PS.18 的 P5-4：PM 在 META 工作区和 Home 各问 5 个标准问题，计时并评分；在工具卡里确认先 Read 了 `thesis.md`/`debate.md`。

**验收标准** 回答引用的是知识层的认知（带版本）；结构符合模板；需要新信息时确实触发了对应 skill 或检索；Home 的问题被正确路由（直接答 / 交办 / 追加议程）。

**风险与注意** 用户记忆能否可靠地让模型先读本 skill，要在验证里观察；不可靠时在 `dalton-conventions` 的 SKILL.md 开头加同样一句（P1 协调）。

**预估工时** 1 天。

---

## PS.17 草稿期审稿与发布时审稿的分工（横切）

**目标**
把 PK.10 的 `dalton-reviewer` 一致地接到各 workflow 的草稿阶段，并和 `publish_report` 内的服务端审稿分清职责：草稿期审稿帮助修订，发布时的服务端审稿才是正式结论；审稿缺席时草稿照常可读，但不能升级为正式版本（蓝图 §3.1 第 7 条）。

**接入点**

| 交付物 | 草稿期审稿（workflow 内，`agentType: 'dalton-reviewer'`） | 发布时（`publish_report` 服务端审稿） |
|---|---|---|
| `bullbear-doc` | 合并后；重点：每个 Debate 的“我的判断”、§2 是否混入价格走势、§4 能否回指快照 | PM 要求发布时 |
| `initial-coverage` | 成稿后；重点：重点题有判断、预期差不是复述共识、估值分解算对 | PM 要求发布时 |
| `full-coverage` | memo 第三段后，与 check-model 并行 | 必经（P3 完成标准） |
| `earnings-deep-update` | 校准后；审稿缺席时启动 skill 只写 `NO_CHANGE` 以外的结论为“待审”说明，不改核心支柱状态 | PM 要求发布时 |
| `topic-research` | 写作与数字核验后 | PM 要求发布时（不回写 wiki） |
| `dalton-call` | 可选 | 必经（正式 call） |

**规则**
1. 草稿期审稿人的回复文本是唯一采信的产物；审稿前后对成稿做 `sha256` 比对，不一致判审稿无效（子智能体没有只读开关，PK D5）。
2. 草稿期审稿的 P0 必须在发布前处理完；`publish_report` 的服务端审稿给 `revise` 时，按其 `must_fix` 修改后重新调用（新的调用会建新请求，PK.11）。
3. 草稿期审稿人模型在 PK.10 的 agent 文件里写成 manifest 键（如 `ocb-openai-gpt-6.1-sol`；lead 切换到其他家族时按 P0.6 换成不同家族的键，`role_policy.py` 会对同家族告警）；发布时的服务端审稿人由 PK 的 `reviewer_models` 列表按 `role_policy.family_of()` 选第一个与作者不同家族、且可用的模型。审稿是纯文本调用，可以用 text-only 的 CLI gateway 模型。本文所有 workflow 不覆盖它。

**验证方式** 在 PS.6 的流程里观察：草稿期审稿意见存在且被处理；服务端审稿 `review_unavailable` 时（例如临时停用审稿模型）草稿仍在 `drafts/` 可读、发布被拒。

**预估工时** 并入 PS.1、PS.6 与 PK。

---

## PS.18 skill 质量评估：PM 可评分的验收样本

**目标**
给 lead 和 PM 一套可以直接拿来验收的样本：每个 P2–P5 交付物指定公司或场景、产出必须包含什么、按什么维度打分、怎样算通过。这一节就是验收依据。

**评分维度**（基金的 STRL 正式定义尚未提供，本文先用下表 M/D/E/C/F/S 六维替代；owner 提供原定义后按原定义替换，见待决问题）

| 代号 | 维度 | 问 PM 的问题 | 0 分 | 1 分 | 2 分（可用） | 3 分（优秀） |
|---|---|---|---|---|---|---|
| M | 业务机制 | 读完知道这家公司怎么赚钱、钱从哪来、什么在变吗？ | 缺失或错误 | 复述公司描述 | 驱动树清楚，增量来源清楚 | 讲出了 PM 不知道的机制或单位经济 |
| D | 分歧 | debate 是真分歧吗？我们站在哪边？ | 没有 debate | 罗列观点，无立场 | 分歧变量清楚，有立场与置信度 | 指出了市场没意识到的分歧点 |
| E | 预期差 | 和市场定价的差异在哪、有多大、为什么存在？ | 没有 | 定性一句 | 至少一个驱动变量上量化了差异，说明了错误定价的原因 | 预期差可交易，验证路径清楚 |
| C | 驱动链 | 变量 → 财务 → 估值 的链条和敏感度清楚吗？ | 没有 | 只有变量名 | 有敏感度和领先指标 | 链条能直接用于跟踪和建模 |
| F | 证伪条件 | 什么情况下我们错了？能观察吗？ | 没有 | 泛泛风险 | 可观察、带阈值或时间 | 每个核心判断都有证伪条件和检查日期 |
| S | 来源 | 数字可回指吗？来源层级和去重对吗？ | 有编造数字 | 多处无来源 | 抽查全对，缺口有标注 | 来源丰富、层级恰当、同源去重 |

**通用通过规则**
- 硬门槛（任何交付物）：抽查 5 个数字全部对得上原件或工具结果；无编造的一致预期、目标价；S ≥ 2。
- “可用”：该交付物适用的维度全部 ≥ 2。
- “优秀”：适用维度平均 ≥ 2.5，且至少一个维度为 3。
- 盲评做法：同一家公司、同一题目，把 Dalton 产物与一份旧 Dalton 或人工研究稿去掉来源标记后一起给 PM，PM 不知道哪份是谁写的，按上表打分。没有对照稿的交付物（快讯、周会）只做单份评分。
- 评分记录：`docs/dalton/dev-guide/trial-runs/<样本编号>-<公司>-<日期>.md`，按 PS.0 的试跑记录模板，附评分表和 PM 的文字意见。PM 意见里的问题要变成 skill 的修改（改 references 或模板），并在下一次试跑记录里写“上次问题是否解决”。

**样本清单**

| 编号 | 交付物 | 样本 | 必须包含 | 适用维度 | 额外自动检查 |
|---|---|---|---|---|---|
| P2-1 | `initial-coverage` | owner 指定的一家未覆盖公司（建议 DASH；备选 AFRM，6 月财年，可顺带检验财季标签） | 12 问初筛中 Q1–Q5、Q7、Q8、Q12 有判断；行业是否周期及依据；壁垒“形成还是被破坏”及可证伪指标；EPS vs 倍数历史分解表；当前 debate（≥2 个，带立场）；预期差或“暂无高 conviction”；初始 thesis 3–5 条支柱带阈值；研究议程 | M D E C F S | 分解表两项之和 = 股价对数变化；知识层文件与报告一致；`verdicts.md` 有“新论点”（`NEW_THESIS`） |
| P2-2 | `dalton-company-background` | 同 P2-1 | 9 段齐全；3 个财年 + 4 个季度；看点和风险绑定业务变量 | M S | 数字回指 filing 表名和期间 |
| P2-3 | `dalton-financial-lenses` | P2-1 公司 + META | 净现金双口径逐项；隐含资产折前折后；回报评分拆解 | S（+M） | `test_return_score.py` 通过；锚点（市值、汇率、报告期）三把镜头一致 |
| B1 | `/bullbear` 快速版 | META | 结论（修正方向、财报风险、置信度）；2–4 个 debate；修正三层与隐含费用敏感度；催化剂按时间；改判条件；来源说明 | D E C F S | 一致预期数字与快照一致；回答在一次对话内完成并记录时长 |
| B2 | `bullbear-doc` | P2-1 公司 | v4.2 全部节；每个 Debate 五块齐全；“我的判断”有立场和改判条件；股东回报评分 | D E C F S | `bb_check.py` 0 WARN；草稿期审稿已执行 |
| P3-1 | `full-coverage` memo | 与 P2-1 同一家公司 | 12 问逐题（含来源行与详见章节）；C4 三年指引可信度表；Pre-mortem；三情景；我们与卖方逐驱动对比；对初次覆盖认知的修正说明 | M D E C F S | `validate_memo.py` 0 FAIL；`publish_report` 核验无来源数字为 0；PM 审批通过 |
| P3-2 | `full-coverage` 模型 | 同上 | 三表 + DCF + comps + Facts / Guidance vs Actual / Street vs House | C S | `recalc.py`、`audit.py --strict` 通过；Checks 全 OK；memo 第六、九章数字与模型一致（抽 5 个） |
| P4-1 | 财报日历 | 全覆盖名单，重点 META、MSFT、GOOGL、AMZN | 官方来源；三态；前瞻/快讯自动任务；对账；总结两项固定项 | S | 自动任务 `next_run_at` 与 T-3 交易日一致；改期演练无重复任务 |
| P4-2 | 财报前瞻 | 四家中本季第一个发布的（预计 10 月下旬，以官方为准），由自动任务触发 | 真实门槛五分；高频数据；按机制聚类的 debate；三情景与历史反应；`predictions.json` | D E C F S | 自动触发（非手动）；无截止时间之后的信息 |
| P4-3 | 财报快讯 | 同 P4-2 | 头条数字含上一季同比；指引对上次指引；Q&A 来自 Q&A；卖方初评；事件五词初判 | E C S | 触发时间 = 发布 + N 小时；14 项数据质量门 |
| P4-4 | 深度链 | 同 P4-2 | 八节点评；模型新版本与 Change Log；前瞻逐条对账（含错因）；thesis 支柱信号与状态；管理层指引记录新行 | M D E C F S | 模型 v2 未覆盖 v1；对账覆盖 `predictions.json` 全部条目 |
| P4-5 | `dalton-stock-move` | 验收周内四家中单日涨跌最大的一天 + 一个 Watch 名字 | 同组与 ETF 对比、z 值、量比；filing/新闻/卖方/社媒；对 thesis 的含义；事件五词 | C F S | 价格触发自动运行至少一次；beta 判定的不推送 |
| P4-6 | thesis 增量 | 四家，连续 5 个交易日 | 每晚每家一条结论；重要变化的增量笔记 | D C S | “无事”夜晚花费；PM 抽查 3 个 `no` 判断无漏报 |
| P4-7 | 公告监控 | 四家中下一份 10-Q | 风险因素 diff（或“无变化”）；MD&A 措辞变化；对 thesis 的影响 | M C S | 发现延迟 ≤ 轮询间隔；同一 accession 只处理一次 |
| P5-1 | `weekly-coverage-review` | 2026-10-26 至 10-30 一周（财报周；若时间不合，取验收时最近一个完整周） | 本周判断；观点变化表；Core 逐家（五源）；Watch 命中按事件分组；EPS vs 倍数归因表；至少一条 call 或“暂无高 conviction” | D E C F S | 归因闭合误差 < 0.1pp；Core 全部完成或缺口写明 |
| P5-2 | `topic-research` | “超大规模云厂商 2027 年 capex 指引与折旧对 META/MSFT/GOOGL/AMZN 的 EPS 影响” | 逐条置信度；矛盾保留原话；对覆盖公司的含义表；数字核验表；信息缺口 | M E C F S | `validate_topic.py` 通过；草稿期审稿已执行；相关公司知识层已更新、wiki 无写入 |
| P5-3 | `dalton-call` | 四家各一次（财报校准后） | 证据门槛逐条核对；通过者：预期差三问、三情景 IRR、仓位级别、对冲、退出与监控；未通过者：缺什么、在等什么 | D E F S | `scenario_irr.py` 与手算一致；正式 call 走到 PM 审批卡片 |
| P5-4 | PM 问答 | META 工作区 5 问 + Home 5 问（见下） | 按回答结构；引用知识层版本；需要时触发 skill | D E F S | 回答时长；Home 问题路由正确 |
| P5-5 | 自主运行一周 | 不派任务的一周 | 每家覆盖公司至少一次由事件或议程触发的研究，有事件五词和下一项验证；日花费不超额度 | — | 来自 `verdicts.md` 与预算账本的统计（蓝图 §4.7 判断标准） |

P5-4 的标准问题（PM 可替换）：
- 公司工作区（META）：①你对 META 的看法和主要 debate？②上次财报后我们的观点变了什么？③2027 年费用指引的市场预期是多少，我们怎么看？④管理层给指引的风格？⑤现在最可能让我们错的是什么？
- Home：①四家里哪家的预期差最大？②这周有哪些观点变化？③AMZN 今天为什么动？（应路由到 stock-move 或引用已有结论）④帮我研究一下 AI 广告对 GOOGL 搜索的影响（应提议专题并交办）⑤下周有哪些财报和需要准备的？

**验收节奏** P2 结束：P2-1、P2-2、P2-3、B1、B2。P3 结束：P3-1、P3-2。P4 结束：P4-1 至 P4-7。P5 结束：P5-1 至 P5-5。每个阶段 lead 按本表验收，PM 盲评至少覆盖 P2-1、P3-1、P5-1。

**预估工时** 每阶段 0.5 天（整理样本、组织盲评、汇总评分），不含 PM 阅读时间。

---

## 附：待决问题（只剩 owner 需要拍板的 6 项）

1. **日预算金额**：P0.8 的日美元额度（Asia/Shanghai 时区，与旧 Dalton 分开计）由 owner 定；本文所有自动任务只受这一个美元额度约束，不设次数限制。
2. **STRL 的正式定义**：PS.18 暂用 M/D/E/C/F/S 六维替代；请提供 STRL 原定义和分档，评分表照原定义替换。
3. **P2 样本公司**：建议 DASH，备选 AFRM（6 月财年，可顺带检验财季标签）。
4. **仓位级别比例与门槛收益率**：`dalton-call` 的 Core/Tactical/Small 具体比例、损失预算由 owner 写进 `sizing.md`；门槛收益率默认 15%，请确认。
5. **周会日**：默认周六 10:00（Asia/Shanghai，财报日历刷新之后）；若周会在别的日子开，只改 `dalton-director-weekly.json` 的 cron。
6. **净现金口径确认**：PS.2 已采用 `findata-netcash` SKILL.md 的版本（受限资金计入基础口径、严格口径扣不自由现金、租赁不参与计算只列示、强制“大存大贷”检查）；请确认。

**已定事项（v0.2 的其他问题，不再讨论）**：财报日期只认官方来源、每周六刷新（PS.7 与 PK.19 一致）；Home 为公司工作区建自动任务走 `PUT /api/v1/dalton/automations/{name}`；分析师对自己的 `dalton-*` 自动任务有常设授权、派单自动批准已开；每晚增量用 PK.16 的后端 `consensus` 探针；`move-up` / `move-down` / `delta` / `earnings-calendar` 文件名由 PK 预检识别；bull/bear 成稿用 kind `bullbear` 发布；合规口径已定（专家访谈、管理层交流、Guidepoint 可用，标身份）；基金 wiki 只读；本阶段只做美股，港股/A 股到 P6。自动任务预加载方式仍由 PS.0 实测二选一，这是实施细节，不需要 owner 决定。
