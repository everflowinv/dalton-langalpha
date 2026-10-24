---
name: dalton-agenda
description: "完成每次研究会话的收尾三步：追加事件五词判断日志、更新待验证问题与跟踪议程，并为明确日期的验证节点配置自动唤醒任务。任何研究任务形成结论或更新认知后收尾时使用；直接调用工具，快速完成。"
---

# dalton-agenda — 研究议程与收尾闭环技能（PK.14）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.

## 1. 定位与触发条件

每一次研究会话（财报预览/快评/深评、股价异动、Filing 审阅、Thesis Delta、初次/深度覆盖、PM 问答使用了新证据）在完成正文与结构化知识修正后，**必须**执行本技能定义的“收尾三步（Closing 3 Steps）”，确保“每一次判断留痕、每一个待验证问题入账、每一个明确日期的验证节点自动唤醒”。

本技能直接操作当前公司工作空间根目录下的知识账本（`agenda.md` / `data/agenda.json`、`verdicts.md`（数据库 `dalton_verdicts` 渲染生成）、`thesis.md`、`debate.md`）与自动化配置目录 `.agents/user/automations/`。

---

## 2. 收尾三步（Closing 3 Steps）

### 第一步：写入判断日志（`knowledge_append_verdict`）

每次形成或复核判断后，调用 `knowledge_append_verdict` 追加一条不可篡改的判断记录（写入数据库 `dalton_verdicts` 表并重渲染 `verdicts.md`）：

- **`verdict`**（必填，严格五选一）：
  - `NO_CHANGE`：本次事件/数据符合预期区间，核心多空逻辑与置信度不变。
  - `THESIS_STRENGTHENED`：核心支柱得到超预期硬数据验证，上调置信度或盈利预期。
  - `THESIS_WEAKENED`：支柱出现边际恶化或未达买方 bogey，但尚未触发 kill condition。
  - `THESIS_BROKEN`：触发 `thesis.md` 中的证伪条件（kill condition）或核心支柱坍塌。
  - `NEW_THESIS`：首次覆盖立项或彻底重构投资框架。
- **`event`**（必填）：本次客观事件或披露的一句话概述（含期间与核心数字）。
- **`rationale`**（必填）：为什么给出该五词判断的核心定量与逻辑依据（含期间与单位）。
- **`trigger`**（必填）：触发本次研究的来源对象 `{kind, ref?, automation_execution_id?}`：
  - `kind`：严格取枚举值 `filing` | `earnings` | `price_move` | `consensus_change` | `indicator` | `due_validation` | `pm_question` | `internal_material` | `agenda` | `director` | `manual`
  - `ref`（可选）：对应财报期间、Filing accession、信号 ID、验证项 ID（如 `"V-001"`）或任务 ID。
  - `automation_execution_id`（可选）：自动任务执行记录 ID。
- **`next_validation`**（必填）：下一个最重要验证节点对象 `{what, due, validation_id?}`：
  - `what`（必填）：具体验证内容与量化门槛（如 `"FY26Q2 财报核验云分部 ARR 增速是否守住 28%"`）
  - `due`（必填）：预计验证日期，在 `verdict` 中只能写 `YYYY-MM-DD` 字符串，**不能为 `null`**（注：`agenda.json` 的 `/next_validations` 中 `due` 允许为 `null` 或 `YYYY-MM-DD`，那是 `agenda` 的契约，与 `verdict.next_validation.due` 不是一回事）
  - `validation_id`（可选）：关联 `agenda.json` 中 `/next_validations` 的 `V-xxx` 编号（如 `"V-002"`）
- **`progress`**（必填）：严格取枚举值 `"none"` | `"some"` | `"resolved"`（无实质进展填 `"none"`，取得阶段性/实质性新证据填 `"some"`，彻底解答问题填 `"resolved"`）。
- **可选关联字段**：`affected`（`{pillars: ["P1"], debates: ["D1"], drivers: ["K1"]}`）、`evidence_ids`（如 `["E-20261023-01"]`）、`question_ids`（如 `["Q-001"]`，服务端会自动向这些问题的 `attempts` 追加一条记录）、`house_view_changed`（布尔值）、`at`（ISO 时间戳，调用工具时服务端也会自动生成）。

