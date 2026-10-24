---
name: dalton-topic-research
description: "围绕跨公司产业趋势、竞争格局、监管或技术专题，按多信息集群并行搜集一手披露、卖方研报与专家访谈证据，经盲区检查与独立审稿产出深度专题报告并回写相关公司知识层。用户想深挖某个行业或技术关键问题时使用；属于完整深度研究工作流。"
---

# 专题研究（Topic Research）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

## 0. 启动工作流（第一步必读）

> **运行工作区约束**：本工作流固定在具体的**公司工作区**内由该公司分析师（`analyst`）执行（完成契约要求更新公司知识层并由仅对 `analyst` 开放的 `dalton-reviewer` 审稿）。若当前在 **Home** 工作区（研究总监 `chief_of_staff`），选定与本专题最相关的核心覆盖公司工作区，调用 `delegate_to_analyst(question=..., workspace_id=..., report_back=True)` 交由该公司分析师在公司工作区执行本技能。

### 开始前：检查未完成的 run（中断续跑）

- 读 `research/<D>-topic-<slug>/.progress.json`（及 `research/` 下其他 `*/.progress.json`；`<D>` 未定时目录用 `latest`），看是否有本 workflow 的 `status` 不是终态（`completed`/`failed`/`cancelled`）的 run。
- 有未完成的 run 时不要新开 run：用**原参数**调用 `RunWorkflow(workflow="topic-research", params={...原参数..., "progress_path": "research/<D>-topic-<slug>/.progress.json"}, resume="<.progress.json 里的 run_id>")` 续跑；label、prompt 哈希与产物哈希都一致的已完派发会直接复用，不重复派子任务。若前一次 run 因审稿 `block` 等原因提前停止，补齐问题后同样用**原参数**加 `resume` 重跑（workflow 返回值里已提供 `resume_hint`）。
- 全新启动时也在 `params` 里带上 `"progress_path": "research/<D>-topic-<slug>/.progress.json"`，让中断后的 run 可被发现与续跑。

1. **先用 Read 工具读 `.agents/skills/run-workflow/SKILL.md`**，了解 workflow 规范（workflow 启用时，加载本 skill 会自动解锁 `RunWorkflow`）。
2. **范围确认与锁定**：
   - 在与 PM 对话时，先用 **≤3 个问题**确认并锁定 5 项范围：
     1. **研究问题与边界**（核心要回答什么、不回答什么）
     2. **时间窗口**（默认优先近 30 天；若指定区间如 `2026Q1–2026Q3`，所有查询必须带时间范围并在标题注明）
     3. **分析维度 / 集群**（默认采用 [references/clusters-investment.md](references/clusters-investment.md) 的 5 个投资研究集群 C1–C5，或按专题定制 3–6 个集群）
     4. **输出粒度与涉猎公司**（涉及哪些覆盖公司或可比公司 `related_tickers`）
     5. **对比基准**（当前市场主流叙事 vs 我们的假设）
   - 若 PM 不在场（如由 `agenda.md` 自主研究议程触发），从 `agenda.md` 对应条目直接锁定范围，并在报告头部注明假设。
