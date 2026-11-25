# 合理性检查、一致预期对账与 `Checks` 交付规则（`sanity-check.md`）

本文规定市场一致预期与卖方预测的取数方法、按指标与期间的数据有效性核验、可比期间重大差异归因四步法、判断理由在工作簿内的写入位置、`Checks` 工作表规范，以及脚本检查警告与退出码的统一交付规则。

---

## 1. 市场一致预期与卖方预测取数规则

<!-- DALTON_CAP:researchdata_bridge -->
1. **市场一致预期（按指标与期间提取有效值）**：
   - 美股与港股调用 `researchdata.finance(route="estimates", operation="consensus", ticker=...)`。注意返回的 `field_status` 是按指标给出的（如 `revenue_estimate`、`earnings_estimate`），顶层 `status == "partial"` 可能仅由行情、目标价或反推模型缺项引起：先读取各指标的 `field_status`，再对目标期间（`0q` 当季、`+1q` 下季、`0y` 本财年、`+1y` 下一财年）逐期检查数值是否有效，仅跳过实际缺值的指标或期间，其余有效收入与每股收益（`EPS`）均值、高低区间与分析师人数照常采用。
   - 同步可调用公开工具 `yf_analysis.get_revenue_estimates(ticker=...)` 与 `yf_analysis.get_earnings_estimates(ticker=...)` 核实期间截止日 `period_end` 与 `EPS` 计算口径 `methodology`（`gaap` 或 `normalized`；`methodology` 仅由 `get_earnings_estimates` 返回，收入预期记录不含该字段，收入偏离在核实财季与币种后照常计算）。当环境已配置 `FMP`（Financial Modeling Prep，金融数据服务商）密钥时，调用 `fundamentals.get_analyst_estimates(symbol=..., period="annual"|"quarter")` 补充多期收入、息税折旧摊销前利润（`EBITDA`）、息税前利润（`EBIT`）、净利润与 `EPS` 一致预期。
   - **期间字段按来源区分**：Yahoo 返回的 `period_end` 可能缺省，FMP 使用 `date` 字段表示期末日期；遇到期间字段缺省且无法由公司财报日历唯一核实时，仅对该期标注「期间无法核实」并留空偏离列，保留其余已核实期间，避免丢弃整个指标。
   - **`implied_model` 口径标注**：返回中的 `implied_model` 是由一致预期收入、`EPS`、稀释股数与税率反推的利润，引用时标注「由一致预期 `EPS` 折算」，区别于营业利润或净利润的市场一致预期。A 股标的转查下方卖方报告或公开一致预期汇总。
   - 每次取数将原始返回保存为快照 `data/consensus/<YYYY-MM-DD_HHMM>.json`，明确区分三类状态：**取数失败**（接口报错或超时，写明失败原因）、**覆盖不足**（接口正常返回但该指标/期间为空或分析师人数为 0，外部预期格留空并标「无公开一致预期」）、**期间或会计口径无法核实**（列示原始参考值并标注「期间或会计口径无法核实」，留空偏离幅度）。
<!-- DALTON_CAP_ELSE -->
1. **市场一致预期（公开市场数据与可选 `FMP` 接口）**：
   - 调用公开工具 `yf_analysis.get_revenue_estimates(ticker=...)` 与 `yf_analysis.get_earnings_estimates(ticker=...)`，获取 `0q`（当季）、`+1q`（下季）、`0y`（本财年）、`+1y`（下一财年）的收入与每股收益（`EPS`）一致预期均值、高低区间与分析师人数。其中 `methodology`（`gaap` 或 `normalized`）仅由 `get_earnings_estimates` 返回，收入预期记录不含该字段（核实财季与币种后照常计算收入偏离），并留意 `year_ago_stale` 标记。
   - 当环境已配置 `FMP`（Financial Modeling Prep，金融数据服务商）密钥使 `fundamentals.get_analyst_estimates(symbol=..., period="annual"|"quarter")` 可用时，同步调用补充多期收入、息税折旧摊销前利润（`EBITDA`）、息税前利润（`EBIT`）、净利润与 `EPS` 一致预期。
   - **逐期核验有效值与期间字段**：读取指标返回后逐期检查数值是否有效；期间字段按来源区分——Yahoo 的 `period_end` 可能缺省，FMP 使用 `date` 字段。若某期缺少期间日期，先对照公司财报日历核实；仍无法唯一确认时，仅对该期标注「期间无法核实」并留空偏离列，保留其余已核实期间，避免丢弃整个指标。
   - 每次取数将原始返回保存为快照 `data/consensus/<YYYY-MM-DD_HHMM>.json`，明确区分三类状态：**取数失败**（接口报错或超时，写明失败原因）、**覆盖不足**（接口正常返回但该指标/期间为空或分析师人数为 0，外部预期格留空并标「无公开一致预期」）、**期间或会计口径无法核实**（列示原始参考值并标注「期间或会计口径无法核实」，留空偏离幅度）。
<!-- /DALTON_CAP -->

