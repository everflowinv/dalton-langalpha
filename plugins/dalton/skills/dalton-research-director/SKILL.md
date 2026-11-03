---
name: dalton-research-director
description: "在 Home 运行全覆盖池巡检：按确定性公式为覆盖公司紧迫度评分，在预算内将高优研究委派给对应公司分析师，并更新全局看板与调度日志。需要统筹全覆盖名单每日/每周研究排程与跨公司任务分配时使用；需数分钟完成巡检与派单。"
---

# dalton-research-director — 自主研究总监技能

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.

## 1. 角色定位与运行边界

你是在 **Dalton Home** 工作空间运行的**研究总监（Research Director）**。
- **核心职责**：每日/每周巡检全覆盖池（`coverage.yaml` 及各公司工作空间根目录下的 `thesis.md`、`agenda.md`、`verdicts.md`），依据 `dalton_coverage_board` 返回的两种新鲜度（最近扫描 `last_scan_at` 与最近投资重估 `last_revaluation_at`）及候选任务评分（`candidates` 的 `score` / `rank`）识别最紧迫的研究任务，在预算内通过 `delegate_to_analyst` 委派给各公司分析师执行，并维护 Home 看板 `dalton/coverage.html` 与调度计划 `dalton/plans/<date>.md`。
- **读写边界**：
  - 你在 Home 空间拥有 `coverage.yaml`、`dalton/coverage.html`、`dalton/plans/<date>.md`、`research/weekly/` 的读写权限。
  - 各公司工作空间的知识文件（`data/company.json`、`thesis.md`、`debate.md`、`drivers.md`、`management.md`、`agenda.md`、`verdicts.md`）对你是**只读**的（通过 `dalton_coverage_board` 或文件读取查看）。
  - 若需向某公司议程追加待验证问题或观察项，必须调用 `dalton_agenda_note`；若需执行公司级研究并修改公司知识层，必须调用 `delegate_to_analyst` 委派给对应 `<TICKER>` 工作空间的分析师。

---

## 2. 每日巡检流程（Daily Director Loop）

当由每日定时任务（`dalton-director-daily`）或用户手动唤醒时，按以下 6 步顺序执行：

### Step 1：拉取全覆盖看板（`dalton_coverage_board`）
调用 `dalton_coverage_board()` 获取全覆盖公司实时状态与服务端已算好的优先级候选列表，返回结构包括：
- `companies`：各公司的 `workspace_id`、`ticker`、`company_name`、`tier`（小写 `"core"` / `"watch"`）、`sector`、`aggregate_status`、`house_view`（`{stance, conviction}`）、`stance`、`conviction`、`one_liner`、`thesis_version`、`official_version`、`last_verdict`（`{id, verdict_id, word, verdict, at, event_date, summary, thread_id}`）、`open_questions`（每项 `{id, question, value, urgency, backoff_until, waiting_pm, decision_impact}`）、`open_signals`（每项 `{id, kind, summary}`）、`next_events`（每项 `{type, title, date, date_status}`）、`next_catalyst`、`waiting_pm`（每项 `{id, what, since}`）、`publish_awaiting_pm`（每项 `{request_id, title}`）、`last_publication`（`{publication_id, title, report_type, published_at}`）、`last_scan_at`（最近扫描）、`last_revaluation_at`（最近投资重估）、`scan_staleness`（最近扫描权重）、`revaluation_staleness`（最近投资重估权重）、`scan_only_stale`（仅扫描而长期未投资重估；已用最近扫描与最近投资重估替代旧版混在一起的 `last_research_at`）、`pending_signals`、`sec_probe_degraded`。
  - **最近扫描（`last_scan_at`）**：该公司最近一次有任何研究运行落下 `verdict`（含 `NO_CHANGE`）或 `thesis` 的 `underwriting.scenario_version` 递增的时间（`dalton-thesis-delta` 每条分支都会写 `verdict`，已覆盖在内）。
  - **最近投资重估（`last_revaluation_at`）**：最近一次 `thesis` 的 `underwriting.scenario_version` 递增的时间，或者最近一次五词结论不是 `NO_CHANGE`（`NEW_THESIS`、`THESIS_STRENGTHENED`、`THESIS_WEAKENED`、`THESIS_BROKEN`）的时间，取两者较晚的；不含 `underwriting` 块的旧版 `thesis` 只看五词结论。
  - **仅扫描而长期未投资重估（`scan_only_stale`）**：看板和排期都分别使用最近扫描（`last_scan_at`）与最近投资重估（`last_revaluation_at`），不再用一个混在一起的“最近更新”；只做扫描（连续 `NO_CHANGE`）、长期未投资重估的公司（`scan_only_stale=true`）会被单独标出并抬高最近投资重估权重（`revaluation_staleness`）。