**`verdict` 完整合规示例（符合 `verdict.schema.json`）**：

```json
{
  "at": "2026-10-23T21:00:00Z",
  "trigger": {
    "kind": "earnings",
    "ref": "V-001"
  },
  "event": "META 3Q26 广告收入同比增长 21% 至 405 亿美元，高于买方 bogey 19%",
  "verdict": "THESIS_STRENGTHENED",
  "affected": {
    "pillars": ["P1"],
    "debates": ["D1"],
    "drivers": ["K1"]
  },
  "rationale": "Reels 与 AI 推荐模型带动广告展示量同比增长 8%、平均单价同比增长 11%，验证核心广告变现效率持续超预期。",
  "evidence_ids": ["E-20261023-01"],
  "house_view_changed": false,
  "next_validation": {
    "what": "4Q26 财报核验假日季广告收入增速是否维持 18% 以上及 2027 年 Capex 指引区间",
    "due": "2027-01-29",
    "validation_id": "V-002"
  },
  "question_ids": ["Q-001"],
  "progress": "resolved"
}
```

### 第二步：更新研究议程（`agenda_ops`）

读取当前工作空间的 `agenda.md`（或机器真值 `data/agenda.json`），在调用 `knowledge_append_verdict` 的 `agenda_ops`（或 `knowledge_update(doc="agenda", ...)`）中维护待验证问题队列（`/open_questions`）、验证节点队列（`/next_validations`）与高频监控配置（`/watch`）。
注意：`ops` 只支持 `create`、`set`、`append`、`append_item`、`insert`、`upsert_item`、`retire`、`retract`、`remove`；`agenda.schema.json` 开启了 `additionalProperties: false`，若 `agenda_ops` 校验或应用失败，服务端会在返回结果中标明 `agenda_update: failed` 并附上期望结构，此时必须按提示调用 `knowledge_update(doc="agenda", ...)` 重试修复。

1. **关闭/解决存量问题（`set` 操作修改 `/open_questions/<id>`）**：
   若本次研究已拿到一手证据回答了某条存量问题（如 `Q-001`），用两条 `set` 操作将其 `status` 设为 `"resolved"`（或放弃时设为 `"dropped"`）并写入 `resolution`（注意：不要使用不存在的 `close_question` 或 `/closed_questions`）：
   - `{"op": "set", "path": "/open_questions/Q-001/status", "value": "resolved"}`
   - `{"op": "set", "path": "/open_questions/Q-001/resolution", "value": "3Q26 10-Q 确认广告单价同比增长 11%（E-20261023-01），问题已解答"}`
2. **新增或更新待验证问题（`append_item` / `set` 操作 `/open_questions`）**：
   用 `append_item` 向 `/open_questions` 追加新问题，严格使用 `agenda.schema.json` 定义的字段（服务端会自动补齐缺失的 `id`、`created_at`、`owner`、`attempts`）：
   - `question`（必填）：具体、可证伪的问题表述
   - `why`（必填）：为什么该问题对 thesis / 估值关键
   - `value`（必填）：`"high"` | `"medium"` | `"low"`
   - `urgency`（必填）：`"now"` | `"this_week"` | `"this_quarter"` | `"someday"`
   - `origin`（必填）：`"analyst"` | `"pm"` | `"director"` | `"event"`
   - `owner`（必填，默认 `"analyst"`）：`"analyst"` | `"pm"`
   - `status`（必填）：`"open"` | `"waiting_pm"` | `"resolved"` | `"dropped"`
   - `linked`（可选）：`{"pillars": ["P1"], "debates": ["D1"], "drivers": ["K1"], "evidence_ids": ["E-20261023-01"]}`
3. **关闭旧验证项与新增下一项验证（`/next_validations`）**：
   - 关闭已完成的验证项：`{"op": "set", "path": "/next_validations/V-001/status", "value": "done"}`
   - 新增下一项验证：`{"op": "append_item", "path": "/next_validations", "value": {"what": "4Q26 财报核验 Capex 指引", "due": "2027-01-29", "expected": "FY27 Capex 指引不超过 800 亿美元", "status": "pending", "source": "analyst"}}`