3. **调用 `RunWorkflow`**（`as_of` 可不传或传 `"latest"`，`RunWorkflow` 会自动取最近一个美股交易日，`resume` 续跑时自动沿用上一 run 的值）：
   ```python
   RunWorkflow(
       workflow="topic-research",
       params={
           "topic": "<专题名称>",
           "slug": "<kebab-case-slug>",
           "as_of": "<YYYY-MM-DD 或 latest，可不传>",
           "horizon": "<时间范围，如 2026Q1-2026Q3 或 近30天>",
           "workspace_dir": "<当前工作区路径>",
           "related_tickers": ["<TICKER1>", "<TICKER2>"],
           "core_questions": ["<Q1>", "<Q2>", "..."],
           "progress_path": "research/<D>-topic-<slug>/.progress.json"
       }
   )
   ```
   Workflow 不可用时：
   - **`workflow.enabled` 已关闭**（工具列表本来就不会有 `RunWorkflow`）：主智能体按下述 Phase 的顺序手动执行，生成各阶段要求的契约产物文件（`research/<D>-topic-<slug>/plan.md`、`primary.md`、至少 3 个 `cluster-*.md`、`internal-memory.md`、`guidepoint.md`、含 `盲区检查完成` 的 `cross-check.md`、`number-audit.md`、`knowledge-revision.json` 及 `drafts/<D>-topic-<slug>.md`），并逐阶段核对产物。Phase 6 派 `dalton-reviewer` 审稿按三步执行：（1）主智能体先计算草稿的 sha256（`sha256sum drafts/<D>-topic-<slug>.md` 或等价的 Python `hashlib.sha256`）；（2）把该哈希写进派给 `dalton-reviewer` 的提示词；（3）要求审稿人在返回的 JSON 里原样回填 `draft_sha256`，并将返回的 JSON（含 `verdict` 与 `draft_sha256`）原样保存到 `research/<D>-topic-<slug>/review.json`（多轮复审时保存最后一轮；改稿后重新计算哈希、重审并覆盖）。收尾调用 `dalton_check_done(workflow="topic-research", params={"topic": "<专题名称>", "slug": "<kebab-case-slug>", "workspace_dir": "<当前工作区路径>", "as_of": "<D>", "manual": true})`（不传 `run_id`），审稿据此文件核对。语言审查两轮（顺序不可颠倒，与 workflow 相同）：成稿后先调用 `check_language(draft_path=<草稿>, round=1)` 并按 `<草稿>.language.md` 清单修改（只改语言，不动事实、数字、术语、引用编号与判断强度），再派 `dalton-reviewer` 首次审稿；修订循环结束、终稿内容定稿后调用 `check_language(round=2)` 按清单完成终稿语言修改，草稿有改动就再调用一次 `check_language(round=2)` 对终稿复核，然后做最后一次审稿（pass 的 JSON 存 review.json）与 `check_draft`；第一次审稿就 pass 时：第 2 轮照常执行；改了稿就补一次终稿审稿（pass 的 JSON 覆盖存 review.json），哈希未变则无需重审。本次产出中其他给人读的 .md 笔记（research/ 支线、背景、快讯、异动、公告点评、专题章节等）同样各审两轮：第 1 轮审完按清单改，第 2 轮复核，改过就再跑一次 round=2 确认；json/xlsx 等机器文件、bg_data/、data/、lenses_work/、model_dump/、知识层渲染文件与 .audit/.annotated/.language 侧车不审。
   - **否则**（`workflow.enabled` 未关闭）：`RunWorkflow` 不在工具列表时，先用 **Read 工具**（不要用 Bash `cat`）读 `.agents/skills/run-workflow/SKILL.md` 后重试；仍没有就停下，告诉 PM「RunWorkflow 不可用」，不要自行手动派发 Task 子智能体代跑。

---

> **第 1–6 节是 workflow 内部各阶段说明（Phase 0–6，由 `RunWorkflow` 执行）。** 仅当 `workflow.enabled` 已关闭时，才由主智能体按这些阶段手动执行并落盘各阶段产物文件。第 7 节（Phase 7）由主智能体在 workflow 结束后亲自完成。

## 1. Phase 0 — 研究规划（Research Planning，产出 `research/<D>-topic-<slug>/plan.md`）

1. **先读现有知识层与基金 Wiki（只读）**：
   - 先读当前公司工作区的 `thesis.md`、`debate.md`、`drivers.md`、`management.md`、`lessons.md`；若涉及其他覆盖公司，跨目录读取 `../<TICKER>/thesis.md` 与 `drivers.md`。
   - 用 `researchdata.research(route="company-wiki")`（或只读文件检索）查阅共享只读基金百科 `wiki/` 中已有的行业/技术/供应链条目（**只读不写回 `wiki/`**）。
2. **拆解 3–6 个信息集群（Clusters）并写入 `research/<D>-topic-<slug>/plan.md`**：
   - 参考 [references/clusters-investment.md](references/clusters-investment.md)。
   - 为每个集群生成 **5–8 条中英文检索词**（含精确机构名、产品代号、供应链术语、时间限定词）。
   - 明确 **5–8 个核心可证伪问题**（每个问题绑定需验证的指标与来源类型），将完整研究规划保存至 `research/<D>-topic-<slug>/plan.md`。