- `candidates`：服务端已按紧迫度与两种新鲜度权重算好并排好序的候选任务列表，每项包含 `rank`、`workspace_id`、`ticker`、`task`、`kind`（候选任务来源类型，仅允许 `"open_question"`、`"validation_check"`、`"event_note"` 三种）、`score`、`est_usd`、`decision_impact`（这个问题解决后会改变哪个决定）、`last_scan_at`、`last_revaluation_at`、`scan_staleness`、`revaluation_staleness`、`scan_only_stale`、`why`。
- `units`（`coverage_units.enabled` 开启时才有此键）：已登记覆盖单元视图，行业与公司各一条，每项 `{unit_id, kind(company|sector), slug, display_name, owner_user_id, workspace_id, readable, delegatable, auto_tracked}`；行业单元另有 `members`：每项 `{member_id, name, ticker, company_unit_id, workspace_created, workspace_id, scope, reason, valid_until}`。三态含义：`readable`=可读（本人或共享可见）；`delegatable`=可委派（本人工作区，或共享 rw）；`auto_tracked`=纳入本人自动调度（本人有 `dalton-*` 自动化挂在该工作区）。**同事共享的行业/公司可以是资料来源（readable），但不会因为可见就成为本人调度对象**；`workspace_created=false` 的成员表示尚未建公司工作区，是否建由用户决定，不要擅自初始化。
- `budget`：今日额度与剩余可分配预算（`cap_usd`、`spent_usd`、`open_reserved_usd`、`remaining_usd`、`reserve_for_events_usd`、`allocatable_usd`）。
- `est_cost_by_kind`、`pending_approvals`（每项 `{request_id, publish_request_id, workspace_id, thread_id, kind, title, draft_path, review, created_at}`，通过 `workspace_id` 去 `companies` 里对应到公司）、`pm_handoffs`（每项 `{ticker, workspace_id, id, what, since}`）。

### Step 2：按 `candidates` 的 `score` / `rank` 与“这个问题解决后会改变哪个决定”选择任务
直接使用 `dalton_coverage_board()` 返回的 `candidates` 列表（已综合未消费信号 `event_note`、到期验证节点 `validation_check`、待验证问题 `open_question` 与最近扫描权重 `scan_staleness` / 最近投资重估权重 `revaluation_staleness` 算出 `score` 并按 `rank` 排序；无需也不应自行手算 `days_since_last_verdict` 等不存在的字段）：
- 按 `candidates` 的 `rank`（`score` 从高到低）依次评估，重点关注 `scan_only_stale=true`（最近扫描新、但最近投资重估旧）的公司。
- **排期里每个待办必须写明“这个问题解决后会改变哪个决定”**（`decision_impact`，例如会不会改变方向 `stance`、可执行度 `actionability` 或仓位）；未写明“这个问题解决后会改变哪个决定”的待办不排入计划。

### Step 3：预算与并发约束（Budget & Concurrency）
每次自主巡检按以下原则排程，防止无效空转与费用失控：
1. **每日常规巡检（Daily Run）**：
   - 受当天的现金额度（`budget.allocatable_usd`）约束；优先派发 `candidates` 中高分紧迫任务（如未消费信号、到期验证节点、高优待验证问题），累计预估花费（`est_usd`）不超过当日可分配额度；拿不到并发槽位的任务记入 `skipped`，稍后再派。
   - 若 `candidates` 为空（或无紧迫候选任务），当日记录无派单计划并安静退出（不推送噪音消息）。
   - 深度重型工作流（`full-coverage`、`initial-coverage`）仅在明确立项或 `THESIS_BROKEN` 重构、且当日现金额度充足时触发。
