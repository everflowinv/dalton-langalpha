---
name: dalton-thesis-delta
description: "采集覆盖公司最新一致预期变化、新公告、内部研究库与卖方增量，对照现有知识层判断是否构成实质性变化，仅在有重要变化时写增量笔记并更新知识层。用于覆盖公司的日常增量跟踪或检查近期基本面有无边际变化；无实质变化时快速短路完成，有变化时需几分钟。"
---

# Dalton 每日 Thesis 增量跟踪（`dalton-thesis-delta`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

## 目标与唤醒机制

每日对覆盖公司收一次“增量包”（一致预期快照与修正、六个内部研究来源 `company-wiki` / `guidepoint` / `funda-ai` / `sales-notes` / `tmtb` / `alphaengine`、新 SEC filing、新闻与 X），判断相对现有知识层是否存在**重要变化（material change）**：
- **零成本预检与 `<dalton_wake>`**：盘中 `watch` 自动任务不再查内部研究库（只查 `sec`、`cnhk`、`agenda`、`earnings` 四个需要及时发现的探针）；六个内部研究来源全部由每日的 `dalton-thesis-delta`（`collect.py`）统一扫一次。预检另有计划唤醒探针 `schedule` 与价格异动探针 `price`；无任何信号的轮次记 `skip`、花费为 0、不建线程。每日 cron（`dalton-<t>-delta.json`，北京时间 11:00；Core 周二至周六，Watch 周六）唤醒后线程首条上下文包含 `<dalton_wake>`（内含信号 `id`、`kind` 及详情）。
- **无重要变化不打扰 PM**：被唤醒后，若所有来源均取数成功（`ok: true`）且经判定不属于重要变化（`material=no`），只记一条 `NO_CHANGE` 结论并调用 `commit --material no` 更新 `seen.json`，**绝不向 PM 推送消息**。

---

## 四步执行流程

### 第 1 步：读取 `<dalton_wake>` 并运行 `collect.py` 补齐增量包

1. 解析线程首条 `<dalton_wake>` 中的唤醒信号（记录信号 `id`、探针名 `probe` 与信号 `key`——唤醒块每行形如 `[P1][agenda] 摘要 (key=validation:V-001:2026-10-28, signal_id=…)`，后续将信号 `id` 写入 `verdict.trigger.ref`，并将探针名按下方对照表映射为 `verdict.schema.json` 允许的 `verdict.trigger.kind` 枚举值）：

| `<dalton_wake>` 探针名（`probe`）与信号子类型 | `verdict.trigger.kind`（必填枚举值） |
|---|---|
| `agenda`，key 以 `validation:` 开头（到期验证项） | `due_validation` |
| `agenda`，key 以 `pm_question:` 开头（PM 问题） | `pm_question` |
| `agenda`，key 以 `catalyst:`、`question:`、`action:` 开头（催化剂、开放问题、行动项） | `agenda` |
| `schedule`（定时唤醒） | `agenda` |
| `sec` | `filing` |
| `cnhk` | `filing` |
| `earnings` | `earnings` |
| `internal` | `internal_material` |
| `price` | `price_move` |
| 手动触发（无 `<dalton_wake>`） | `manual` |

2. 运行采集脚本生成完整增量包（同时把本轮一致预期快照写入工作区 `data/consensus/<YYYY-MM-DD_HHMM>.json`）：

```bash
python3 .agents/skills/dalton-thesis-delta/scripts/collect.py collect \
  --ticker {TICKER} \
  --workspace-dir .
```

   脚本默认经沙箱内 PTC 包装模块（`tools.researchdata`，与 `ExecuteCode` 调 researchdata 同一模块）取数；`DALTON_RESEARCHDATA_URL` 只作 HTTP 桥覆盖。若返回 `status=researchdata_transport_unconfigured`，是配置错误（两条调用路都不可用），本轮未采集任何来源：不要重试采集，按“所有来源取数失败”如实汇报并停下，严禁走短路分支或写成“本轮无新文档”。

