---
name: dalton-topic-research
description: "启动专题研究（topic-research workflow）：围绕产业、竞争、监管或技术专题按 Phase 0–7 执行多集群并行证据收集（精读全文 + 内部纪要/Guidepoint 20 词限制）、盲区检查（写下“盲区检查完成”）、置信度标注校验（validate_topic.py）、数字核验与 dalton-reviewer 独立审稿，回写相关公司知识层与复利教训，不写回基金 wiki。触发词：专题研究、专题深度、行业研究、竞争格局研究、topic research、/topic-research。"
---

# 专题研究（Topic Research）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

## 0. 启动工作流（第一步必读）

1. **先读 `.agents/skills/run-workflow/SKILL.md`**，解锁 `RunWorkflow` 工具。
2. **范围确认与锁定**：
   - 在与 PM 对话时，先用 **≤3 个问题**确认并锁定 5 项范围：
     1. **研究问题与边界**（核心要回答什么、不回答什么）
     2. **时间窗口**（默认优先近 30 天；若指定区间如 `2026Q1–2026Q3`，所有查询必须带时间范围并在标题注明）
     3. **分析维度 / 集群**（默认采用 [references/clusters-investment.md](references/clusters-investment.md) 的 5 个投资研究集群 C1–C5，或按专题定制 3–6 个集群）
     4. **输出粒度与涉猎公司**（涉及哪些覆盖公司或可比公司 `related_tickers`）
     5. **对比基准**（当前市场主流叙事 vs 我们的假设）
   - 若 PM 不在场（如由 `agenda.md` 自主研究议程触发），从 `agenda.md` 对应条目直接锁定范围，并在报告头部注明假设。
3. **调用 `RunWorkflow`**：
   ```python
   RunWorkflow(
       workflow="topic-research",
       params={
           "topic": "<专题名称>",
           "slug": "<kebab-case-slug>",
           "horizon": "<时间范围，如 2026Q1-2026Q3 或 近30天>",
           "workspace_dir": "<当前工作区路径>",
           "related_tickers": ["<TICKER1>", "<TICKER2>"],
           "core_questions": ["<Q1>", "<Q2>", "..."]
       }
   )
   ```

---

## 1. Phase 0 — 研究规划（Research Planning）

1. **先读现有知识层与基金 Wiki（只读）**：
   - 若在公司工作区，先读 `thesis.md`、`debate.md`、`drivers.md`、`management.md`、`lessons.md`；若涉及其他覆盖公司，跨目录读取 `../<TICKER>/thesis.md` 与 `drivers.md`。
   - 用 `researchdata.research(route="company-wiki")`（或只读文件检索）查阅共享只读基金百科 `wiki/` 中已有的行业/技术/供应链条目（**严禁写回 `wiki/`**）。
2. **拆解 3–6 个信息集群（Clusters）**：
   - 参考 [references/clusters-investment.md](references/clusters-investment.md)。
   - 为每个集群生成 **5–8 条中英文检索词**（含精确机构名、产品代号、供应链术语、时间限定词）。
   - 明确 **5–8 个核心可证伪问题**（每个问题绑定需验证的指标与来源类型）。

---

## 2. Phase 0.5 — 一手锚点采集（Primary Source Anchoring）

在派发并行搜索前，先锁定一手基准数据作为锚点：
- **公司 / 竞争专题**：拉取相关公司最新 10-K / 10-Q / 20-F（`dalton_sec.list_filings` / `dalton_sec.get_company_facts` 或 `researchdata.finance(route="sec")`）、近 1–2 个季度电话会纪要（`researchdata.research(route="transcript")`）、近 6 个月 AlphaEngine 卖方深度报告（`researchdata.research(route="alphaengine", operation="search_library")`）。
- **行业 / 技术 / 政策专题**：拉取监管原文、行业协会统计、头部公司披露口径及内部共享资料库（`researchdata.research(route="sales-notes")`、`researchdata.research(route="tmtb")`、`researchdata.research(route="funda-ai")`）。

---

## 3. Phase 2 — 多集群并行证据收集（Parallel Cluster Evidence）

按集群并行派发 3–6 个 `dalton-evidence` 子代理，每个集群遵守以下硬约束：

1. **精读全文**：每个集群在广度搜索（5–8 轮中英查询）后，必须挑选 **3–5 篇最高质量长文 / 原始文件调用全文读取**（`researchdata.research(route="alphaengine", operation="get_document")`、`web_fetch` 或 SEC 原文 `researchdata.finance(route="sec")`），严禁仅凭搜索摘要写结论。
2. **内部记忆库与专家网络必查**：
   - 检索 `researchdata.research(route="sales-notes")`、`researchdata.research(route="tmtb")`、`researchdata.research(route="funda-ai")`。
   - 检索 `researchdata.research(route="guidepoint")` 相关专家访谈。**合规红线：Guidepoint 内容以提炼观点为主，直接引用原文每篇不得超过 20 个英文词，严禁将完整访谈全文贴入报告。**