---

## 2. Phase 0.5 — 一手锚点采集（Primary Source Anchoring，产出 `research/<D>-topic-<slug>/primary.md`）

在派发并行搜索前，先锁定一手基准数据作为锚点并写入 `research/<D>-topic-<slug>/primary.md`：
- **公司 / 竞争专题**：拉取相关公司最新 10-K / 10-Q / 20-F（`dalton_sec.list_filings` / `dalton_sec.get_company_facts` 或 `researchdata.finance(route="sec")`）、近 1–2 个季度电话会纪要（`researchdata.research(route="transcript")`）、近 6 个月 AlphaEngine 卖方深度报告（`researchdata.research(route="alphaengine", operation="search_library")`）。
- **行业 / 技术 / 政策专题**：拉取监管原文、行业协会统计、头部公司披露口径及内部共享资料库（`researchdata.research(route="sales-notes")`、`researchdata.research(route="tmtb")`、`researchdata.research(route="funda-ai")`）。

---

## 3. Phase 2 — 多集群并行证据收集（Parallel Cluster Evidence，产出至少 3 个 `cluster-*.md`、`internal-memory.md`、`guidepoint.md`）

按集群并行派发 3–6 个 `dalton-evidence` 子代理，将各集群证据分别写入 `research/<D>-topic-<slug>/cluster-*.md`（至少 3 个文件，如 `cluster-1.md`…`cluster-5.md`），同时将内部记忆库与专家访谈专项汇总分别写入 `research/<D>-topic-<slug>/internal-memory.md` 和 `research/<D>-topic-<slug>/guidepoint.md`。每个集群遵守以下硬约束：

1. **精读全文与港 A 股交叉核对**：
   - 每个集群在广度搜索（5–8 轮中英查询）后，挑选 **3–5 篇最高质量长文 / 原始文件调用全文读取**（`researchdata.research(route="alphaengine", operation="get_document")`、`web_fetch` 或 SEC 原文 `researchdata.finance(route="sec")`），基于正文全文提取结论。
   - **港股、A 股公司的财务和经营数字**，必须用 `researchdata.finance(route="cn-hk", ...)` 交叉核对：`ask` 取平台值，`filings` 回公告原文；具体规则直接遵循 `.agents/skills/dalton-conventions/references/evidence-and-sources.md` 的 A/H（`cn-hk`）一节，不重复展开。
2. **内部记忆库（`internal-memory.md`）、专家网络（`guidepoint.md`）与社区情绪/线索层**：
   - 检索 `researchdata.research(route="sales-notes")`、`researchdata.research(route="tmtb")`、`researchdata.research(route="funda-ai")`，汇总输出至 `research/<D>-topic-<slug>/internal-memory.md`。
   - 检索 `researchdata.research(route="guidepoint")` 相关专家访谈，汇总输出至 `research/<D>-topic-<slug>/guidepoint.md`。**合规红线：Guidepoint 内容以提炼观点为主，直接引用原文每篇不超过 20 个英文词，不将完整访谈全文贴入报告。**
   - **社区层（雪球、员工评论、`last30days`）作为情绪和线索层**：对照 `<data_map>`，如果数据地图里有这些路由就调用采集情绪与一线线索；如果没有（或路由不可用），直接写进报告的“未查来源与缺口”。
3. **结构化证据包与时效性纪律**：
   - **每条数字证据都要同时带“数据所属期”（如 `2026Q2`）和“发布日期”（如 `2026-08-15`）**，并附完整 `[引用N]` 来源（标题、机构/作者、发布日期、URL/doc_id）及原文关键数字。
   - **发布日期超过 6 个月的证据标 `【陈旧】`**：只能作背景，不能作核心论据；用作核心论据之前，必须找到 6 个月内的更新，或者明确写明找不到。

---

## 4. Phase 3 & Phase 3.5 — 交叉验证与盲区检查（Cross-Verification & Blind Spot Gate，产出 `research/<D>-topic-<slug>/cross-check.md`）

