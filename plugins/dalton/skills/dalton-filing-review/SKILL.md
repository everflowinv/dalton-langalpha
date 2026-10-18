---
name: dalton-filing-review
description: "覆盖公司 SEC 公告审阅（公告监控响应）：被 watch 预检的 sec 探针通过 <dalton_wake> 唤醒（或手动指定 accession）后，按 form-guide.md 审阅 10-Q/10-K、8-K、融资文件（S-3/424B）、DEF 14A、13D/13G 与 Form 4，对照 thesis/debate 给出事件五词结论，写事件笔记并更新知识层。触发词：公告审阅、filing review、10-Q 审阅、10-K 审阅、8-K 审阅、/dalton-filing-review。"
---

# Dalton 公司公告审阅（`dalton-filing-review`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

## 目标与触发方式

覆盖公司一有新 SEC 公告，对应公司的分析师即被唤醒读公告、核对数字与措辞变化、判断对 thesis 与 debate 的影响并给出事件五词结论，不靠人工提醒：
- **自动唤醒**：由 `dalton-<t>-watch.json` 配合后端 PK.16 的 `sec` 探针发现新公告，注入 `<dalton_wake>`（内含 `kind="filing"`、信号 `id`、表单类型与 `accession` 号）。
- **手动调用**：`/dalton-filing-review {TICKER} {FORM} {ACCESSION}`（若未传 accession，先调 `dalton_sec.list_filings` 取最新一份该表单）。
- **来源阶梯**：严格遵循 `10-Q / 10-K > 8-K 附件 > 新闻稿` 的一手权威优先级。
- **Watch 表单配置初始化**：若读取 `agenda.md`（`data/agenda.json`）发现 `watch.filings.forms` 尚未设置，按 `.agents/skills/dalton-filing-review/references/watch-forms.md` 的推荐配置调用 `knowledge_update(doc="agenda", ...)` 写入。

---

## 五步审阅流程

### 第 1 步：财报撞车预检与例行表单分流

1. **检查当天是否已有财报结论**：
   - 先读 `verdicts.md` 当天是否已有 `trigger.kind="earnings"` 的结论（财报日 8-K Item 2.02 常与 `/dalton-earnings-flash` 快讯同时或先后触发）。
   - **若已有快讯结论**：本步不重做快讯，只打开 8-K 附件（EX-99.1）核对快讯里的关键数字（收入、EPS、指引、股本）与正式 8-K 附件是否完全一致。完全一致则写一条 `NO_CHANGE`（`progress="none"`，`trigger.ref` 填本次 filing 信号 id）；若有差异或 8-K 包含其他非业绩 Item（如 Item 1.01、2.05、5.02），则针对差异或额外 Item 继续审阅。
2. **例行文件（如 `FWP` 自由书写招股书）**：
   - 按 `references/watch-forms.md` 规则，不深读正文，仅记录备案并写 `NO_CHANGE`（`progress="none"`），不推送 PM。

### 第 2 步：按 `form-guide.md` 逐项审阅公告要点

先读取 `.agents/skills/dalton-filing-review/references/form-guide.md`，再用 `researchdata.finance(route="sec", ...)`（`read-item` / `search-text` / `attachments`）或 `get_sec_filing` 读取正文，用 `dalton_sec.get_company_facts` 拉取结构化 XBRL 事实：

1. **10-Q / 10-K（及外国发行人 20-F）**：
   - **MD&A 措辞变化**：与上一期对比增长驱动、利润率解释、流动性与资本开支（Capex）指引措辞的变化；
   - **风险因素（Item 1A）逐条 diff**：对比上一期 10-Q/10-K 的风险因素章节，列出新增、删除或实质改写的风险条目。**硬性纪律**：若因工具未取到上一期 Item 1A 全文而无法对比，必须如实写明**“本轮未取得上一期全文”**，**严禁在未对比的情况下写“风险因素无变化”**；只有两期全文均已读取并逐条核对无新增/实质改写时，才写“风险因素无变化”；
   - **分部与会计政策**：分部报告口径调整、收入确认或折旧年限等会计估计变化；
   - **或有事项（Commitments & Contingencies）**：未决诉讼、监管调查、赔偿计提区间变化；
   - **回购、股本与 RPO/递延收入**：期末及期后最新普通股股数、回购执行金额与剩余授权、RPO（剩余履约义务）与递延收入增速；
   - **XBRL 数字入库与模型联动**：新季度财务数字用 `dalton_sec.get_company_facts` 提取（严格遵守：单季 Q 只配约 90 天的 3 个月口径，Q4 = FY − 9M 标 `derived_filing_actual`），更新到 `drivers` 的最新读数；若该公司已有财务模型（`model/<TICKER>-model-v*.xlsx`），在 `agenda` 中新增“10-Q/10-K 发布后模型待更新”验证项（或按 `model-update` 生成映射表交 PM 决定是否写入新版本）。
