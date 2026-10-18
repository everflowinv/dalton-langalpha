---
name: dalton-thesis-delta
description: "每晚覆盖公司 thesis 增量跟踪：由后端预检（consensus/sec/internal/price 探针）通过 <dalton_wake> 唤醒后，运行 collect.py 生成一致预期快照与增量包，按 materiality.md 判断是否存在重要变化；有重要变化才写增量笔记、更新知识层并推送 PM，无重要变化只记 NO_CHANGE 与 seen.json 不打扰 PM。触发词：thesis delta、每晚增量、日常跟踪、/dalton-thesis-delta。"
---

# Dalton 每晚 Thesis 增量跟踪（`dalton-thesis-delta`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

## 目标与唤醒机制

每晚对覆盖公司收一次“增量包”（一致预期快照与修正、新卖方研报与 sales note、TMTB、FUNDA AI、新入库 wiki、新 SEC filing、新闻与 X），判断相对现有知识层是否存在**重要变化（material change）**：
- **零成本预检与 `<dalton_wake>`**：每晚 cron（`dalton-<t>-delta.json`）先由后端运行 `consensus`、`sec`、`internal`、`price` 四个零 LLM 探针。无任何信号的夜晚记 `PRECHECK_SKIPPED`、花费为 0、不建线程（后端仍把当晚一致预期快照存档到 `data/consensus/`）。只有至少命中一个探针信号时才唤醒智能体，线程首条上下文包含 `<dalton_wake>`（内含信号 `id`、`kind` 及命中的阈值）。
- **无重要变化不打扰 PM**：被唤醒后，若经判定不属于重要变化（`material=no`），只记一条 `NO_CHANGE` 结论并调用 `commit --material no` 更新 `seen.json`，**绝不向 PM 推送消息**。

---

## 四步执行流程

### 第 1 步：读取 `<dalton_wake>` 并运行 `collect.py` 补齐增量包

1. 解析线程首条 `<dalton_wake>` 中的唤醒信号（记录 `signal_id`、`kind` 与命中详情，后续写入 `verdict.trigger.ref` 与 `verdict.trigger.kind`；手动调用时 `trigger.kind="manual"`）。
2. 运行采集脚本生成完整增量包（同时把本轮一致预期快照写入工作区 `data/consensus/<YYYY-MM-DD_HHMM>.json`）：

```bash
python3 .agents/skills/dalton-thesis-delta/scripts/collect.py collect \
  --ticker {TICKER} \
  --workspace-dir .
```

3. **快速短路分支（不读任何文档全文）**：
   - 检查 `collect.py` 返回的汇总 JSON：若 `auto_flags` 为空，且新内部文档（`counts.internal` + `counts.alphaengine`）与新公告（`counts.sec`）均为 `0`（即预检信号在完整增量包复核中不成立）：
     1. 直接执行提交：
        ```bash
        python3 .agents/skills/dalton-thesis-delta/scripts/collect.py commit \
          --workspace-dir . \
          --run-id {RUN_ID} \
          --material no \
          --summary "无新增"
        ```
     2. 调用 `knowledge_append_verdict` 写入一条 `NO_CHANGE`（`progress="none"`，`trigger.kind` 按 `<dalton_wake>` 唤醒信号类型，`trigger.ref` 填信号 id，`next_validation` 沿用当前最近待验证项）；
     3. **立即结束本轮任务，不读取任何文档全文，也不推送 PM**。

### 第 2 步：对照知识层与 `materiality.md` 判断是否重要

当第 1 步未短路时：
1. 按 `.agents/skills/dalton-knowledge/SKILL.md` 读序先读本工作区 `agent.md` 托管块、`thesis.md`、`debate.md`、`drivers.md`、`agenda.md` 及最近 `verdicts.md`。
2. 读取 `research/tracker/<run_id>/packet.md` 及关联的新文档摘段/原文：
   - 内部库新文档（`sales-notes`、`tmtb`、`funda-ai`、`company-wiki`）：先读摘段，必要时通过 PTC `researchdata.research(..., operation="read", document=...)` 读全文；
   - AlphaEngine 新研报：先看摘要，挑带新数字（盈利预测修正、capex/费用、目标价/评级变化、核心 KPI 调查）的研报调 `get_document` 读全文（每轮最多 3 篇；遇配额或未登录即停并记录状态）；
   - SEC 新公告：若当日 `dalton-filing-review` 或 `dalton-earnings-flash` 已经在 `verdicts.md` 写过同一份公告的结论，直接引用，不重复拆解；否则读取对应 Item 或附件。
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
1. **撰写增量笔记**：按 `.agents/skills/dalton-thesis-delta/templates/delta.md` 写入 `research/<YYYY-MM-DD>-delta.md`（写清变化是什么、一手与卖方证据、影响哪个 thesis 支柱或 debate、事件五词结论、下一项验证）。
2. **更新知识层**：调用 `knowledge_update` 更新受影响的文档（`thesis` / `debate` / `drivers` / `management`）。
3. **追加事件结论**：调用 `knowledge_append_verdict` 写入五词结论（`THESIS_STRENGTHENED` / `THESIS_WEAKENED` / `THESIS_BROKEN` / `NEW_THESIS` 或有实质新证据但维持原判的 `NO_CHANGE` + `progress="material"`；`trigger.kind` 按变化主要来源填 `internal_material`、`filing`、`indicator`、`price_move` 等，`trigger.ref` 填 `<dalton_wake>` 信号 id）。
4. **推送 PM**：输出不超过 10 行的精炼中文摘要推送给 PM（判断先行：五词结论 + 变化核心事实与带期间数字 + 受影响的支柱/Debate + 下一项验证）。

#### 分支 B：无重要变化（`material=no`）
1. **只记结论**：调用 `knowledge_append_verdict` 写入一条 `NO_CHANGE`（`progress="none"`，`trigger.kind` 与 `trigger.ref` 对应唤醒信号）。
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

2. **议程验证项闭环**：检查 `agenda.md` 中已到期或本轮已获解答的验证项（`V-...`），在第 3 步同一次 `knowledge_append_verdict` 调用的 `agenda_ops` 参数中将其关闭（`close_validation`），并把新的待验证事项入列。
