---
name: dalton-earnings-flash
description: "财报发布后数小时内（T0+N 小时）产出一份财报快讯：头条数字与上一季同比、关键经营 KPI、指引变化、电话会 Q&A 要点、卖方初评与 sales 反馈、前瞻要点初步对照及事件五词初判，并确认次日深度链自动任务。触发词：财报快讯、earnings flash、业绩快讯、T0 快讯。"
---

# Dalton 财报快讯（`dalton-earnings-flash`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

---

## 目标与定位

在公司财报发布后数小时内（由 PS.7 的 `t0` 自动任务在 `release_time + N` 小时触发，或由分析师手动触发），向 PM 交付一份结构固定、零幻觉的**财报快讯**（落盘至 `research/<D>-flash-<季度>.md` 并直接推送）：
- 第一时间给出头条业绩、上一季同比对比、关键经营 KPI、指引变化、电话会 Q&A 核心要点、卖方初评与 sales 交易台反馈；
- 对照前瞻预测文件 `predictions.json` 给出 3–5 条初步核对结论；
- 给出事件五词结论**初判**（写入 `verdicts.md`），并确保次日 T+1 深度链自动任务（`tp1`）就绪。

> **注意**：快讯写入 `research/<D>-flash-<季度>.md` 并通过对话/`delivery` 推送，**不进 `drafts/`**，也不调 `publish_report`。多公司同日发布时，每家公司独立输出完整一块。

---

## 12 步必做流程

### Step 1：确认报告期间与三期对比基线
- 核实最新发布的财年与财季标签（沿用公司自身命名，如 `FY26Q3`，严格区分财季与自然季）。
- 准备三组对比期间：**当季（Current Quarter）、上一季（Prior Quarter）、去年同期（Year-Ago Quarter）**。
- 若正式 `10-Q` / `10-K` 尚未提交，使用财报 `8-K EX-99.1`（外国私人发行人用 `6-K` 业绩附件；港股在 P6 用 HKEX 公告）并在 `Sources` 中明确注明。

### Step 2：一手披露先行（含上一季同比与上次指引）
- 通过 `dalton_sec.list_filings` / `researchdata.finance(route="sec")` 及公司 IR 页面获取当季财报新闻稿（Earnings Release）、财务补充表（Financial Supplement）和演示稿（Investor Presentation）。
- **强制调取上一季财报新闻稿或电话会**：提取**上一季报告的同比增速（prior-quarter YoY）**与**上一季发布的本季/全年官方指引**。**严禁拿本季环比（QoQ）冒充“上一季同比增速”**，也严禁凭卖方转述代替上次官方指引。

### Step 3：电话会全文与 Q&A 提炼
- 调用 `researchdata.research(route="transcript", ticker=T, limit=2)` 定位本次财报电话会，并用 `researchdata.research(route="alphaengine", operation="get_document", params={"doc_id": ...})` 按 `offset` 分页读完全文。
- **`Q&A takeaways` 只从电话会 Q&A 部分提取**（提炼 5–10 个最有决策价值的管理层回答，每个话题 1–2 句，不把开场白塞进 Q&A）。
- **逐级降级规则**：若触发时刻电话会全文尚未入库，依次退到公司 IR Webcast 回放、8-K 预备发言稿（Prepared Remarks），并在正文开头与 `Sources` 中如实写明“截至触发时刻电话会纪要尚未入库”。

### Step 4：卖方初评与独立研究点评
- 调用 `researchdata.research(route="alphaengine", operation="search_library", ...)` 按公司名和 ticker 检索财报后报告（类别：`sell_side`、`sell_side_comment`、`sell_side_report`、`domestic_report`、`foreign_report`）。
- **时间窗严格限定为 `[官方发布时刻 T0, 触发时刻]`**，**严格排除财报前瞻（preview）**；记录每份采用报告的机构名、标题、发布日期与 `doc_id`。
- 同时间窗检索 `FUNDA AI`（`route="funda-ai"`）与 `TMTB`（`route="tmtb"`）：若有信息增量，在**卖方初评**下单独列为“**独立研究点评（仅有增量时）**”，标明来源身份，**绝不计入卖方一致预期或券商预测修正区间/中位数**。