2. **8-K（及 6-K）**：
   - 按具体 Item 号审阅：
     - `Item 1.01 / 1.02`：签署或终止重大最终协议（交易对手、金额、期限、约束条款）；
     - `Item 2.01`：完成重大资产收购或剥离；
     - `Item 2.02`：经营业绩与财务状况（见第 1 步撞车规则）；
     - `Item 2.03`：新增重大直接财务义务或表外安排；
     - `Item 2.05 / 2.06`：退出/处置活动成本（裁员重组）与重大资产减值（现金 vs 非现金部分、预计节约金额）；
     - `Item 4.01 / 4.02`：更换审计师或**历史财报不可依赖（Non-Reliance，最高级别治理警报）**；
     - `Item 5.02`：董事或核心高管（CEO/CFO/业务负责人）离任、新任或薪酬计划变更；
     - `Item 7.01 / 8.01`：Reg FD 披露或其他重大事项（如投资者日材料、诉讼和解、回购授权）。
3. **融资类（S-1 / S-3 / S-3ASR / 424B2 / 424B5）**：
   - 提取发行品种（普通股/可转债/优先债）、发行规模、发行价或票面利率与到期日、潜在摊薄比例、募集资金用途；
   - **重算净现金**：调用 `.agents/skills/dalton-financial-lenses/SKILL.md` 的双口径规则，按融资后 pro-forma 重算一次基础净现金与严格净现金。
4. **DEF 14A（委托书 / Proxy Statement）**：
   - 高管短期/长期激励挂钩的财务与经营 KPI 及权重变化、SBC 年化稀释率、关联交易与关键股东提案。
5. **SC 13D / 13G 与 Form 4**：
   - 申报人身份（激进投资人 / 财务机构 / CEO / CFO / 董事）、交易方向、股数与金额、交易后持股比例、Form 4 脚注是否注明属于预先设立的 `Rule 10b5-1` 自动交易计划。

### 第 3 步：对照 Thesis 与 Debate 判断影响

按 `.agents/skills/dalton-knowledge/SKILL.md` 读取本工作区 `thesis.md`（核心支柱与确认/警告/证伪阈值）和 `debate.md`：
- 判断本次公告事实是否触及某条支柱或某条 debate；
- 选定事件结论五词之一：`NO_CHANGE`（无变化）、`THESIS_STRENGTHENED`（强化）、`THESIS_WEAKENED`（削弱）、`THESIS_BROKEN`（证伪）、`NEW_THESIS`（新论点）。

### 第 4 步：输出公告审阅事件笔记

按 `.agents/skills/dalton-filing-review/templates/filing-review.md` 将审阅结果写入：
`research/<YYYY-MM-DD>-filing-<form>-<accession短号>.md`
（其中 `<accession短号>` 取 accession 末尾 6 位数字或去横线后的短标识，`<form>` 如 `10-Q`、`8-K`、`424B2`）。
- 必须包含：一句话结论（含事件五词）、关键变化（逐条标明页码、小节名或 Item 号）、风险因素 diff 结论（10-Q/10-K 必填）、对 thesis/debate 的影响、需要跟进的事项。
- 若属重大融资、重组或会计重述等需正式归档的事项，可额外写入 `drafts/` 并调用 `publish_report(kind="event_note")` 提交审批。

### 第 5 步：收尾三步（知识层写入与 PM 推送）

1. **追加事件结论（必做）**：调用 `knowledge_append_verdict`：
   - `verdict.verdict`：五词正式词形；
   - `verdict.trigger.kind`：`"filing"`（手动无信号时填 `"manual"`）；
   - `verdict.trigger.ref`：**必须与 `<dalton_wake>` 里的信号 `id` 严格一致**（用于 PK 验收与防重核对）；
   - `verdict.evidence`：必须包含本次公告的 `accession` 号、表单类型与提交日期；
   - `verdict.next_validation`：下一项验证的内容与截止日期；
   - `agenda_ops`：把后续跟进事项（如“10-Q 发布后更新模型 vN”）写入议程。
2. **更新知识层**：
   - 当结论非 `NO_CHANGE`（或有新季度财务读数）时，调用 `knowledge_update` 更新 `thesis`、`debate` 或 `drivers`（写入 10-Q/10-K 最新季度读数或融资后严格净现金）；
   - 若公告中出现管理层表述变化（如风险因素新增实质条目、指引口径或薪酬考核指标调整），调用 `knowledge_update(doc="management", ...)` 记录。
3. **推送 PM**：
   - 非 `NO_CHANGE`（或虽维持原判但 10-Q/10-K 提供了重要新季度细节）时，向 PM 推送精炼摘要；纯例行无变化文件不打扰 PM。
