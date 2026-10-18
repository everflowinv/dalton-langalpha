# {TICKER} {FORM} 公告审阅笔记｜{YYYY-MM-DD}（`{accession}`）

**一句话结论**：{五词：无变化 / 强化 / 削弱 / 证伪 / 新论点（`NO_CHANGE` / `THESIS_STRENGTHENED` / `THESIS_WEAKENED` / `THESIS_BROKEN` / `NEW_THESIS`）}——{核心发现及对哪个 Thesis 支柱或 Debate 的影响}
- **表单类型**：`{FORM}`（报告期：{report_date}｜提交日期：{filing_date}）
- **Accession 号**：`{accession}`（唤醒信号 ID：`{signal_id}`）

## 一、关键变化（带章节 / 页码 / Item 号）
1. **{Item 号或章节名，如 Item 2 MD&A / Note 12 Commitments}**：
   - {具体披露事实与数字，注明期间、单位、口径，并与上一期或此前 8-K 快报对比}
2. **{Item 1A 风险因素 Diff（10-Q / 10-K 必填）}**：
   - {逐条列出新增、删除或措辞实质升级的风险条款；若两期全文对比无变化写“风险因素无变化”；若未能读取上一期 Item 1A 全文，必须写“本轮未取得上一期全文”}
3. **{资本结构 / 净现金 / 股本与回购 / RPO（如适用）}**：
   - {最新稀释股本、回购执行额、RPO 余额，或融资后的 Pro-forma 基础净现金与严格净现金}

## 二、对 Thesis 支柱与 Debate 的影响
- **关联支柱 / Debate**：{如 P1 / D3}
- **影响评估**：{确认 / 强化 / 削弱 / 证伪 / 无实质影响，附具体定量依据}
- **知识层更新**：
  - `verdicts.md`：已写入 `{五词}`（`trigger.kind="filing"`, `trigger.ref="{signal_id}"`）
  - `thesis` / `debate` / `drivers` / `management`：{说明已更新的字段与读数，或“无需修改”}

## 三、后续跟进事项（已写入 Agenda）
- **下一项验证（`next_validation`）**：{具体待验证事项，如“根据 10-Q 分部折旧与 Capex 附注更新财务模型 vN”}（截止时间：`{YYYY-MM-DD}`）
