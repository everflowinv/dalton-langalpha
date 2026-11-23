# 四维做空初筛详细检查清单

执行 `dalton-short-screen` 时，以下四个维度及检查项必须逐项核查并给出具体数字、期间口径、accession number 或来源 URL。

---

## 维度 1：会计红旗分析（10 项必查）

**数据源**：最新 `10-K`（近 2 期）+ 最新 `10-Q`（近 3 期）；外国私人发行人用 `20-F` + `6-K`。

<!-- DALTON_CAP:researchdata_bridge -->```python
# 美股本土公司
researchdata.finance(route="sec", operation="list-filings", ticker="{TICKER}", form="10-K", limit=2)
researchdata.finance(route="sec", operation="list-filings", ticker="{TICKER}", form="10-Q", limit=3)

# 外国私人发行人（BABA, PDD, JD, TCOM, BIDU 等）
researchdata.finance(route="sec", operation="list-filings", ticker="{TICKER}", form="20-F", limit=2)
researchdata.finance(route="sec", operation="list-filings", ticker="{TICKER}", form="6-K", limit=3)

# 结构化三表与财务比率
researchdata.finance(route="sec", operation="financials", ticker="{TICKER}", form="10-K", limit=5, params={"statement": "all"})
researchdata.finance(route="sec", operation="ratios", ticker="{TICKER}")

# 按 Item 读正文（10-K Item 7 MD&A / Item 8 附注；10-Q Part I Item 2）
researchdata.finance(route="sec", operation="read-item", ticker="{TICKER}", form="10-K", item="7", params={"max_chars": 40000})
researchdata.finance(route="sec", operation="read-item", ticker="{TICKER}", form="10-Q", item="2", params={"max_chars": 40000})

# 全文关键词定位（可按需配 params 里的 window 与 max_matches）
researchdata.finance(route="sec", operation="search-text", ticker="{TICKER}", form="10-K", query="revenue recognition")
researchdata.finance(route="sec", operation="search-text", ticker="{TICKER}", form="10-K", query="capitalized")

# 自然语言结构化事实补充
researchdata.finance(route="sec", operation="smart-facts", ticker="{TICKER}", query="contract assets and contract liabilities last 3 years", limit=10)
```<!-- DALTON_CAP_ELSE -->```python
# 美股本土公司
dalton_sec.list_filings(ticker="{TICKER}", forms="10-K,10-Q", limit=10)

# 外国私人发行人（BABA, PDD, JD, TCOM, BIDU 等）
dalton_sec.list_filings(ticker="{TICKER}", forms="20-F,6-K", limit=10)

# 结构化三表与 XBRL 事实（MD&A 与附注正文按 accession 从 SEC EDGAR 原文核对）
dalton_sec.get_company_facts(ticker="{TICKER}")
```<!-- /DALTON_CAP -->

### 1. 收入确认政策
- 是否存在激进收入确认（bill-and-hold、channel stuffing 迹象、提前确认长期合同收入、高度波动的非经常性或事件驱动型收入）。
- 收入确认政策 YoY 是否有变更。
- 合同资产（contract assets）vs 合同负债（contract liabilities）增速与绝对值变化——合同资产快于收入膨胀、或合同负债缩水同时合同资产抬升是核心红旗。

### 2. 一次性 / 非经常性收益
- 资产出售收益、诉讼和解收益、保险赔付、政府补贴等非经常性收益明细。
- 上述收益占税前利润（pre-tax income）的比例。
- 是否用于掩盖主营业务收入或营业利润率的恶化。

### 3. 资本化 vs 费用化
- 研发支出资本化比例及近 2–3 年趋势（软件/平台型公司重点关注内部使用软件资本化）。
- 内部开发成本/获客成本（如递延客户转换成本）资本化金额 YoY 变化及与收入增速的剪刀差。
- 固定资产或无形资产折旧/摊销年限是否被延长。

### 4. 应收账款质量
- DSO（Days Sales Outstanding）近 2–3 年与近 4–8 季趋势，对比收入增速。
- 坏账准备（allowance for doubtful accounts）绝对额及占应收账款总余额比例的变化。
- 关联方应收账款（related-party receivables）规模与占比。

### 5. 存货质量（有存货的公司适用；纯服务业标「不适用」）
- DIO（Days Inventory Outstanding）趋势。
- 存货跌价/减值准备（inventory write-downs / obsolescence reserve）变化。
- 原材料（raw materials）、在制品（work-in-progress）、成品（finished goods）结构变化（成品堆积为重点红旗）。

