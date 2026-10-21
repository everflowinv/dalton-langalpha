---
name: dalton-bullbear
description: "梳理个股市场共识、2–4 个核心多空分歧、盈利预测修正轨迹与隐含费用空间、近期催化剂及改判条件。用户想弄清市场对某只股票在争什么、预期差在哪时使用；对话快速版几分钟可完成，若要求完整 v4.2 长稿则启动多阶段深度工作流。"
---

# Dalton 个股多空讨论（`dalton-bullbear`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

本 skill 提供两种交付形态，共用同一套买方分析链：**市场共识（price in 了什么）→ 真正的分歧（Top Debates）→ 盈利预测修正地图（含隐含费用空间与敏感度）→ 催化剂与 KPI → 改判条件**：

1. **对话快速版（默认路径）**：PM 问“X 的多空在争什么 / earnings revision / 近期催化剂”且未要求完整文档，或输入 `/bullbear {TICKER}` 时，由主智能体直接在对话中完成取证与判断，不派发 workflow、不跑财务体检和股东回报支线。
2. **v4.2 完整成稿（`bullbear-doc` workflow）**：PM 要求正式多空长稿、或按研究议程需要产出完整 v4.2 文档时，启动 `bullbear-doc` workflow（6 个子任务阶段：准备 → 股东回报支线 → 主稿 → 合并 → 独立审稿 → 修订）。

---

## 一、对话快速版流程（默认，主智能体直接执行）

> **速度原则**：快速版的价值在速度与判断锐度。不要在快速版里跑 `fincheck.py` 和股东回报支线。

1. **读基金方法论与公司知识层**
   - 读 `.agents/skills/dalton-conventions/SKILL.md`。
   - 按 `.agents/skills/dalton-knowledge/SKILL.md` 读序读取当前工作区的 `agent.md`、`thesis.md`、`debate.md`、`drivers.md`。
   - 若已有历史 `debate.md`，本轮以**增量更新与对比**为主：重点核查哪些 debate 的倾向或置信度发生了变化、原因是什么（新证据 vs 已 price in），禁止忽略旧结论直接覆盖。

2. **一致预期与隐含费用空间**
   - 在 PTC（`execute_code`）中先调一次 `researchdata.catalog()`，再调 `researchdata.finance(route="estimates", ticker=T)`，获取本财年（`0y`）与下一财年（`+1y`）EPS、收入一致预期、7/30/90 天 EPS 修正轨迹、近 30 天上调/下调次数，以及近 4 次财报次日涨跌。
   - 每次取数将原始 JSON（附 `_meta{route, fetched_at, source}`）存盘到 `data/consensus/<YYYY-MM-DD_HHMM>.json`。
   - 运行隐含费用测算脚本（或调用同名函数）：
     ```bash
     python .agents/skills/dalton-bullbear/scripts/implied_model.py --estimates-json data/consensus/<latest>.json
     ```
     若最近财年存在一次性税项，传入 `--tax <公司指引有效税率>`，得出一致预期隐含税前利润、隐含总费用空间（`implied_total_expenses_bn`）、每 100 亿美元费用的 EPS 敏感度（`eps_per_10bn_expense`）及每 1% 收入的 EPS 敏感度（`eps_per_1pct_revenue`）。

3. **内部研究库四路由检索**
   - 对 `sales-notes`、`tmtb`、`funda-ai`、`company-wiki` 四个路由，分别用以下三组关键词各查一次（时间窗默认最近 92 天）：
     1. 公司名 / 股票代码
     2. 核心产品或核心业务名
     3. `"capex"` / `"费用"` / `"指引"` / `"预测"`
   - **Sales color 专项规则**：按公司名和代码分别搜，邮件按 `message_id`、研报按 `doc_id` 去重后用 `read` 读正文（多票日报按 `next_offset` 翻页摘出本公司段落）。按正文里的 sales/trading 身份声明归类，不按数据库分类（如 `Sector Specialist Commentary`、`TECH SKETCH` 可能归在 `foreign_report` 下）。

