# Conviction Call 六条硬性证据门禁（6 Hard Evidence Gates）

在生成任何正式 Conviction Call 之前，必须逐一核验以下 **6 条硬性门禁**。**只有 6 条全部 `PASS` 才允许输出正式推荐；只要有任意 1 条为 `FAIL`，必须立即终止推荐写作，改用《暂无高 conviction 声明模板》诚实向 PM 汇报缺口。**

---

## Gate 1：基础覆盖与模型时效性（Coverage & Model Recency）
- **通过标准**：
  - 已完成完整首次深度覆盖（PS.6 `dalton-initiate-full`），**或者**已完成快速覆盖 + 最近一次季报校准（PS.9 `dalton-earnings-review`）；
  - 且上述基准报告与财务模型更新距今 **不超过 120 天**。
- **不通过（FAIL）情形**：仅做过快速筛选（Quick Scan）、尚无三表/驱动模型、或最近一次财报发布后尚未完成模型校准。

---

## Gate 2：核心支柱证据强度与双源印证（Core Pillar Evidence Quality）
- **通过标准**：
  - `data/thesis.json` 中支撑本次 Call 的每一条核心支柱（Core Pillars），均具备 **≥2 个独立来源**支撑（注：同一份公司指引或同一篇新闻被多家卖方转述，只算 **1 个**来源）；
  - 每条支撑支柱的证据质量评级 **≥ `"中"`**（不允许以 `"低"` 或纯假设支柱作为 Call 的主心骨）；
  - 每条支柱均已设定量化的**确认阈值（Confirm）、预警阈值（Warn）与证伪阈值（Falsify）**。
- **不通过（FAIL）情形**：核心多头/空头逻辑完全依赖管理层单方口头承诺、单一匿名渠道传闻、或缺乏可量化的证伪边界。

---

## Gate 3：可量化的预期差与错误定价机制（Quantifiable Variant Perception）
- **通过标准**：
  - 在至少 **1 个核心经营驱动变量（Driver）** 上，本基金预测（House Case）与市场一致预期（Street Consensus）存在明确的**数字化差异**（可直接引用财务模型 `Street vs House` 工作表或近期多空辩论报告 §4）；
  - 能够清楚解释**为什么市场错误定价至今仍存**（如：线性惯性外推、机构持仓拥挤/被迫调仓、旧叙事锚定、近因偏差、分部披露口径掩盖或卖方覆盖盲区）。
- **不通过（FAIL）情形**：我们的营收/EPS 预测与卖方共识几乎一致，仅靠“给更高估值倍数”来推导出上涨空间。

---

## Gate 4：可观测、带时间窗口的验证路径（Time-Bound Validation Catalysts）
- **通过标准**：
  - 未来 **1–4 个季度内**存在明确、可观测、带具体时间窗口的催化剂事件（如特定季报分部披露、新品量产交付节点、客户合同续签、监管裁决日、行业高频跟踪数据拐点），能使市场验证我们的预期差。
- **不通过（FAIL）情形**：“价值终将回归”但未来 12 个月内找不到任何触发市场重估的具体检验事件。

---

## Gate 5：概率加权 IRR 超越门槛且下行可承受（Hurdle Rate & Downside Asymmetry）
- **通过标准**：
  - 经 `scenario_irr.py` 计算，Bear / Base / Bull 三档情景的**概率加权年化 IRR 超过基金门槛收益率（默认 Hurdle Rate = 15.0%）**；
  - 且 Bear 情景下的最大回撤（Max Drawdown）与拟建议的仓位级别（`Core` / `Tactical` / `Small`）相匹配、风险可承受。
- **不通过（FAIL）情形**：概率加权年化 IRR ≤ 15%，或下行亏损幅度过大（负偏态）而无法通过期权/配对有效对冲。

---

## Gate 6：近 30 天无未处理的逻辑破产或受损支柱（No Unhandled Broken/Impaired Thesis）
- **通过标准**：
  - 近 **30 天**的 `verdicts.md` 中**不存在未处理的 `THESIS_BROKEN` 结论**；
  - `data/thesis.json` 中支撑本次方向的支柱**不存在处于 `impaired`（受损未修复）状态**的条目。
- **不通过（FAIL）情形**：近期季报或公告刚触发 `THESIS_WEAKENED` / `THESIS_BROKEN` 且尚未完成多空重辩（Bull/Bear）与逻辑重建。

---

## 门禁核验表格式（报告必附）

| 门禁编号 | 门禁名称 | 本次核验事实与引用位置 | 状态（PASS/FAIL） |
|---|---|---|---|
| **Gate 1** | 覆盖与模型时效（≤120天） | <最新模型与报告日期、文件名> | PASS / FAIL |
| **Gate 2** | 核心支柱双源与证伪阈值 | <支柱 ID、独立来源数、证据等级> | PASS / FAIL |
| **Gate 3** | 驱动变量数字预期差 | <Driver 名称：House vs Street 差值及模型单元格> | PASS / FAIL |
| **Gate 4** | 可观测验证催化剂与时间窗 | <催化剂事件与具体季度/日期> | PASS / FAIL |
| **Gate 5** | 概率加权 IRR > 15% 且下行可控 | <加权 IRR 数值、Bear 回撤幅度> | PASS / FAIL |
| **Gate 6** | 近 30 天无未处理受损支柱 | <核查近 30 天 verdicts.md 与 thesis.json 状态> | PASS / FAIL |