### 6. 现金流与利润质差（复用 `dalton-financial-lenses`）
- **不重复手写公式**：调用 `.agents/skills/dalton-financial-lenses/SKILL.md` 中的 `earnings_quality.py` 计算：
  - `CFO / NI` 比率（并拉取近 4–8 个季度趋势）
  - Sloan 应计比率（总应计口径与经营应计口径）
  - 现金转换周期 `CCC`
- 结合现金流量表附注拆解经营现金流与净利润的主要差异项（营运资本变动、非现金减值、递延税、SBC 等）。
- 测算股权激励费用（SBC）加回前后的真实利润差异。

### 7. 自由裁量假设
- 商誉（Goodwill）占总资产比重及减值测试关键假设（折现率 WACC、长期增长率、终值增长率、fair value cushion 披露）。
- 长期资产减值触发条件与历史减值记录。
- 公允价值计量中 Level 3 资产/负债占比及估值输入变化。

### 8. 表外项目
- 经营租赁义务（ASC 842 / IFRS 16 后重点检查短期租赁、可变租赁及长期最低采购/云服务承诺）。
- 可变利益实体（VIE）结构与并表判断。
- 担保、履约承诺与未决或有负债（Commitments & Contingencies）。
- 非合并合营/联营企业投资（equity method investments）及隐含风险敞口。

### 9. 偿债能力与现金流健康度（复用 `dalton-financial-lenses`）
- **净现金/净负债双口径不重复手写公式**：调用 `.agents/skills/dalton-financial-lenses/SKILL.md` 中的 `netcash.py` 计算基础净现金（Definition A）、严格净现金（Definition B）、营运资本、其他应收/应付 >20% 预警及“大存大贷”标记。
- 补充以下杠杆与偿债指标（附 2–3 年趋势）：
  - `Net Debt / EBITDA`（与行业可比公司对比）
  - `FCF`（`CFO - CapEx`）近 2–3 年趋势及 `FCF Yield`（`FCF / Market Cap`）
  - `Interest Coverage`（`EBIT / Interest Expense`）
  - `Debt Maturity Profile`（短债 vs 长债结构、未来 1–2 年到期债务规模与再融资压力）
- 若 Net Debt 快速上升而 FCF 持续下滑，标为重大红旗。

### 10. 边际变化（最关键）
- 汇总第 1–9 项在最近 2–3 个报告期的变化趋势表格。
- 任何突然的会计政策或会计估计变更。
- 审计师是否更换、审计师连续服务年限、审计意见是否有保留或强调事项段（Critical Audit Matters）。

---

## 维度 2：高管薪酬与激励分析（`DEF 14A` / `20-F Item 6.B`）

**数据源**：最新 `DEF 14A`；外国私人发行人无 `DEF 14A` 时，查最新 `20-F` 的 `Item 6.B`（Compensation）。

<!-- DALTON_CAP:researchdata_bridge -->```python
researchdata.finance(route="sec", operation="list-filings", ticker="{TICKER}", form="DEF 14A", limit=2)
researchdata.finance(route="sec", operation="search-text", ticker="{TICKER}", form="DEF 14A", query="Summary Compensation Table")
researchdata.finance(route="sec", operation="search-text", ticker="{TICKER}", form="DEF 14A", query="threshold")
# 外国发行人备用（读取完整 Item 6，再在正文内查找 6.B Compensation 小节）
researchdata.finance(route="sec", operation="read-item", ticker="{TICKER}", form="20-F", item="6", params={"max_chars": 40000})
```<!-- DALTON_CAP_ELSE -->```python
dalton_sec.list_filings(ticker="{TICKER}", forms="DEF 14A", limit=2)
# 外国发行人备用（按 accession 读取 20-F Item 6.B Compensation 小节）
dalton_sec.list_filings(ticker="{TICKER}", forms="20-F", limit=2)
```<!-- /DALTON_CAP -->

### 必查项目
1. **CEO / CFO 总薪酬结构**：Base salary、Bonus / Non-equity incentive、Stock awards（PSUs/RSUs）、Option awards、其他薪酬及各项占比。
2. **绩效指标挂钩（必须完整提取，不可概括）**：
   - 短期激励（Annual Cash Incentive）挂钩的具体财务与非财务 KPI（Revenue、EBITDA、EPS、FCF 等）。
   - 长期激励（Long-Term Equity Incentives）挂钩指标（相对 TSR、Revenue CAGR、EPS growth 等）。
   - **完整条款**：每个指标的 `threshold`、`target`、`maximum` 数值及对应 payout 倍数（例如 `threshold = 50%`, `target = 100%`, `max = 200%`）。
   - **绩效周期**：1 年 / 3 年 / 其他滚动周期。
   - **权重**：各指标在总激励中的权重占比。
   - **相对 TSR 对标组**：peer group 具体构成（如披露）。
   - **Non-GAAP 指标定义**：如出现 `"Adjusted ..."` / `"Compensation Adjusted ..."`，必须列出代理书披露的具体调整项清单。