3. **快速短路分支（不读任何文档全文）**：
   - 检查 `collect.py` 返回的汇总 JSON 中的 `internal_status`、`failed_sources`、`all_sources_ok` 与 `short_circuit_eligible`：
   - **任何来源 `ok: false` 都不能当成“没有新文档”**：当某个来源 `ok: false`（`all_sources_ok == false` 或 `failed_sources` 非空）时，**严禁走“所有来源 0 新增 → 短路结束”的分支**；在本轮结论（`verdict` 与 `commit --summary`）和汇报里必须如实写明“`<来源> 本轮取数失败（原因）`”，**绝不能写成“本轮无新文档”或“无新增”**。
   - **短路判断要求所有来源都 `ok: true` 而且都是 `0` 新增**：仅当 `all_sources_ok == true`（所有来源 `ok: true`，`failed_sources` 为空）、`auto_flags` 为空，且新内部文档（`counts.internal` + `counts.alphaengine`）与新公告（`counts.sec`）均为 `0`（即 `short_circuit_eligible == true`）时：
     1. 直接执行提交：
        ```bash
        python3 .agents/skills/dalton-thesis-delta/scripts/collect.py commit \
          --workspace-dir . \
          --run-id {RUN_ID} \
          --material no \
          --summary "无新增"
        ```
     2. 调用 `knowledge_append_verdict` 写入一条 `NO_CHANGE`（`progress="none"`，`trigger.kind` 按上方对照表将 `<dalton_wake>` 探针名映射为合法枚举值，`trigger.ref` 填信号 id，`next_validation` 沿用当前最近待验证项）；
     3. **立即结束本轮任务，不读取任何文档全文，也不推送 PM**。

### 第 2 步：对照知识层与 `materiality.md` 判断是否重要

当第 1 步未短路时：
1. 按 `.agents/skills/dalton-knowledge/SKILL.md` 读序先读本工作区 `agent.md` 托管块、`thesis.md`、`debate.md`、`drivers.md`、`agenda.md` 及最近 `verdicts.md`。
2. 读取 `research/tracker/<run_id>/packet.md` 及关联的新文档摘段/原文（若存在 `ok: false` 的来源，同步核实 `packet.md` / `internal_status` 中的失败原因并在本轮结论与汇报中如实写明“`<来源> 本轮取数失败（原因）`”）：
   - 内部库新文档（`company-wiki`、`guidepoint`、`funda-ai`、`sales-notes`、`tmtb`）：先读摘段，必要时通过 PTC `researchdata.research(..., operation="read", document=...)` 读全文（**Guidepoint 例外：只有 `search_library` 问答检索，没有全文读取**——`read` / `get_document` 均不存在，不要调用；检索到的问答正文已在增量包该访谈条目的 `qa` 字段里，且检索到的问答不等于完整访谈）；
   - AlphaEngine 新研报：先看摘要，挑带新数字（盈利预测修正、capex/费用、目标价/评级变化、核心 KPI 调查）的研报调用 `researchdata.research(route="alphaengine", operation="get_document", params={"doc_id": ...})` 读全文（每轮最多 3 篇；遇配额或未登录即停并记录状态）；
   - SEC / 港股与 A 股新公告：若当日 `dalton-filing-review` 或 `dalton-earnings-flash` 已经在 `verdicts.md` 写过同一份公告的结论，直接引用，不重复拆解；否则美股读取对应 SEC Item 或附件，港股与 A 股调用 `researchdata.finance(route="cn-hk", operation="filings", ticker=CODE_5_OR_6, start=..., end=..., limit=20)` 读取巨潮法定公告原文（港股未覆盖时用 `web_search` 查披露易 `site:hkexnews.hk` 原文链接），并可调用 `researchdata.finance(route="cn-hk", operation="ask", query=..., limit=20)` 辅助核查资金流、两融、回购或股东变动（平台财务表仅作初值，核心数字回公告核实）。
3. 严格按 `.agents/skills/dalton-thesis-delta/references/materiality.md` 的十条标准判定本轮是否为**重要变化（`material=yes`）**：
   - **重要（任一命中即 `material=yes`）**：
     1. 自动提示命中下一财年 EPS、下一财年/下一季收入或目标价均值变化阈值，且经核查非单纯口径噪音；
     2. 主要券商评级变化、目标价变动 ≥10%，或下一财年 EPS / 费用 / capex 预测发生实质调整，且不在已有观点池中；
     3. 公司事实变化：业绩、指引、8-K 重大事项、融资（股权/可转债/大额发债）、管理层新表态、金额重大的法律或监管事项或时间表变化；
     4. 某条 `debate` 的当前倾向或置信度发生改变，或浮现新分歧；
     5. 某个核心 KPI 越过 `data/thesis.json`（`thesis.md`）中的确认、警告或证伪阈值；
     6. 买方持仓、拥挤度或 whisper 发生明显变化（来自 `sales-notes` 或 `tmtb`）；
     7. 催化剂新增、改期或取消；
     8. 两个互不相关的专业 X 账号提出同一个新基本面论据，或 X 上的新数据点被第二个独立来源证实；
     9. 价格自动提示命中（单日 ≥4% 或 5 日 ≥8%）并找到基本面/事件原因（若价格触发任务已跑过 `/dalton-stock-move`，直接引用其结论，不重复归因）；
     10. 财报 8-K（Item 2.02）首次出现总是重要（若 `/dalton-earnings-flash` 已跑，引用其结论并聚焦卖方修正）。
   - **不重要（`material=no`）**：已有观点的重复转述、单条孤立 X 帖、泛泛的常规财报前瞻、小券商微调目标价且无新论据。