4. **AlphaEngine 卖方研报检索**
   - 中英文 query 各搜一次（`researchdata.research(route="alphaengine", operation="search_library", ...)`），必须传 `sort="relevance"` 并同时给 `date_from`、`date_to`（默认 92 天）。
   - 挑 3–6 篇高相关报告调 `operation="get_document"` 读正文，优先外资行（MS、GS、UBS、JPM、Citi、BofA、Jefferies、Bernstein 等）的财报前瞻与预测调整报告。
   - 若涉及 AI 收入占比、大单 TCV/RPO 或 AI 降本提效，遵循 `references/ai-revenue-rules.md` 核对口径。

5. **一手指引核对**
   - 用 `dalton_sec.list_filings` 与 `researchdata.finance(route="sec")` 核对最新 8-K / 10-Q / 10-K 的业绩指引原文与电话会纪要，记录 accession 与期间。分析师提问中提及但管理层未确认的数字，标为“分析师提问口径，管理层未确认”。

6. **按固定结构在对话中回复**（除非 PM 要求写文件，否则直接在对话输出）：

```markdown
**结论**：修正方向（上修/下修/持平）、财报风险方向（偏上行/偏下行/中性）、置信度（高/中/低）——一句话给立场与核心依据
**多空在争什么（估值之外）**：2–4 个 debate，每个一行：分歧变量｜Bull 怎么看（带来源）｜Bear 怎么看（带来源）｜我的倾向与置信度（若与上一版不同，写明变化原因）
**盈利预测修正三层**：收入｜费用/capex 或利润率｜一次性项目（按对 EPS 影响排序）；附隐含费用空间与敏感度（`[假设] 卖方费用比一致预期隐含空间多 X，对应 EPS 少 Y`）
**3–6 个月催化剂**（按时间排）：事件｜日期或窗口｜检验哪个 debate｜bull/bear 兑现阈值
**改判条件**：什么可观测证据或数字出现我会换边
**来源**：每类来源（内部库四路由 / AlphaEngine / SEC filing / 一致预期）命中与实际使用数；本轮未取得的写明原因（无相关结果 / 渠道失败 / 未覆盖）
```

7. **收尾：写知识层并询问是否转成稿**
   - 按下方“三、知识层写入与发布规则”更新 `debate`、`drivers` 并追加 `verdict`。
   - 询问 PM 是否需要跑 `bullbear-doc` 生成包含独立股东回报支线与 XBRL 财务体检的 v4.2 完整成稿。

---

## 二、v4.2 完整成稿流程（`bullbear-doc` workflow）

当 PM 要求完整文档、深度多空成稿，或议程自动任务触发完整成稿时：

1. **加载 workflow 工具**
   - 先读 `.agents/skills/run-workflow/SKILL.md` 以解锁 `RunWorkflow`。
   - 读 `.agents/skills/dalton-bullbear/references/v42-structure.md` 与 `references/return-branch.md` 了解产物标准。

2. **启动 `bullbear-doc`**
   - 调用 `RunWorkflow`：
     ```json
     {
       "workflow": "bullbear-doc",
       "params": {
         "ticker": "<TICKER>",
         "company": "<公司名>",
         "market": "US",
         "workspace_dir": "<当前工作区路径>",
         "as_of": "<YYYY-MM-DD>",
         "research_days": 92,
         "prior_draft": "<可选：上一版 bullbear 草稿或发布稿路径>"
       }
     }
     ```
   - Workflow 内部自动执行 6 个派发阶段：
     - **阶段 1（准备）**：跑 `.agents/skills/dalton-financial-lenses/scripts/anchors.py` 锁定市值与汇率（`anchors.json`）；跑 `.agents/skills/dalton-bullbear/scripts/fincheck.py` 生成资产负债表触发项与 Capex 体检（`fincheck.md`）；存一致预期快照并跑 `implied_model.py`（`consensus.md`）；锁定价格锚（`price.md`）。
     - **阶段 2（股东回报支线）**：按 `references/return-branch.md` 与 `return_score.py` 产出第一节（`return.md`，≤40 行）、第九节附录（`return_appendix.md`，≤30 行）与 5 条摘要（`return_summary.md`）。
     - **阶段 3（主稿）**：按 `references/v42-structure.md` 和 `templates/bullbear.md` 撰写 `main.md`，跑 `scripts/bb_check.py` 修到 0 WARN。
     - **阶段 4（合并与审稿）**：把 `return.md` 插入 `<!-- INSERT:return -->`、`return_appendix.md` 插入 `<!-- INSERT:return_appendix -->`，生成 `drafts/<as_of>-bullbear.md`，复跑 `bb_check.py` 到 0 WARN，并派 `dalton-reviewer` 独立审稿。
     - **阶段 5（修订）**：按审稿意见处理全部 P0 与可改 P1，复跑 `bb_check.py` 确认 0 WARN。
   - **Workflow 不可用时的降级路径**：若 `config.yaml: workflow.enabled` 关闭导致 `RunWorkflow` 不可用，主智能体按上述阶段 1→5 的顺序手动执行脚本与支线写作，最终输出到 `drafts/<as_of>-bullbear.md`。