3. **红旗信号**：
   - 激励是否过度挂钩收入/规模指标（忽视利润与现金流）→ 可能诱发激进收入确认。
   - 是否存在大额 one-time / discretionary bonus 或高比例主观评估权重。
   - Non-GAAP 调整项定义是否过于宽泛（如反复剔除重组、SBC、常规研发/折旧）。
   - 股价指标是否采用绝对股价门槛而非相对 TSR。
   - 是否存在异常加速 vesting 条款或丰厚的 golden parachute。
4. **薪酬与业绩对齐度**：
   - CEO 过去 3 年 realized pay（或 Compensation Actually Paid）vs 公司股价/股东总回报表现。
   - Say-on-Pay 投票赞成率（如有披露）。
   - 辅以 Form 4 高管近期公开市场买卖记录。

---

## 维度 3：管理层与董事会变动（过去 2 年）

**数据源**：最近 2 份 Proxy Statement 董事/高管名单对比 + `8-K`（Item 5.02）+ 两年窗口新闻检索。

<!-- DALTON_CAP:researchdata_bridge -->```python
researchdata.finance(route="sec", operation="eight-k", ticker="{TICKER}", limit=20)
researchdata.finance(route="sec", operation="list-filings", ticker="{TICKER}", form="8-K", limit=20)
web_search(query='"{COMPANY}" ("executive departure" OR "CFO change" OR "board change" OR "resigned")', start_date="<两年前 YYYY-MM-DD>", end_date="<今天 YYYY-MM-DD>")
```<!-- DALTON_CAP_ELSE -->```python
dalton_sec.list_filings(ticker="{TICKER}", forms="8-K", limit=20)
web_search(query='"{COMPANY}" ("executive departure" OR "CFO change" OR "board change" OR "resigned")', start_date="<两年前 YYYY-MM-DD>", end_date="<今天 YYYY-MM-DD>")
```<!-- /DALTON_CAP -->

### 必查项目
1. **高管变动清单**：过去 2 年离任与新任高管时间表；**CFO、Chief Accounting Officer（CAO）、Controller 离任或频繁更换为重大红旗**，必须逐一核查离任去向与披露原因。
2. **董事会变动**：独立董事比例变化、审计委员会（Audit Committee）成员及主席变更、是否有激进投资人（activist investor）入驻董事会。
3. **变动原因与模式**：是否出现 `"spend more time with family"` 等模糊辞职理由、短期内多位高管密集离职、或新任高管集中来自同一关联圈层（裙带关系）。

---

## 维度 4：社交媒体、员工、产品口碑、专家访谈与做空数据

**铁律**：**每一条发现必须附带具体来源 URL**<!-- DALTON_CAP:guidepoint -->（或 Guidepoint 的 `source_attribution.markdown`）<!-- /DALTON_CAP -->；无来源 URL 的发现不得写入报告。

### 1. 员工口碑
<!-- DALTON_CAP:researchdata_bridge -->```python
# 默认只调用免费源
researchdata.community(route="employee", operation="blind", company="{COMPANY}", pages=2)
researchdata.community(route="employee", operation="summary", company="{COMPANY}", pages=40)
# 仅在用户明确要求开启付费源时，才可加 allow_paid=True 调用 indeed / glassdoor
# 公开补充：PayScale / Levels.fyi / WARN 裁员记录
web_search(query='"{COMPANY}" (Glassdoor OR PayScale OR Levels.fyi OR WARN layoffs)', start_date="<两年前 YYYY-MM-DD>", end_date="<今天 YYYY-MM-DD>")
```
- Blind 第 31 条起正文为锁死占位文本，**只采信评分与前 30 条**。<!-- DALTON_CAP_ELSE -->```python
# 公开职场评价与裁员记录（Blind / Glassdoor / PayScale / Levels.fyi / WARN）
web_search(query='"{COMPANY}" (Blind OR Glassdoor OR PayScale OR Levels.fyi OR WARN layoffs)', start_date="<两年前 YYYY-MM-DD>", end_date="<今天 YYYY-MM-DD>")
```<!-- /DALTON_CAP -->
- 核查：综合评分与趋势、管理层/薪酬/工作量高频投诉词、裁员与冻结调薪记录。