### 第 3 步：分支处理（更新知识层 / 写增量笔记 / 决定是否推送）

#### 分支 A：存在重要变化（`material=yes`）
1. **撰写增量笔记**：按 `.agents/skills/dalton-thesis-delta/templates/delta.md` 写入 `research/<YYYY-MM-DD>-delta.md`（日期统一用**美东业务日**——本轮运行开始时的美东日历日，与 `dalton_check_done` 口径一致；自动唤醒常在美东前一天晚间启动，此时不要用 UTC/北京日期命名）（写清变化是什么、一手与卖方证据、影响哪个 thesis 支柱或 debate、事件五词结论、下一项验证）。
2. **更新知识层**：调用 `knowledge_update` 更新受影响的文档（`thesis` / `debate` / `drivers` / `management`）。
3. **追加事件结论**：调用 `knowledge_append_verdict` 写入五词结论（`THESIS_STRENGTHENED` / `THESIS_WEAKENED` / `THESIS_BROKEN` / `NEW_THESIS` 或有实质新证据但维持原判的 `NO_CHANGE` + `progress="some"`（彻底解答问题时填 `"resolved"`）；`trigger.kind` 按第 1 步对照表与变化主要来源填 `consensus_change`、`internal_material`、`filing`、`earnings`、`indicator`、`price_move`、`due_validation`、`pm_question`、`agenda` 或 `manual`，`trigger.ref` 填 `<dalton_wake>` 信号 id）。
4. **回复摘要**：最后一条回复写不超过 10 行的精炼中文摘要（delta 任务不发私信推送，PM 在对话里看到它；判断先行：五词结论 + 变化核心事实与带期间数字 + 受影响的支柱/Debate + 下一项验证）。

#### 分支 B：无重要变化（`material=no`）
1. **只记结论**：调用 `knowledge_append_verdict` 写入一条 `NO_CHANGE`（`progress="none"`，`trigger.kind` 按第 1 步对照表映射，`trigger.ref` 填唤醒信号 id）。
2. **不写增量笔记，不推送 PM**。

### 第 4 步：提交状态（`commit`）与关闭到期验证项

1. **必须执行 `commit`**（无论 `material=yes` 还是 `no`），把本轮所有新文档、公告和 X 帖子 id 记入 `research/tracker/seen.json`，更新 `last_run.json` 与 `updates.log`：

```bash
python3 .agents/skills/dalton-thesis-delta/scripts/collect.py commit \
  --workspace-dir . \
  --run-id {RUN_ID} \
  --material {yes|no} \
  --summary "{一句话结论摘要}"
```

> **强约束**：`seen.json` 只在 `commit` 成功时更新；若 `collect` 或中途分析失败，绝不能提前把未审阅的文档标为已见，确保下一轮自动重取。

2. **议程验证项闭环**：检查 `agenda.md` 中已到期或本轮已获解答的验证项（`V-...`），在第 3 步同一次 `knowledge_append_verdict` 调用的 `agenda_ops` 参数中将其关闭（`{"op":"set","path":"/next_validations/V-xxx/status","value":"done"}`），并把新的待验证事项入列。注意：verdict 的 `next_validation` 只写下一项要看什么，不会关闭任何验证项。

### 收尾门禁：汇报完成前必须过完成契约（`dalton_check_done`）

- 第 4 步 `commit` 成功后（无论 `material=yes` 还是 `no`，含第 1 步的快速短路分支），结束本轮前调用 `dalton_check_done(workflow="dalton-thesis-delta", params={"ticker": ..., "as_of": "<本轮美东业务日 YYYY-MM-DD，与增量笔记文件名一致>"})`；若笔记文件名日期与 `as_of` 不同（如跨了美东零点），再加 `"delta_note": "<本轮实际写入的笔记路径>"` 显式指给契约。
- 契约按 `research/tracker/last_run.json` 判定：它须为本轮 `commit` 写入；`material=yes` 时还要求 `research/<本轮日期>-delta.md` 增量笔记（按本轮运行开始时间核对新近度；也接受本轮实际写入的同目录 `*-delta.md` 或 params 里显式给的 `delta_note` 路径），以及 `thesis`/`debate`/`drivers`/`management` 至少一份本轮 `knowledge_update` 新版本；`material=no` 时没有增量笔记是合法结果。两种情况都要求本轮 verdict 已写。
- `done=false` 时按 `missing` 先补齐再查（`commit` 未落盘就重跑 `commit`、增量笔记缺失就补写、verdict 未写就 `knowledge_append_verdict`、知识层缺本轮更新就 `knowledge_update`）；补不了的缺项只能汇报“未完成”并列出缺项与原因，不得汇报“完成”。
