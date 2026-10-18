---
name: dalton-pm-qa
description: "处理基金经理（PM）日常互动问答：先读知识层（thesis/debate/drivers/verdicts/published/agenda），按问题类型分流（直接回答、补查实时信息、或提议新研究），以固定五段结构（直接回答、依据、和现有观点的关系、不确定性与改判条件、下一步）输出，并在引入新证据或改变判断时执行收盘三步更新 verdicts 与 agenda（origin='pm'）。触发词：PM 提问、回答 PM、观点问答、怎么看、为什么我们认为、/pm-qa、/dalton-pm-qa。"
---

# PM 日常互动问答（PM Q&A）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

## 1. 第一步：先读知识层（Knowledge Layer First）

回答 PM 任何关于覆盖公司或行业的问题前，**必须先读知识层**，确保回答与本基金已沉淀的 House View 前后一致：
1. `agent.md` 受管区块（当前 thesis 状态与速览）
2. `thesis.md`（核心论点、支柱状态与三档阈值）
3. `debate.md`（核心多空焦点与我们站在哪一边）
4. 按问题需要读取：`drivers.md`（关键经营驱动变量与 House vs Street 对比）、`management.md`（管理层画像与指引记录）、`verdicts.md`（近 14 天事件结论）
5. `published/INDEX.md`（近期已发布的深度报告与模型位置）与 `agenda.md`（当前开放问题与验证计划）

---

## 2. 第二步：按问题类型分流（3-Way Triage）

### 类型 (a)：现有知识层可直接回答
- **场景**：询问我们对某公司的核心逻辑、某条支柱的证伪阈值、管理层评价、模型中的关键假设、或近期某次财报/公告的既有结论。
- **动作**：直接基于知识层作答，**必须在依据中显式引用知识层版本与更新日期**（例如：`依据 thesis v13（2026-10-19）及 drivers.md`）。

### 类型 (b)：需要最新实时信息
- **场景**：询问“今天股价为什么大跌/大涨”、“刚出的 8-K/季报怎么看”、“最近一周卖方一致预期有没有动”或某个最新行业新闻的影响。
- **动作**：
  1. 先查 `verdicts.md` 今天是否已有自动监控（如 `dalton-thesis-delta`、`dalton-filing-review`）生成的结论；
  2. 若无，按问题性质立即调用对应快速技能或定向查询：
     - 股价异动 → 按 `/dalton-stock-move` 跑 `move_quant.py` 并检索异动原因；
     - 刚披露季报 → 按 `/dalton-earnings-flash` 快速比对指引与一致预期；
     - 多空焦点快评 → 按 `/bullbear` 快速模式拆解；
     - 单项数据核实 → 调用 `researchdata`（`finance` / `research` / `news`）定向拉取一手数据。

### 类型 (c)：需要全新深度研究
- **场景**：PM 问及知识层尚未覆盖的新竞争对手、新技改路线、未建模的新业务分部、或跨行业专题。
- **动作**：
  1. 先基于现有知识层给出**初步判断、已知事实边界与核心未知项**（绝不不懂装懂）；
  2. 明确向 PM 提议启动 `/topic-research`（或 `/bullbear`、`/dalton-earnings-deep`），说明拟拆解的核心问题；
  3. 若 PM 同意立即深挖则启动对应工作流；若暂不深挖，通过 `agenda_ops` 将该问题记入 `agenda.md` 的 `open_questions`（标记 `origin="pm"`）。

---

## 3. 第三步：固定五段回答结构（5-Part Response Format）

所有对 PM 的实质性研究问答，统一采用 [templates/pm-qa.md](templates/pm-qa.md) 的五段式结构（长短随问题复杂度伸缩，但五段要素不可缺）：

1. **直接回答**：第一句话直接亮明立场与量化结论（“是/否/强化/削弱 + 核心数字”），不铺垫背景。
2. **依据**：清晰区分 **【知识层既有定论】**（标明 `thesis v#` / `drivers.md` / 模型单元格）与 **【本次新查证据】**（标明具体来源、日期与六类证据标签：`事实` / `公司表态` / `一致预期` / `模型推导` / `判断` / `假设`）。
3. **和现有观点的关系**：明确说明这件事对当前 `thesis.md` 的哪条支柱、`debate.md` 的哪个焦点或 `drivers.md` 的哪个变量构成影响，属于事件五词中的哪一个（`THESIS_STRENGTHENED` / `THESIS_WEAKENED` / `THESIS_BROKEN` / `NO_CHANGE` / `NEW_THESIS`）。
4. **不确定性与改判条件**：当前还有什么关键信息是未知的（`目前缺乏数据`）？未来在什么时间窗口、观察到哪个指标越过什么具体量化阈值，会让我们改变今天的回答？
5. **下一步**：明确列出已完成或待执行的动作（如：已更新 `verdicts.md` 与 `agenda.md`；或建议启动 `/topic-research`；或无需修改知识层）。

---

## 4. 第四步：收盘三步（Closing Discipline）

1. **引入新证据或改变判断时（必写 Verdict + Agenda）**：
   - 只要回答过程中查询并采纳了新证据、修正了驱动变量、或触发了支柱状态变化，必须在回答结尾调用：
     - `knowledge_append_verdict`（`trigger.kind="pm_question"`，写明事件五词结论与摘要）；
     - 若需同步更新 `thesis` / `drivers` / `debate`，调用 `knowledge_update`；
     - 将 PM 的关切点或衍生出的待验证问题通过 `agenda_ops` 写入 `agenda`（`open_questions`，`origin="pm"`）。
2. **仅复述解释既有知识层时（不写 Verdict）**：
   - 若本次仅向 PM 解释或引用知识层已有结论、未引入任何新外部证据且观点无变化，**不要调用 `knowledge_append_verdict`**（保持 `verdicts.md` 信噪比）；
   - 但若 PM 的提问指出了新的研究方向或优先级调整，仍应通过 `agenda_ops` 更新 `agenda.md`（`origin="pm"`）。

---

## 5. Home（研究总监）路由规则

当 PM 在 **Home 工作区**向研究总监提问时：
1. **单公司已有结论查询**：直接只读读取 `../<TICKER>/` 下的 `thesis.md`、`debate.md`、`drivers.md`、`verdicts.md` 作答，并标注数据来自 `<TICKER> thesis v#`。
2. **跨公司横向对比 / 组合层面提问**：先调用 `dalton_coverage_board` 获取全覆盖面板，再按需读取相关公司的知识层文件做横向对比表。
3. **需要单公司新分析或更新知识层**：先基于现有知识层给出初步回答，随后调用 `delegate_to_analyst` 将深挖任务或知识层修改提案派发给对应 `<TICKER>` 分析师工作区，并在【下一步】中告知 PM 已交办。
4. **PM 下达研究优先级变更**：调用 `dalton_agenda_note` 写入对应公司的议程。
5. **边界红线**：严禁在 Home 直接做单公司重型深度检索或直接跨目录修改 `../<TICKER>/data/*.json`。

---

## 6. 四条硬性禁令（Prohibitions）

1. **严禁不读知识层直接靠搜索或通用记忆作答**：导致前后口径矛盾是严重失职。
2. **严禁把卖方一致预期（Street Consensus）当成本基金观点（House View）**：必须明确区分“卖方普遍预期 X，但我们认为 Y”。
3. **严禁只罗列正反两面事实而不给明确立场**：“一方面…另一方面…”而无倾向性判断对 PM 毫无价值。
4. **严禁把研究观点或新结论存进会话 Memory**：所有观点沉淀必须走 `knowledge_update` / `knowledge_append_verdict` 写入知识层文件。