将跨集群交叉验证与盲区核查结果写入 `research/<D>-topic-<slug>/cross-check.md`：

1. **逐条标注置信度与时效（遵循 [references/principles.md](references/principles.md)）**：
   - `【高】`：≥3 个独立可信来源相互印证（同一来源被多家媒体转述只算 1 个）。
   - `【中】`：2 个独立来源印证，或 1 个权威一手来源（如 SEC 10-K 原文）。
   - `【低】`：单一非一手来源、管理层单方口径、或搜索合成推断（**必须写明低置信原因**）。
   - `【冲突】`：不同可信来源给出矛盾数字或相反结论——**原样保留各方口径与来源（如“来源 A 称‘数千人’[引用15]，来源 B 称‘数百人’[引用16]”），不折中合并成“数百至数千人”**。
   - `【更正】`：修正过往报告或市场流传的错误数据时，必须写明：**原错误值 → 更正后值 → 更正依据**。
   - `【陈旧】`：每条数字证据都必须带“数据所属期”和“发布日期”；发布日期超过 6 个月的证据必须标 `【陈旧】`，只能作背景，不能作核心论据（用作核心论据前必须找到 6 个月内的更新，或写明找不到）。
2. **Phase 3.5 盲区检查硬门禁**：
   - 对照 Phase 0 的 5–8 个核心问题逐一核查：是否有问题尚无一手数据？是否有反方最强论据未被检索？是否有发布日期超过 6 个月的陈旧数据未标 `【陈旧】` 或未找 6 个月内更新？港股与 A 股财务/经营数字是否已按 `.agents/skills/dalton-conventions/references/evidence-and-sources.md` 的 A/H 一节通过 `finance(route="cn-hk", ...)`（`ask` + `filings`）交叉核对？
   - **对照 `<data_map>` 盘点未查来源**：写结论之前必须对照 `<data_map>`，在报告“未查来源与缺口”小节列出“相关但未查的来源”并说明原因（含社区层雪球、员工评论、`last30days` 等在数据地图中缺失或未用的情况）。
   - 若存在关键缺口，最多追加派发 **≤2 个补漏 `dalton-evidence` 子代理**定向补齐。
   - 补齐后（或确认无盲区后），必须在 `research/<D>-topic-<slug>/cross-check.md` 末尾明确写下 **`盲区检查完成`**（未写下“盲区检查完成”不得进入 Phase 4 写作）。

---

## 5. Phase 4 — 撰写报告与脚本校验（Writing & Validation，产出 `drafts/<D>-topic-<slug>.md`）

1. 按 [templates/topic.md](templates/topic.md) 撰写 `drafts/<D>-topic-<slug>.md`（`<D>` 为 `as_of` 日期 `YYYY-MM-DD`）。
   - **重深度、反凑字数**：不设 300/400/500 行机械字数下限，以证据密度、因果机制与对覆盖公司判断的实质贡献为准。
   - **数据缺口诚实声明**：找不到可靠数据的维度直接写 **`目前缺乏数据`**，不估算编造。
   - **人物首次出现带全名+职务**，所有关键数字带具体日期/季度。
2. **运行置信度与引用闭环校验脚本**：
   ```bash
   python3 .agents/skills/dalton-topic-research/scripts/validate_topic.py \
     --input drafts/<D>-topic-<slug>.md --json
   ```
   - 修复所有 `INFLATED_HIGH`（标`【高】`但引用 <3）、`INFLATED_MEDIUM`（标`【中】`但引用 <2）、未定义引用或缺失章节错误。
   - 若脚本提示 `LOW_RATIO_WARNING`（`【低】`占比 < 15%），检查是否存在把单一来源拔高为`【中】`/`【高】`的置信度注水，并在送审时提请审稿人重点复核。

---

## 6. Phase 5 & Phase 6 — 数字核验、独立审稿与知识层修订草案（Verification & Independent Review）