### Step 5：Sales 与投资者反馈（严格时间窗与匿名纪律）
- 调用 `researchdata.research(route="sales-notes", params={"layer": "raw", "media": "all"})` 检索时间窗 **`[T0, 触发时刻)`**（或发布后 24 小时内，`T0` 为官方财报发布时间戳）。
- **合格标准**：只采纳包含机构销售台或买方客户真实反应、反复追问的问题、持仓/资金流观察、多空交锋、盈利质量看法的原始邮件；排除已在 AlphaEngine 覆盖的正式研报、纯新闻摘抄与会议通知。
- **去重与匿名**：按邮件线程去重，**严禁点名终端买方客户身份**，统一标注 `anecdotal`，不得升级为卖方共识。
- **空结果与失败区分**：
  - 若检索完成但无合格反馈：**直接省略 `Sales/Investor feedback` 整行（禁止写“未找到”或留空行）**；
  - 若通路/工具报错无法访问：正文中不写“无反馈”，只在 `Sources` 中披露访问缺口。

### Step 6：按行业选择核心经营 KPI
- 按 [`references/kpi-packs.md`](references/kpi-packs.md) 及公司自身披露核心指标，选取 **3–6 个关键经营 KPI**，列出当季绝对值、同比（YoY）、环比（QoQ）及**上一季同比增速**，并指出 1–2 个决定本季盈利质量的驱动项。
- 若公司变更 KPI 定义或分部披露结构，必须显式标注**可比性断点（comparability break）**。

### Step 7：对账与 Beat/Miss 计算公式
- **头条 Beat/Miss**：`beat/miss = 实际值 / 一致预期 - 1`，**仅在实际值与一致预期处于完全相同的会计口径（GAAP vs Non-GAAP/Adjusted）和同一币种时计算**。
- **亏损或零附近分母**：当指标为亏损、负 EPS 或分母接近 0 时，只报**绝对差额**（或利润率 bps 差额），禁止报告误导性百分比。
- **指引对比**：
  - **指引 Beat/Miss**：用本次指引**中点（midpoint）**对比同口径卖方一致预期，并同时保留完整指引区间；
  - **指引修正（Revision）**：用本次新指引区间/中点对比**上一季财报给出的官方指引**；未提供指引写“未提供（not guided）”。
- **一次性项目与冲突处理**：解释重大一次性损益，若其改变 beat 解读则同时列出 GAAP 与 Adjusted EPS；不同来源数字冲突时，**实际值一律以监管 filing 为准**，单家券商预测以该机构最新日期研报为准，并如实说明冲突。

### Step 8：最新股价与远期估值计算
- 取分析时点最近一个完整常规交易日收盘价（**普通未复权收盘价**，注明日期、币种、交易所与来源；yfinance 仅用于核对日期明确的市场价格，严禁作财报财务数据源）。
- **远期 P/E**：`最新收盘价 / 卖方财报后修正 EPS`（同财年、同 GAAP/Adjusted 口径）。
- 多家可比卖方已出修正时，给出本财年与下一财年收入/EPS 修正前后数值、百分比幅度，以及对应远期 P/E 的**区间与中位数**；**严禁把 GAAP 与 Adjusted EPS 混算或求平均**；负 EPS 标为 `NM`。
- **快讯时点兜底**：若快讯触发时卖方模型修正尚未发布，明确写“**卖方修正未出，次日深度点评补**”，禁止用财报前旧预测冒充财报后修正。

### Step 9：前瞻要点初步对照
- 读取工作区 `research/` 下最新的 `preview-<季度>/predictions.json`（及 `drafts/` 下对应的前瞻稿），挑选最重要的 **3–5 条预测**（如总营收、核心利润/EPS、下季指引、最关键 KPI 与核心 debate），在快讯末尾给出“**前瞻要点初步对照**”（完整逐条对账表在次日深度链 `dalton-earnings-deep` 中生成）。