<!-- DALTON_CAP:researchdata_bridge,alphaengine -->
2. **卖方报告（营业利润、净利润、分部拆解与远期预测）**：调用 `researchdata.research(route="alphaengine", ...)` 检索最近一个财报季后 3–5 家覆盖机构的深度与业绩点评报告，提取各分部收入与利润率、集团营业利润、净利润及 `+2y`（如 `2028E`）远期预测数字与核心假设；报告未覆盖的远期或细分项标注「无公开一致预期」。
<!-- DALTON_CAP_ELSE -->
2. **卖方与公开预期参照（营业利润、净利润、分部拆解与远期预测）**：优先读取用户放入工作区 `sources/` 的参考报告，结合公司官方投资者关系（`IR`〔Investor Relations〕）网站披露的一致预期汇总、公开财报电话会问答纪要，以及通过 `web_search` / `web_fetch` 检索到的公开机构业绩点评摘要，提取分部、营业利润、净利润与远期预测参考值；公开渠道未披露的期间或分部标注「无公开一致预期」。
<!-- /DALTON_CAP -->

**来源标注纪律**：在工作簿对照表注明数据源、提取日期与样本数（市场一致预期标如 `<Yahoo Finance 2026-11-25, N=34`，单家卖方标如 `<Morgan Stanley 2026-11-18`），明确区分市场一致预期（`consensus`）、单家卖方预测（`broker estimate`）、由一致预期 `EPS` 折算值、公司法定披露（`fact`）与本方假设（`assumption`）。

---

## 2. 可比期间重大差异归因四步法

对每一个已完成口径核实的预测期间（`0q`、`+1q`、`0y`、`+1y`、`+2y`）和可比核心指标（收入、分部收入、营业利润、净利润、`EPS`/`EPADS`）评估差异。定量筛查参考值：**收入偏离约 ±3% 以上，或营业利润 / 净利润 / `EPS` 偏离约 ±5% 以上，或微利转型公司营业利润率偏离约 ±150 bps（基点，1 个百分点 = 100 基点）以上**（可按公司波动率与估值敏感度调整）：

1. **第一步：先核实五项基础口径，再算偏离**
   依次核对并换算至同一基准后才计算偏离百分比（任一项无法确认时标注「期间或会计口径无法核实」并留空偏离列）：
   - 财年截止日与财季归属（核对 `period_end` / `date` 与模型财年截止月一致）；
   - 报表本位币与股票交易币种及折算汇率；
   - 会计准则与调整项（`GAAP` / 国际财务报告准则 `IFRS` 与 `Non-GAAP` / `Normalized` 调整范围）；
   - 每股普通股与每份 `ADS` 换算比；
   - 持续经营与近期并表/剥离范围。
2. **第二步：有来源支撑的逐层归因与未解释差额单列**
   表格中区分对比基准是「市场一致预期均值（`Consensus`）」还是「具体卖方机构预测（`Broker Estimate`）」。具备卖方分部模型或多指标一致预期时，按链路量化收入规模、利润率、未分配总部项、净利息、有效税率与稀释股数的各自贡献；当公开数据仅有总收入与 `EPS` 时，只量化有数据支撑的项目，将其余无法拆细的部分如实列为「未解释差额（`Unexplained Variance`：公开一致预期未披露分部/费用明细）」。
3. **第三步：修正建模疏漏或确认独立观点（`contrarian view`）**
   若差异源于漏计总部费用、单季偶发项外推、期初现金重复计息、回购漏扣现金流或加总年残留旧假设，立即修正公式；非核心科目缺少一手证据支撑时回调至历史稳态中枢；确由核心业务先行指标、高频跟踪数据或管理层最新资本配置计划支撑时，确认为合理的主动独立观点并保留。
4. **第四步：在工作簿内写明具体理由**
   对所有保留的重大偏离写明具体业务驱动、量化差值与证据出处：
   - **存量模型**：主表位写入各公司 `Valuation - <TICKER>` 或 `Summary` 页下方的 `Consensus / Broker vs House` 对照表（无独立估值/汇总页时置于主表下方或独立对照表，A 列涂 `99CCFF` 分节条），并在 `Model - <TICKER>` / `Key Metrics - <TICKER>` 对应驱动行的最右侧红字备注列写旁注；行业多公司模型同步在首页 `README & Changelog` 附差异摘要。
   - **从零新建单公司模型**：写入独立工作表 `Street vs House`。
   - 对照表列结构：`指标 | 期间 | House 预测 | 外部预期值 | 基准类型 | 差异（% 或 bps）| 已证实归因与未解释差额 | 坚持独立观点的具体理由与证据出处 | 外部来源、日期与状态说明`；`House 预测` 列用公式引用计算页，`外部预期值` 列用蓝字输入，可比期间 `差异` 列写斜体公式。

---

## 3. `Checks` 工作表规格与独立核算要求

每本交付模型必须包含 `Checks` 工作表（第 1 行为列名表头，第 2–9 行为八项标准检查，第 10 行为 `Overall`），四列结构为：`A 列：check_id | B 列：检查内容、期间与参与单元格范围 | C 列：动态状态公式 | D 列：最大绝对差额公式及 WARN/N/A 原因说明`。