3. **主智能体核查与收尾**
   - 检查 `drafts/<as_of>-bullbear.md` 是否存在，运行 `python .agents/skills/dalton-bullbear/scripts/bb_check.py drafts/<as_of>-bullbear.md` 确认 0 WARN。
   - 可选调用 `check_draft(draft_path="drafts/<as_of>-bullbear.md")` 查看数字核验反馈（草稿期不阻断）。
   - 执行下方知识层写入，向 PM 汇报核心判断、各 Debate 倾向变化及审稿结论。

---

## 三、知识层写入与发布规则（主智能体收尾必做）

无论快速版还是完整成稿，结束时均由主智能体按 `.agents/skills/dalton-knowledge/SKILL.md` 与 `.agents/skills/dalton-agenda/SKILL.md` 写入知识层（子智能体无 `knowledge_*` 工具）：

1. **`knowledge_update(doc="debate")`**
   - 每个 Debate 写入一条结构化记录：分歧变量、多方论据（Bull）、空方论据（Bear）、双方最强反驳、我的判断（立场与置信度）、依赖假设、改判条件、来源引用。
   - 若某条 Debate 的倾向或置信度相比旧版发生变化，使用 `set` 操作更新并在 `reason` 中写明变化原因（服务端自动在 `debates[].history` 保留旧值与原因）。

2. **`knowledge_update(doc="drivers")`**
   - 将 §4（Earnings revision map）识别出的核心驱动变量与 EPS/收入敏感度、§5（Catalyst & KPI）的 3–5 个跟踪 KPI 及 bull/bear 区间写入 `drivers`（新读数用 `append`）。

3. **`knowledge_append_verdict`**
   - `trigger.kind`：PM 发起填 `"manual"`，议程触发填 `"agenda"`。
   - `verdict`：对照上一版 debate 与 thesis 给出正式五词结论之一（`NO_CHANGE` / `THESIS_STRENGTHENED` / `THESIS_WEAKENED` / `THESIS_BROKEN` / `NEW_THESIS`）。
   - `agenda_ops`：把 §5 的催化剂写入 `watch.events`，把改判条件写入 `next_validations`。

4. **发布（`publish_report`）**
   - 对话快速版不发布。
   - 完整成稿默认保存在 `drafts/<as_of>-bullbear.md`；当 PM 要求正式发布时，调用 `publish_report(draft_path="drafts/<as_of>-bullbear.md", title="...", kind="bullbear", slug="...")`。

---

## 四、本 Skill 目录文件

- `references/v42-structure.md`：v4.2 完整成稿章节结构、写作与引用规则、信源检索细则。
- `references/return-branch.md`：股东回报支线（第一节 ≤40 行、第九节附录 ≤30 行、5 条摘要）规则。
- `references/ai-revenue-rules.md`：AI 收入口径（已确认收入 vs ARR vs TCV/RPO）与 AI 提效费用核查规则。
- `templates/bullbear.md`：v4.2 中文成稿 Markdown 模板（含 `<!-- INSERT:return -->` 与 `<!-- INSERT:return_appendix -->`）。
- `scripts/bb_check.py`：v4.2 机械格式检查与支线合并脚本。
- `scripts/fincheck.py`：基于 `dalton_sec` / SEC XBRL companyfacts 的资产负债表大额/跳变科目与 Capex 强度体检脚本。
- `scripts/implied_model.py`：一致预期隐含税前利润、隐含总费用空间与 EPS 敏感度测算脚本。