### Step 10：事件五词初判与知识层写入
- 给出事件五词结论（快讯阶段允许注明“初判”，由次日深度链终判）：
  - JSON 正式词形：`NO_CHANGE` / `THESIS_STRENGTHENED` / `THESIS_WEAKENED` / `THESIS_BROKEN` / `NEW_THESIS`
  - Markdown 显示：无变化 / 强化 / 削弱 / 证伪 / 新论点
- 调用 `knowledge_append_verdict` 写入初判（`trigger.kind="earnings"`，`trigger.ref` 填 `<dalton_wake>` 的 `t0` 信号 id 或 `"manual"`），并在 `agenda_ops` 中保留次日深度链与前瞻完整对账待办。

### Step 11：确认或补建 T+1 深度链自动任务
- 检查本工作区 `.agents/user/automations/dalton-<t>-earn-<period>-tp1.json`（T+1 交易日 20:00 当地市场时区）是否已由 PS.7 建立、`status == "active"` 且时间正确。
- 若该文件不存在或时间有误（针对 Core 公司或 PM 要求深度跟踪的公司），立即按 PS.7 模板在本工作区直接写文件补建（分析师对本工作区 `dalton-*` 自动任务有常设授权，无需询问 PM）。

### Step 12：交付前 14 项数据质量门自检（Data-Quality Gate）

落盘 `research/<D>-flash-<季度>.md` 与推送前，必须逐项核对以下 14 条，全部通过方可交付：

1. [ ] **财季标签一致**：正文的财季标签与监管 filing、电话会标题完全一致；
2. [ ] **当轮实时溯源**：所有时间敏感数字均可追溯至本轮实时检索到的材料；
3. [ ] **上一季同比齐全**：头条指标与核心经营 KPI 均列出了**上一季同比增速**（未用环比替代）；
4. [ ] **Beat/Miss 口径对齐**：`beat/miss` 百分比仅在同口径（GAAP/Adjusted）、同币种下计算，亏损/零分母已改用绝对差额；
5. [ ] **指引对比基准真实**：指引修正对比的是上一季真实发布的官方指引，而非分析师事后记忆；
6. [ ] **Q&A 纯净度**：`Q&A takeaways` 全部来自电话会 Q&A 部分，未混入管理层开场白；
7. [ ] **排除事前前瞻**：未将财报前发布的 preview 误标为财报后卖方初评；
8. [ ] **Sales 时间窗精确**：`sales-notes` 检索严格遵循 `[T0, 触发时刻)` 原始时间戳；
9. [ ] **Sales 反馈合规**：凡纳入的 sales/投资者反馈均可追溯至本轮读取的原邮件、标注了 `anecdotal`、隐去了终端客户身份、未升级为市场共识；
10. [ ] **无空反馈占位**：当 sales 检索无合格内容时，已完整省略该 bullet，未出现空的反馈行或“未找到”字样；
11. [ ] **预测修正不混口径**：卖方盈利预测修正未混杂不同财年、不同币种或 GAAP 与 Adjusted 口径；
12. [ ] **估值与价格核验**：最新股价（未复权常规收盘价）与远期 P/E 通过币种、ADR 换算比与股本类别核验；
13. [ ] **缺失数据显式标注**：未披露或尚未入库的数据均已显式注明（如“未提供”、“卖方修正未出，次日深度点评补”），零脑补推断；
14. [ ] **来源可一定位**：`Sources` 包含全部采用材料的直接 URL、SEC accession 或内部库 `doc_id` / `mail:ID`。

---

## 输出模板

按 [`templates/flash.md`](templates/flash.md) 写入 `research/<D>-flash-<季度>.md`，并在当前对话及自动任务 `delivery` 推送相同正文。