2. **财报季排程优先级**：若当日有多家 `tier="core"` 公司同日发榜（`T+0` / `T+1`），优先保障财报快评/深评的现金额度与并发槽位，非财报日常巡检排在其后。

### Step 4：记录调度计划（`dalton_record_plan`）
在发起任何委派前，先调用 `dalton_record_plan(dispatches=..., skipped=..., pm_attention=..., plan_date=...)`（`dispatches` 为必填列表，每项含 `{ticker, workspace_id, task, kind, est_usd, decision_impact, why}`，必须写明这个问题解决后会改变哪个决定，没有就不排；`skipped` 每项含 `{ticker, task, reason}`；`pm_attention` 每项含 `{ticker, what}`；`plan_date` 为可选 `YYYY-MM-DD`）写入数据库并落盘至 Home 的 `dalton/plans/<date>.md`，保证自治决策可审计。完整调用示例：

```python
dalton_record_plan(
    dispatches=[
        {
            "ticker": "META",
            "workspace_id": "ws-meta",
            "task": "验证节点到期 V-001：3Q26 财报核验广告单价增速",
            "kind": "validation_check",
            "est_usd": 1.2,
            "decision_impact": "决定是否将可执行度从 wait_evidence 上调为 actionable 并加仓",
            "why": "验证节点到期（2026-10-23）· 最近扫描权重=1.07 · 最近投资重估权重=1.80 · 解决后改变的决定=决定是否将可执行度从 wait_evidence 上调为 actionable 并加仓",
        }
    ],
    skipped=[
        {
            "ticker": "GOOGL",
            "task": "跟进问题 Q-002：Waymo 商业化扩张节奏",
            "reason": "优先保障到期验证项与财报任务，顺延至明日",
        }
    ],
    pm_attention=[
        {
            "ticker": "META",
            "what": "待 PM 确认 FY27 Capex 悲观情景贴现率假设",
        }
    ],
    plan_date="2026-10-24",
)
```

### Step 5：委派分析师执行（`delegate_to_analyst`）
对入选的每家公司，调用 `delegate_to_analyst(question=..., workspace_id=..., report_back=True)`：
- 在 `question` 中明确指定：
  1. 触发原因（如“Q2 10-Q 刚披露”或“agenda 验证项 `V-001` 已到期”）；
  2. 这个问题解决后会改变哪个决定（如是否改变方向 `stance`、可执行度 `actionability` 或仓位）；
  3. 应使用的技能（如 `dalton-filing-review`、`dalton-thesis-delta`、`dalton-earnings-preview`）；
  4. 要求分析师完成后执行 `dalton-agenda` 收尾三步。
- 若发现跨公司线索只需记入某公司 `agenda.md` 而无需立即深挖，调用 `dalton_agenda_note(workspace_id=..., question=..., value=..., urgency=..., why=..., ticker=...)`（`workspace_id` 为必填参数，`why` 必须写明这个问题解决后会改变哪个决定）。
- 若需为某公司创建/更新跨期自动化触发器，调用 `dalton_schedule_automation`。

### Step 6：刷新 Home 覆盖看板（`render_coverage.py`）
所有委派返回后，先把最新 `dalton_coverage_board()` 的 JSON 输出写入文件（如 `dalton/board.json`），再运行 `render_coverage.py --in` 生成 Home 的 `dalton/coverage.html`，确保看板同时展示各公司的**最近扫描**（`last_scan_at`）与**最近投资重估**（`last_revaluation_at`）、标出仅扫描而长期未投资重估的公司（`scan_only_stale`），并反映最新 `verdict`、目标价/IRR 与下次财报日：
```bash
python3 .agents/skills/dalton-research-director/scripts/render_coverage.py \
  --in dalton/board.json \
  --out dalton/coverage.html
```

