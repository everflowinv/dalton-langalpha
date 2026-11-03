# Conviction Call 六条硬性证据门禁（6 Hard Evidence Gates）

在生成任何正式 Conviction Call（高确信度投资建议）之前，必须逐一核验以下 **6 条硬性门禁**。**只有 6 条全部 `PASS`（通过）才允许输出正式推荐；只要有任意 1 条为 `FAIL`（未通过），必须立即停止高确信推荐写作，改用《门禁未通过声明模板》向 PM 如实汇报缺口，附上已完成的赔率卡与三层保护，并将降级后的 `actionability`（行动分类）与完成的赔率卡写入 `thesis`（`underwriting` + `house_view`）。**

---

## Gate 1：基础覆盖与模型时效性（Coverage & Model Recency）
- **通过标准**：
  - 已完成完整首次深度覆盖（`dalton-full-coverage`），**或者**已完成快速覆盖 + 最近一次季报校准（`dalton-earnings-deep`）；
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

## Gate 3：可量化的预期差或倍数 regime 切换（双轨：Path A 盈利预期差 / Path B 倍数 regime）
- **通过标准（满足路径 A 或路径 B 之一）**：
  - **路径 A（盈利预期差）**：
    1. 在至少 **1 个核心经营驱动变量（Driver）** 上，本基金预测（House Case）与市场一致预期（Street Consensus）存在明确的**数字化差异**（可直接引用财务模型 `Street vs House` 工作表或近期多空辩论报告 §4）；
    2. 能够清楚解释**为什么市场错误定价至今仍存**（如：线性惯性外推、机构持仓拥挤/被迫调仓、旧叙事锚定、近因偏差、分部披露口径掩盖或卖方覆盖盲区）。
  - **路径 B（倍数 regime 切换，五条条件必须全部满足）**：
    1. 点名是**增长持续期、资本强度 / 现金转化、资本配置、尾部折价**四类中的哪一类发生变化，并给出可观测的领先指标；
    2. 给出**同一份盈利预测下**新旧估值倍数的价格桥，盈利变动贡献与估值倍数变动贡献分开列示；
    3. 给出**历史或同业估值倍数锚，并说明为什么适用于当前环境**；
    4. 存在 **1–4 个季度内**可观测的证伪或去折价催化剂节点与量化阈值；
    5. **同一原因不能在 EPS 和估值倍数里各算一次**（此外当估值倍数变动对目标回报贡献超过 50% 时，除非重估机制已在当期或近期现金流中实际兑现〔`thesis.underwriting.rerating_observed=true` 且附 `rerating_sources`〕，否则 `conviction` 最高只能标 `medium`）。
- **不通过（FAIL）情形**：我们的营收/EPS 预测与卖方共识几乎一致，且既无满足路径 B 五条条件的切换机制，仅靠“给更高估值倍数”或**“估值终将回到历史均值”**来推导出上涨空间（“回到历史均值”一律判 `FAIL`）。

---

## Gate 4：可观测、带时间窗口的验证路径（Time-Bound Validation Catalysts）
- **通过标准**：
  - 未来 **1–4 个季度内**存在明确、可观测、带具体时间窗口的催化剂事件（如特定季报分部披露、新品量产交付节点、客户合同续签、监管裁决日、行业高频跟踪数据拐点），能使市场验证我们的预期差。
- **不通过（FAIL）情形**：“价值终将回归”但未来 12 个月内找不到任何触发市场重估的具体检验事件。

---

## Gate 5：赔率裁决达标且下行受控（Breakeven Verdict & High Conviction Paths）
- **通过标准（直接取 `knowledge_update(doc="thesis", dry_run=True)` 返回的赔率计算摘要，严禁心算）**：
  - 所主张方向的 `thesis.underwriting.breakeven_verdict` 中每条 `verdict` 为 **`"clearly_above"`（明显高于所需胜率）**；
  - 不利方向价带端点相对基准股价的亏损幅度在上限之内（`act_now` 默认 ≤ 25%，即 `max_adverse_move_act_now`；多头看 `bear` 价带下沿，空头看 `bull` 价带上沿且须同时具备逼空压力情景 `underwriting.stress` 与已知借券成本 `underwriting.borrow_cost_annual`）；
  - 若给出 `conviction = "high"`（高确信），还必须满足以下两条路径之一（且估值倍数变动占目标回报比例 > 50% 时重估机制必须已在现金流中兑现，即 `underwriting.rerating_observed=true` 并附 `rerating_sources`）：
    1. **路径 ①（有来源支撑的权重区间整体达标）**：`underwriting.weight_bounds` 有历史基率或二元事件来源支撑（`rationale` 不得为风险中性期权隐含概率口径），且赔率计算摘要显示**全部权重组合（含对所主张方向最不利的那组权重）期望终值都越过回报门槛**；
    2. **路径 ②（不需要乐观情景兑现即可达标）**：在两行固定测试权重（`b = 25%` 和 `b = 50%`）下，赔率计算摘要显示**价格区间下端（多头）或上端（空头）也无需 bull/bear 即可达标**，且满足立即行动的不利波动上限。
- **不通过（FAIL）情形**：`breakeven_verdict[].verdict` 不是 `"clearly_above"`（如 `"around"` / `"below"` / `"cannot_judge"`），或不利方向波动超过 25% 上限，或拟给 `high` 确信却不满足上述两条路径之一。

---

## Gate 6：主驱动支柱完好且受损支柱已全额扣减（Primary Pillar Intact & Impaired Pillars Deducted）
- **通过标准**：
  - 近 **30 天**的 `verdicts.md` 中**不存在未处理的 `THESIS_BROKEN` 结论**；
  - `data/thesis.json` 中支撑本次推荐的**主驱动支柱（primary driving pillar）不得处于 `impaired`（受损）或 `broken`（证伪）状态**；
  - **其他非主驱动支柱若处于 `impaired` 状态，允许通过**，前提是已在三情景估值中**全额扣减**其受损影响，且**在过去 30 天内已经重估过**。
- **不通过（FAIL）情形**：近 30 天存在未处理的 `THESIS_BROKEN`、主驱动支柱处于 `impaired` 或 `broken` 状态、或存在未在估值中全额扣减／过去 30 天内未重估过的 `impaired` 支柱。

---

## 门禁核验表格式（报告必附）

| 门禁编号 | 门禁名称 | 本次核验事实与引用位置 | 状态（PASS/FAIL） |
|---|---|---|---|
| **Gate 1** | 覆盖与模型时效（≤120天） | <最新模型与报告日期、文件名> | PASS / FAIL |
| **Gate 2** | 核心支柱双源与证伪阈值 | <支柱 ID、独立来源数、证据等级> | PASS / FAIL |
| **Gate 3** | 预期差双轨（Path A 驱动差值 / Path B 倍数切换五条件） | <Driver 差值及模型单元格，或 Path B 五条核验> | PASS / FAIL |
| **Gate 4** | 可观测验证催化剂与时间窗 | <催化剂事件与具体季度/日期> | PASS / FAIL |
| **Gate 5** | 赔率裁决 `clearly_above`、不利波动 ≤25% 及高确信路径 | <`knowledge_update` 赔率摘要：所需胜率、不利波动、高确信路径> | PASS / FAIL |
| **Gate 6** | 主驱动支柱完好（其他 `impaired` 支柱已全额扣减且近 30 天已重估） | <核查近 30 天 verdicts.md 与 thesis.json 支柱状态> | PASS / FAIL |