1. **Phase 5 数字核验表（产出 `research/<D>-topic-<slug>/number-audit.md`）**：将正文中所有核心财务/行业数字与 SEC 原始文件或行情源逐一核对，输出核验记录至 `research/<D>-topic-<slug>/number-audit.md`，并在报告末尾填写 `## 数字核验表`（指标｜报告数值｜原始来源｜核验状态 `PASS`/`FAIL`）。存在任何 `FAIL` 不得送审。
2. **Phase 6 独立审稿（`dalton-reviewer`，产出 `research/<D>-topic-<slug>/review.json` 与 `knowledge-revision.json`）**：
   - 将草稿、`validate_topic.py` 输出与 `number-audit.md` 交给 `dalton-reviewer` 审阅（含核查“`## 引用` 表存在且每个 `[引用N]` 都能在表里找到”；手动路径下先计算草稿 `sha256` 写进审稿提示词，要求审稿人原样回填 `draft_sha256`，将返回的 JSON 保存到 `research/<D>-topic-<slug>/review.json`）。
   - 审稿结论统一为 `pass` / `revise` / `block` 三值：返回 `revise` 时按 `must_fix` 补证据或修订后复审（最多 2 轮），直至获得 `pass`；返回 `block` 时停下并向 PM 汇报原因与缺口，不发起发布。
   - 审稿通过后，汇总对当前公司及关联覆盖公司的知识层更新建议，生成 `research/<D>-topic-<slug>/knowledge-revision.json`。

---

## 7. Phase 7 — 知识层回写与收盘三步（Knowledge Layer Update & Handoff，workflow 结束后主智能体必做）

1. **更新当前公司知识层**：
   - 读取 `research/<D>-topic-<slug>/knowledge-revision.json`，将专题沉淀的结构性变化通过 `knowledge_update` 写入当前公司工作区的 `drivers` 或 `debate`。
   - 若专题揭示了可复用的研究规律或踩坑教训，写入 `lessons`（必须带 `applies_when` 与 `scope`：单公司或行业级）。
   - 调用 `knowledge_append_verdict`（`trigger.kind="agenda"`）记录本次专题对 thesis 的结论，并通过 `agenda_ops` 关闭已完成的议程项、登记新发现的跟踪项。
2. **跨公司更新走提案机制**：
   - 若专题结论涉及其他覆盖公司，**不直接跨目录修改其他公司的 `data/*.json`**，在 `knowledge-revision.json` 中列出跨公司修改提案，并通过 `agenda_ops` 写入 `pm_handoffs`（或由 Home 研究总监通过 `delegate_to_analyst` / `dalton_agenda_note` 分发给对应公司分析师）。
3. **只读基金 Wiki**：`wiki/` 目录由基金统一维护，分析师只读不写。
4. **汇报完成前必须过完成契约（`dalton_check_done`）**：
   - 调用 `dalton_check_done(workflow="topic-research", run_id=<`research/<D>-topic-<slug>/.progress.json` 里的 run_id>, params={"topic": "<专题名称>", "slug": "<kebab-case-slug>", "workspace_dir": "<当前工作区路径>", "as_of": "<D>"})`（与启动时的参数一致；未传 `as_of` 时用 `as_of="latest"`）；
   - 走手动降级路径（`workflow.enabled` 已关闭、没有 run）时不传 `run_id`，调用 `dalton_check_done(workflow="topic-research", params={"topic": "<专题名称>", "slug": "<kebab-case-slug>", "workspace_dir": "<当前工作区路径>", "as_of": "<D>", "manual": true})`，审稿结论按 `research/<D>-topic-<slug>/review.json` 核对；
   - `done=false` 时按 `missing` 先补齐再查（缺文件补文件、`validate_topic.py` 修到通过、知识层缺 source=tool 新版本就 `knowledge_update`），补不了的缺项只能汇报"未完成"并列出缺项与原因，不得汇报"完成"。
5. **发布归档**（完成契约通过后）：当 `dalton-reviewer` 返回 `pass` 且 PM 要求正式发布时，先运行 `check_draft(draft_path="drafts/<D>-topic-<slug>.md")`，再调用 `publish_report(draft_path="drafts/<D>-topic-<slug>.md", title="<专题名称>", kind="topic", slug="<slug>", updates_thesis=False)`。