### 2. 投资者社区与做空机构报告
<!-- DALTON_CAP:researchdata_bridge -->```python
researchdata.community(route="x-timeline", operation="search", query='"{COMPANY}" (fraud OR short OR "red flag")', since="<两年前 YYYY-MM-DD>", until="<今天 YYYY-MM-DD>", count=25)
web_search(query='"{COMPANY}" (fraud OR short OR "red flag") site:x.com', start_date="<两年前 YYYY-MM-DD>", end_date="<今天 YYYY-MM-DD>")
researchdata.community(route="last30days", operation="search", query="{COMPANY} {TICKER}", days=30, quick=True)
web_search(query='"{COMPANY}" ("short report" OR "short thesis") OR "${TICKER}" "short"', start_date="<两年前 YYYY-MM-DD>", end_date="<今天 YYYY-MM-DD>")
```
- X 平台按原帖 status URL 去重并注明发帖日期。
- `last30days` 仅用于近 30 天 StockTwits / HN / YouTube / 公开新闻情绪参考。<!-- DALTON_CAP_ELSE -->```python
x_search(query='"{COMPANY}" (fraud OR short OR "red flag")', from_date="<两年前 YYYY-MM-DD>", to_date="<今天 YYYY-MM-DD>")
web_search(query='"{COMPANY}" (fraud OR short OR "red flag") site:x.com', start_date="<两年前 YYYY-MM-DD>", end_date="<今天 YYYY-MM-DD>")
web_search(query='"{COMPANY}" ("short report" OR "short thesis") OR "${TICKER}" "short"', start_date="<两年前 YYYY-MM-DD>", end_date="<今天 YYYY-MM-DD>")
```
- X 平台按原帖 status URL 去重并注明发帖日期。
- 可辅以 `web_search` 检索近 30 天 StockTwits / HN / YouTube / 公开社区的情绪参考。<!-- /DALTON_CAP -->
- **Reddit 当前无通路，报告正文与来源节必须写明「Reddit 未覆盖」**。

### 3. 负面新闻与监管/诉讼
```python
web_search(query='"{COMPANY}" ("SEC investigation" OR restatement OR whistleblower OR "auditor resigned" OR lawsuit)', start_date="<两年前 YYYY-MM-DD>", end_date="<今天 YYYY-MM-DD>")
```
- 遇到关键报道、做空报告或监管公告页面，调用 `WebFetch(url="<真实URL>", prompt="提取核心时间线、指控要点与公司回应")` 读取正文。

### 4. 技术与产品口碑
- 检索产品迭代速度、技术债务/legacy stack 评价、技术骨干流失、客户重大投诉或产品质量纠纷，每条附来源 URL。

<!-- DALTON_CAP:guidepoint -->### 5. Guidepoint 专家访谈
```python
researchdata.research(
    route="guidepoint",
    operation="search_library",
    query="{COMPANY} competitive position customer churn management execution concerns",
    limit=8,
    params={"keywords": ["{TICKER}"]},
)
```
- 重点关注：客户流失/砍单、渠道压货、前员工对管理层与财务文化的真实评价、竞对视角的份额变化。
- 每条引用附 `source_attribution.markdown`，verbatim 原文引用不超过 20 词。
- 初次无结果时放宽到行业/主要竞对查 1 次；仍无结果则如实写「Guidepoint 无覆盖」。<!-- DALTON_CAP_ELSE -->### 5. 产业链与一线从业者公开访谈
```python
web_search(query='"{COMPANY}" (customer churn OR channel inventory OR former executive interview OR supply chain)', start_date="<两年前 YYYY-MM-DD>", end_date="<今天 YYYY-MM-DD>")
```
- 重点关注：客户流失/砍单、渠道压货、前员工对管理层与财务文化的公开评价、竞对视角的份额变化。
- 每条引用附具体来源 URL，原话直接引用不超过 3 句。
- 初次无结果时放宽到行业/主要竞对查 1 次；仍无结果则如实写「公开一线访谈无覆盖」。<!-- /DALTON_CAP -->

### 6. 做空数据（Short Interest & Short Volume，美股适用）
```python
get_company_overview(symbol="{TICKER}")
```
- 从返回的 `### Share Structure` 提取最新 `Short Interest`、`Short % of Float`、`Days to Cover`、`Short Volume Ratio` 及对应截至日期。
- 如需查多期历史变化，在 `execute_code` 中从 `tools.price_data` 导入 `get_short_data` 读取美股 FINRA 双周 `short_interest` 与每日场外 `short_volume` 序列，判断做空拥挤度、逼空风险与近几期空头增减趋势。