| 行号 | `check_id` | 检查内容 | 允许状态值 |
|---|---|---|---|
| 2 | `annual_vs_quarters` | 年度与四财季流量加总及期末时点余额一致性（近期历史 + 全部加总预测年） | `OK` / `WARN` / `FAIL` |
| 3 | `segment_tieout` | 分部与集团收入、营业利润恒等（单业务无分部公司可填 `="N/A"` 并在 D 列注明） | `OK` / `WARN` / `FAIL` / `N/A` |
| 4 | `cash_rollforward` | 预测期期末现金滚存、跨期期初衔接与资产负债表现金一致性 | `OK` / `WARN` / `FAIL` |
| 5 | `debt_rollforward` | 预测期有息债务滚存及跨期衔接（零有息债或未单列债务滚存公司可填 `="N/A"` 并在 D 列注明） | `OK` / `WARN` / `FAIL` / `N/A` |
| 6 | `share_rollforward` | 预测期期末股数滚存、回购现金流引用与 `EPS` 加权稀释股数分母 | `OK` / `WARN` / `FAIL` |
| 7 | `bs_balance` | 完整资产负债表每期 `|总资产 − (总负债 + 股东权益)| < 0.05`（精简滚存模型可填 `="N/A"` 并在 D 列注明） | `OK` / `WARN` / `FAIL` / `N/A` |
| 8 | `implied_rates` | 预测期隐含存款收益率与借款利率对照本公司历史区间（默认警告带 ±150 bps；`<0%` 或 `>20%` 排查单位错配） | `OK` / `WARN` / `FAIL` |
| 9 | `summary_links` | 汇总表收入与 `EPS` 年度及季度序列与主表零差额 | `OK` / `WARN` / `FAIL` |
| 10 | `Overall` | **仅汇总 `Checks` 工作表 C2:C9 的八项状态**：`=IF(COUNTIF(C2:C9,"FAIL")>0,"FAIL",IF(COUNTIF(C2:C9,"WARN")>0,"WARN","OK"))` | `OK` / `WARN` / `FAIL` |

**`Checks` 公式纪律**：C2:C9 必须写引用真实模型单元格的动态比较公式（如 `=IF(ABS(Model!G10-SUM(Model!H10:K10))<0.5,"OK","FAIL")`），合法豁免项写 `="N/A"` 并在 D 列写明原因；写死静态文本或未绑定真实单元格的占位公式（如 `=IF(1,"OK","FAIL")`）会被 `style_audit.py` 直接拦截。B 列写明参与核对的单元格范围，脚本会对登记的单元格独立重算差额复核。

---

## 4. `Checks!Overall`、脚本 `warn` 警告与退出码交付判定

1. **`Checks!Overall` 与脚本 `warn` 职责分离**：
   - `Checks!C10`（`Overall`）**只汇总 `Checks` 工作表 `C2:C9` 的八项工作表检查状态**；
   - `style_audit.py` 自身检出的 `warn` 级问题（例如 `font_family`、`implied_rate_diagnostics`、`layout_and_grouping`、`hidden_sheet_referenced`、`ambiguous_cell_role`）会在输出 JSON 的 `findings` 与独立数组 `script_warnings` 中单独列出，并标明每一项对应的说明位置（`explanation_location`）及是否已附说明（`explained`）。
2. **说明写入位置**：
   - `implied_rate_diagnostics` 警告写在 `Checks!D8`（`implied_rates` 行 D 列）或 `README & Changelog`；
   - 其余脚本 `warn` 项写在 `Checks` 对应行 D 列或 `README & Changelog` 说明区（提及对应检查项代号与业务原因），亦可在离线审查时通过 `--warn-notes <notes.json>` 传入核验。
3. **退出码与交付判定标准**：
   - **退出码 `1`（阻断交付）**：参数错误或文件损坏；检出任一 `fail` 级问题（含 `--strict` 提升为 `fail` 的样式/汇总项、`Checks` 页出现 `"FAIL"`、缺必检行、缓存为空、假公式汇总、`"WARN"`/`"N/A"` 缺少 D 列说明、对非豁免项填 `"N/A"`）；或在 `--strict` 重算后交付检查中存在**缺少说明的脚本 `warn` 项**（`unexplained_script_warning`，返回 `delivery_decision: "block_missing_warning_explanation"`）。
   - **退出码 `0`（允许交付）**：零 `fail` 项，且在 `--strict` 重算后模式下所有 `Checks` 工作表 `"WARN"`/`"N/A"` 行及所有脚本 `warn` 项均已写明具体业务原因（此时 `status == "pass"`、`delivery_decision == "pass"`）。在非 `--strict` 模式或 `--static-only` 预检模式下，缺少说明的脚本 `warn` 作为提示输出，若零 `fail` 项则返回退出码 `0`（`delivery_decision == "pass_with_advisory_warnings"`）。