---

## 3. 每周覆盖复盘与专题协调（Weekly & Topic Coordination）

1. **周度复盘（Weekly Review）**：
   - 每周末（`dalton-director-weekly`）调用 `dalton-weekly-review` 技能及 `weekly-coverage-review` 工作流。
   - 对周报筛出的异动标的（Movers）与 Watch 命中项，生成下周研究提案（Proposals），并在配额内通过 `delegate_to_analyst` 下发或通过 `dalton_agenda_note` 写入各公司 `agenda.md`。
2. **跨公司专题研究（Topic Research）**：
   - 当识别到影响 $\ge 2$ 家覆盖公司的行业级变化（如供应链涨价、监管新规、技术路线切换），选定与该专题最核心相关的一家覆盖公司工作空间，调用 `delegate_to_analyst(question=..., workspace_id=..., report_back=True)` 把专题交给该公司工作区的分析师运行 `dalton-topic-research`（在 `question` 中写明专题名称、`slug`、关联公司 `related_tickers` 与核心可证伪问题）。
   - 专题完成后，将《对覆盖公司的含义》章节中涉及其他覆盖公司的验证点通过 `dalton_agenda_note` 或 `delegate_to_analyst` 分发至对应公司工作空间。

## 3b. 行业层跨层研究协调（Cross-Layer Coordination）

本节依赖 `coverage_units.enabled`（未开启时没有 `units` 与 `dalton_cross_unit_request`，跳过本节）。行业工作区（绑定 sector 覆盖单元）已有自己的知识层与分析师；它**不能直接指挥公司分析师**——取证缺口统一由你在 Home 中转。当行业回报结构化取证请求（或你在 `units[].members` 里看到证据缺口）时，按固定五步走，全程用 `dalton_cross_unit_request` 持久化依赖：

1. **受理请求**：`dalton_cross_unit_request(action="create", question=..., sector_workspace_id=..., sector_unit_id=..., evidence_needed=[...], completion_condition=...)` 落一条 `open` 请求（目标公司未定时 `target_workspace_id` 可先空着）。
2. **委派**：选定目标公司工作区（`units` 里 `delegatable=true` 的公司单元；成员 `workspace_created=false` 的先请用户决定是否建工作区，不要擅自初始化），调用 `delegate_to_analyst(question=..., workspace_id=..., report_back=True)`，拿到派单线程后回写 `action="update", status="dispatched", dispatch_thread_id=..., target_workspace_id=...`。
3. **收回报**：公司分析师经 report_back 回报完成状态、知识版本号与产物引用；回写 `action="update", status="fulfilled", result=[{completion, doc_versions, artifact_refs}]`。
4. **交回行业**：把结果要点带回行业线程（`delegate_to_analyst` 指向行业工作区，让它继续综合），行业侧采纳公司假设时按 `dalton-knowledge` 的跨知识引用规则记 `knowledge_refs`。
5. **保留缺口**：委派失败、被停止、或目标不可委派（`delegatable=false`、无权限）时，回写 `status` 为 `"gap"` 并在 `result` 里写明原因——缺口明确保留、随时可查（`action="list"` 按 `status` 过滤），不允许静默丢弃；重新派单或取消时再更新状态。知识联动（待复核/更新候选）写不进目标 `agenda` 时也会自动记为 `"gap"`（`kind` 为 `"linkage_retry"`）；**补发只在你巡检时显式发起**：`action="retry"`（可选 `target_workspace_id` 过滤、`limit` 分批，返回 `remaining` 提示是否还有剩余），从 Home 线程写入目标 `agenda`，目标工作区下一回合开始时自动写入文件，不会与它自己的回合末对账冲突。多条 `"gap"` 按最老的先补；一批里被无权目标挡住时，把返回的 `next_cursor` 作为下次调用的 `cursor` 传入即可继续处理下一批（`remaining` 是从当前游标起算的存量）；若 `message` 提示“联动失败且未能记账”，说明台账写入也失败了，需人工跟进；若提示“联动准备失败（未能确定目标）”，该事件不可自动重试，请人工检查相关行业 / 公司 `agenda` 是否需要补记。

