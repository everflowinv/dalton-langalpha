---
name: dalton-agenda
description: Closes every research session with verdict recording, agenda updates, and optional verification scheduling.
---

# dalton-agenda — 研究议程与收尾闭环技能（PK.14）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.

## 1. 定位与触发条件

每一次研究会话（财报预览/快评/深评、股价异动、Filing 审阅、Thesis Delta、初次/深度覆盖、PM 问答使用了新证据）在完成正文与结构化知识修正后，**必须**执行本技能定义的“收尾三步（Closing 3 Steps）”，确保“每一次判断留痕、每一个待验证问题入账、每一个明确日期的验证节点自动唤醒”。

本技能直接操作当前公司工作空间根目录下的知识账本（`agenda.md` / `data/agenda.json`、`verdicts.md` / `data/verdicts.jsonl`、`thesis.md`、`debate.md`）与自动化配置目录 `.agents/user/automations/`。

---

## 2. 收尾三步（Closing 3 Steps）

### 第一步：写入判断日志（`knowledge_append_verdict`）

每次形成或复核判断后，调用 `knowledge_append_verdict` 追加一条不可篡改的判断记录（落盘至 `data/verdicts.jsonl` 并重渲染 `verdicts.md`）：

- **`verdict`**（必填，严格五选一）：
  - `NO_CHANGE`：本次事件/数据符合预期区间，核心多空逻辑与置信度不变。
  - `THESIS_STRENGTHENED`：核心支柱得到超预期硬数据验证，上调置信度或盈利预期。
  - `THESIS_WEAKENED`：支柱出现边际恶化或未达买方 bogey，但尚未触发 kill condition。
  - `THESIS_BROKEN`：触发 `thesis.md` 中的证伪条件（kill condition）或核心支柱坍塌。
  - `NEW_THESIS`：首次覆盖立项或彻底重构投资框架。
- **`summary`**（必填）：一句话结论 + 核心定量依据（含期间与单位）。
- **`trigger`**（必填）：触发本次研究的来源，包含：
  - `kind`：严格取枚举值 `filing` | `earnings` | `price_move` | `indicator` | `due_validation` | `pm_question` | `internal_material` | `agenda` | `director` | `manual`
  - `ref`：对应财报期间、Filing 编号、异动日期或任务 ID。
- **`next_validation`**（必填）：下一个最重要验证节点（如 `"2026-08-15 FY26Q2 财报核验云分部 ARR 增速是否守住 28%"`）。
- **`progress`**（必填）：本次回答了哪些旧问题、新增了哪些待验证问题（如 `"closed: [q_margin_q1]; opened: [q_capex_fy27]"`）。

### 第二步：更新研究议程（`agenda_ops`）

读取当前工作空间的 `agenda.md`（或机器真值 `data/agenda.json`），在调用 `knowledge_append_verdict` 的 `agenda_ops`（或 `knowledge_update(doc="agenda", ...)`）中维护待验证问题队列（`open_questions`）、高频跟踪指标（`watch`）与历史归档（`closed_questions`）：

1. **`close_question`**：若本次研究已拿到一手证据回答了某条存量问题，传入 `id` 与 `resolution`（结论摘要 + 证据引用 + 解决日期），将其移入 `closed_questions`。
2. **`add_question` / `update_question`**：将本次研究新发现的争议点或下一步验证点写入 `open_questions`：
   - `id`：稳定短标识（如 `q_hbm3e_yield_26q3`）
   - `question`：具体、可证伪的问题表述
   - `linked_pillar` / `linked_debate`：关联的 `thesis.md` 支柱或 `debate.md` 编号
   - `priority`：`P0`（决定仓位/生死）| `P1`（影响目标价/斜率）| `P2`（长期跟踪）
   - `due_date`：预计可获知答案的日期（`YYYY-MM-DD`）或事件节点
   - `verify_how`：用什么一手数据源/指标验证（如 `"10-Q Note 12 RPO 披露"`）
3. **`set_watch`**：更新 `watch.filings`、`watch.events`（如确认后的下次财报日）、`watch.indicators` 或 `watch.price_triggers`。
4. **队列容量纪律**：`open_questions` 保持精简高优（活跃问题一般不超过 7 条，P0 不超过 3 条）；过期或失去相关性问题应及时 `close_question` 并注明原因。

### 第三步：按需调度一次性验证唤醒（`once` Automation）

若第二步新增或更新的 `P0` / `P1` 问题具有**明确的未来日历验证时点**（例如：某监管终裁日、竞争对手财报日、管理层承诺的指引更新日、行业数据发布日）：

1. **常设授权（Standing Owner Authorization）**：在公司工作空间内，分析师代理拥有直接写入一次性验证自动化的常设授权，直接调用 `Write` 工具写入 `.agents/user/automations/dalton-<t>-v<YYYYMMDD>-<slug>.json`（其中 `<t>` 为小写 ticker，`<YYYYMMDD>` 为触发日期，`<slug>` 为简短英文标识）。若在 Home 空间由研究总监跨公司调度，则调用 `dalton_schedule_automation`。
2. **Automation JSON 规范**（不要写 `max_failures`；`dalton-*` 自动任务不会因连续失败而停用，系统会按时间自动退避）：
   ```json
   {
     "name": "NVDA FY27Q2 Blackwell 毛利率验证",
     "description": "Verify NVDA Q2 FY27 Blackwell gross margin recovery against 75% bogey",
     "status": "active",
     "trigger_type": "once",
     "next_run_at": "2026-08-28T08:00:00-04:00",
     "timezone": "America/New_York",
     "instruction": "执行议程问题 q_blackwell_gm 验证：核查 Q2 10-Q / 业绩公告中的 Blackwell 毛利率是否达到 75% 门槛，并按 .agents/skills/dalton-agenda/SKILL.md 收尾三步更新 thesis 与 agenda。",
     "workspace_id": "<WORKSPACE_ID>",
     "thread": "new"
   }
   ```
   *(注：字段须与 `docs/dalton/dev-guide/automation-templates/` 模板保持一致，只使用平台支持的合法字段)*。

---

## 3. 自检清单

- [ ] `knowledge_append_verdict` 已调用，`verdict` 为五词枚举之一，`trigger.kind` 合法。
- [ ] 已解决的旧问题已通过 `agenda_ops(op="close_question")` 归档，未留在 `open_questions` 中。
- [ ] 新提出的 `next_validation` 已同步体现在 `agenda.md` 的 `open_questions` 中。
- [ ] 凡有明确验证日期的 P0 验证点，均已生成合法的 `once` automation JSON。