3. **结构化证据包返回**：每条证据必须带完整 `[S#]` 来源（标题、机构/作者、发布日期、URL/doc_id）、数据所属期（如 `2026Q2`）及原文关键数字。

---

## 4. Phase 3 & Phase 3.5 — 交叉验证与盲区检查（Cross-Verification & Blind Spot Gate）

1. **逐条标注置信度（遵循 [references/principles.md](references/principles.md)）**：
   - `【高】`：≥3 个独立可信来源相互印证（同一来源被多家媒体转述只算 1 个）。
   - `【中】`：2 个独立来源印证，或 1 个权威一手来源（如 SEC 10-K 原文）。
   - `【低】`：单一非一手来源、管理层单方口径、或搜索合成推断（**必须写明低置信原因**）。
   - `【冲突】`：不同可信来源给出矛盾数字或相反结论——**必须原样保留各方口径与来源（如“来源 A 称‘数千人’[S15]，来源 B 称‘数百人’[S16]”），严禁折中合并成“数百至数千人”**。
   - `【更正】`：修正过往报告或市场流传的错误数据时，必须写明：**原错误值 → 更正后值 → 更正依据**。
2. **Phase 3.5 盲区检查硬门禁**：
   - 对照 Phase 0 的 5–8 个核心问题逐一核查：是否有问题尚无一手数据？是否有反方最强论据未被检索？是否有时间超过 6 个月的陈旧数据未做最新校验？
   - 若存在关键缺口，最多追加派发 **≤2 个补漏 `dalton-evidence` 子代理**定向补齐。
   - 补齐后（或确认无盲区后），必须在工作流记录与底稿核验区明确写下 **`盲区检查完成`**（未写下“盲区检查完成”不得进入 Phase 4 写作）。

---

## 5. Phase 4 — 撰写报告与脚本校验（Writing & Validation）

1. 按 [templates/topic.md](templates/topic.md) 撰写 `drafts/<YYYY-MM-DD>-topic-<slug>.md`。
   - **重深度、反凑字数**：不设 300/400/500 行机械字数下限，以证据密度、因果机制与对覆盖公司判断的实质贡献为准。
   - **数据缺口诚实声明**：找不到可靠数据的维度直接写 **`目前缺乏数据`**，严禁估算编造。
   - **人物首次出现带全名+职务**，所有关键数字带具体日期/季度。
2. **运行置信度与引用闭环校验脚本**：
   ```bash
   python3 .agents/skills/dalton-topic-research/scripts/validate_topic.py \
     --input drafts/<YYYY-MM-DD>-topic-<slug>.md --json
   ```
   - 修复所有 `INFLATED_HIGH`（标`【高】`但引用 <3）、`INFLATED_MEDIUM`（标`【中】`但引用 <2）、未定义引用或缺失章节错误。
   - 若脚本提示 `LOW_RATIO_WARNING`（`【低】`占比 < 15%），检查是否存在把单一来源拔高为`【中】`/`【高】`的置信度注水，并在送审时提请审稿人重点复核。

---

## 6. Phase 5 & Phase 6 — 数字核验与独立审稿（Verification & Independent Review）

1. **Phase 5 数字核验表**：将正文中所有核心财务/行业数字与 SEC 原始文件或行情源逐一核对，在报告末尾填写 `## 数字核验表`（指标｜报告数值｜原始来源｜核验状态 `PASS`/`FAIL`）。存在任何 `FAIL` 不得送审。
2. **Phase 6 独立审稿（`dalton-reviewer`）**：
   - 将草稿、`validate_topic.py` 输出与数字核验表交给 `dalton-reviewer` 审阅。
   - 若返回 `MAJOR_REVISION` 或 `REJECT`，按审稿意见退回补证据或重写，直至获得 `PASS` 或 `MINOR_REVISION`（改完小修项）。

---

## 7. Phase 7 — 知识层回写与收盘三步（Knowledge Layer Update & Handoff）

1. **更新当前公司知识层**（若在公司工作区）：
   - 将专题沉淀的结构性变化通过 `knowledge_update` 写入 `drivers` 或 `debate`。
   - 若专题揭示了可复用的研究规律或踩坑教训，写入 `lessons`（必须带 `applies_when` 与 `scope`：单公司或行业级）。
   - 调用 `knowledge_append_verdict`（`trigger.kind="agenda"`）记录本次专题对 thesis 的结论，并通过 `agenda_ops` 关闭已完成的议程项、登记新发现的跟踪项。
2. **跨公司更新走提案机制**：
   - 若专题结论涉及其他覆盖公司（或当前在 Home 工作区），**不得直接跨目录修改其他公司的 `data/*.json`**，应生成知识层修改提案，在 Home 通过 `delegate_to_analyst` 交办给对应公司分析师，或在公司工作区通过 `pm_handoffs` 提请同步。
3. **严禁写回基金 Wiki**：`wiki/` 目录由基金统一维护，分析师只读不写。
4. **发布归档**：当 `dalton-reviewer` 通过且 PM 要求正式发布时，先运行 `check_draft`，再调用 `publish_report(draft_path=..., kind="topic", slug="<slug>", updates_thesis=False)`。