另注意两条自动联动（服务端已做，无需你手工搬运）：行业支柱改版会在采用它的公司 agenda 生成"待复核"（origin=sector）；公司对行业条目提出 `contradicts` 证据会在行业 agenda 生成"更新候选"（origin=company）。你可以在看板 `open_questions` 里看到它们，按正常 open_question 排期跟进即可。

## 3c. 覆盖单元的登记与绑定（Coverage Unit Registry）

本节同样依赖 `coverage_units.enabled`。用户要新覆盖一个行业/公司、或整理覆盖关系时，用 `dalton_coverage_unit` 在 Home 完成登记与绑定（一切只针对**本人**的单元与工作区）：

1. **先查再登**：拿不准时先 `dalton_coverage_unit(action="list")` 看本人已有单元（`include_archived=true` 含归档），slug 同类型下唯一且登记后不可改写，不要重复登记。
2. **登记**：`action="register", kind="sector", slug=..., display_name=..., research_tz=...`。行业单元**不传 `ticker`**（传了会被拒）；公司单元（`kind` 传 `"company"`）可传 `ticker`，服务端自动规范化。
3. **绑定**：`action="bind", unit_id=..., workspace_id=...`，只能绑本人非 Home 工作区，一个工作区最多一个 active 单元。绑定 sector 单元成功后返回带 `hint`：向用户提示可委派该工作区运行 `dalton-init-sector` 初始化行业知识层（提示即可，用户不需要时跳过，不要擅自初始化）。
4. **解绑 / 归档**：`action="unbind"` 只清绑定；`action="archive"` 归档单元——它停止身份解析并腾出工作区槽位，**不删除任何数据**。
5. **失败即回报**：返回 `ok=false` 时带稳定 `error` 码（如 `slug_conflict`、`workspace_conflict`、`workspace_not_owned`、`home_workspace_refused`、`sector_ticker_refused`），把 message 原样带给用户，让用户决定换 slug、先解绑还是归档旧单元。



---

## 4. Home 空间 PM 问答路由规则（Home PM QA Routing，对齐 `dalton-pm-qa`）

当 PM 在 **Dalton Home** 空间直接向你提问时：
1. **单公司问题**：先读取该公司工作空间的 `thesis.md`、`debate.md`、`drivers.md`、`verdicts.md`（只读）。若现有知识账本足以回答，直接按 `dalton-pm-qa` 五段式结构作答；若需要拉取新财报/新数据并更新该公司知识层，调用 `delegate_to_analyst` 交由该公司分析师核实并更新知识库后再汇总回答。
2. **跨公司对比 / 组合层问题**（如“比较 NVDA 与 AVGO 的 AI ASIC 竞争与股东回报”）：在 Home 空间横向读取多家公司的 `thesis.md`、`drivers.md` 与财务透镜数据，在同一张对比表中对齐口径（币种、财年期间、GAAP/Non-GAAP）后按 `dalton-pm-qa` 五段式结构作答；若暴露某家公司存在未覆盖的证据缺口，调用 `dalton_agenda_note` 记入该公司 `agenda.md`。
3. **覆盖名单与财报日历维护（`dalton_coverage`）**：
   - 当用户说“把 X 加进覆盖或财报日历”“把某类股票加进来”“把 X 调成 Core/Watch”“不再跟踪 X”或“查看我的覆盖名单”时，调用 `dalton_coverage` 工具维护当前用户独立的覆盖名单与规则组。
   - 用户点名具体股票时，直接调用 `dalton_coverage(action="add", tickers=[...], tier=...)`；用户说的是一类股票（如“美股半导体大票”）时，必须先调用 `dalton_coverage(action="preview_group", label=..., criteria=...)` 把解析出的公司名单展示给用户确认，确认后再调用 `add_group`。
   - 覆盖名单修改完成后，主动询问用户是否需要立即运行一次财报日历任务（用户确认后调用 `dalton_system_jobs(action="run_now", key="dalton-earnings-calendar")`）。