4. **更新高频跟踪配置（`/watch`）**：
   使用 `set` 或 `append` 更新 `agenda.schema.json` 支持的 `/watch/events`、`/watch/indicators`、`/watch/price`（`{abs_move_pct_1d, excess_vs_benchmark_pct, benchmark}`）、`/watch/filings`（`{sources, forms}`）或 `/watch/internal`。
5. **队列容量纪律**：`open_questions` 保持精简高优（`status="open"` 的活跃问题一般不超过 7 条，`value="high"` 且 `urgency="now"` 的问题不超过 3 条）；过期或失去相关性的问题应及时将 `/open_questions/<id>/status` 设为 `"resolved"` 或 `"dropped"` 并填写 `resolution`。

**`agenda_ops` 完整合规示例（符合 `ops.py` 与 `agenda.schema.json`）**：

```json
[
  {
    "op": "set",
    "path": "/open_questions/Q-001/status",
    "value": "resolved"
  },
  {
    "op": "set",
    "path": "/open_questions/Q-001/resolution",
    "value": "3Q26 财报确认 AI 推荐驱动广告单价同比提升 11%（E-20261023-01）"
  },
  {
    "op": "append_item",
    "path": "/open_questions",
    "value": {
      "question": "2027 年 AI 基础设施 Capex 扩张是否会导致自由现金流利润率跌破 25%？",
      "why": "决定 P2 资本回报率支柱与 DCF 远期 FCF 假设",
      "value": "high",
      "urgency": "this_quarter",
      "origin": "event",
      "owner": "analyst",
      "status": "open",
      "linked": {
        "pillars": ["P1"],
        "debates": ["D1"],
        "drivers": ["K1"],
        "evidence_ids": ["E-20261023-01"]
      }
    }
  },
  {
    "op": "set",
    "path": "/next_validations/V-001/status",
    "value": "done"
  },
  {
    "op": "append_item",
    "path": "/next_validations",
    "value": {
      "what": "4Q26 财报核验假日季广告收入增速是否维持 18% 以上及 2027 年 Capex 指引区间",
      "due": "2027-01-29",
      "expected": "4Q26 广告收入同比增速 >=18%，2027 Capex 指引 <=800 亿美元",
      "status": "pending",
      "source": "analyst",
      "linked": {
        "pillars": ["P1"],
        "debates": ["D1"]
      }
    }
  },
  {
    "op": "set",
    "path": "/watch/price",
    "value": {
      "abs_move_pct_1d": 4.0,
      "excess_vs_benchmark_pct": 3.0,
      "benchmark": "QQQ"
    }
  }
]
```

### 第三步：按需调度一次性验证唤醒（`once` Automation）

若第二步新增或更新的高优问题（`value="high"`）具有**明确的未来日历验证时点**（例如：某监管终裁日、竞争对手财报日、管理层承诺的指引更新日、行业数据发布日）：

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
     "instruction": "执行议程问题 Q-002 验证：核查 Q2 10-Q / 业绩公告中的 Blackwell 毛利率是否达到 75% 门槛，并按 .agents/skills/dalton-agenda/SKILL.md 收尾三步更新 thesis 与 agenda。",
     "workspace_id": "<WORKSPACE_ID>",
     "thread": "new"
   }
   ```
   *(注：仅使用上述平台支持的合法字段：`name`、`description`、`status`、`trigger_type`、`next_run_at`/`cron_expression`、`timezone`、`instruction`、`workspace_id`、`thread`)*。

---

## 3. 自检清单

- [ ] `knowledge_append_verdict` 已调用，`verdict` 为五词枚举之一，`event`、`rationale`、`next_validation`（`{what, due}`）、`progress`（`none|some|resolved`）及 `trigger.kind` 均合法，且返回结果无 `agenda_update: failed`。
- [ ] 已解决的旧问题已通过 `agenda_ops` 的 `{"op": "set", "path": "/open_questions/<id>/status", "value": "resolved"}` 及 `resolution` 标记解决。
- [ ] 新提出的验证节点已通过 `append_item` 同步体现在 `agenda.md` 的 `/next_validations` 或 `/open_questions` 中。
- [ ] 凡有明确验证日期的高优验证点，均已生成合法的 `once` automation JSON。
