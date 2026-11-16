---
name: dalton-short-screen
description: "四维做空风险初筛：核查会计红旗 10 项、高管薪酬（DEF 14A / 20-F）、两年管理层变动、社媒口碑与做空数据，输出带溯源链接的筛查报告。评估做空标的或排查财务与治理红旗时使用。"
---

# 做空风险四维初筛（`dalton-short-screen`）

四维做空初筛流水线：会计红旗、高管薪酬与激励、过去两年管理层与董事会变动、员工与社媒口碑（附做空持仓与做空成交量辅助核查）。**所有四个维度必须全部执行，不可跳过。**

详细检查清单见 [`references/four-dimensions.md`](references/four-dimensions.md)，输出模板见 [`references/output-template.md`](references/output-template.md)。

---

## 执行铁律（必须遵守）

1. **SEC Filing 是一手信源**：第 1–3 维核心财务与治理数字必须直接读 SEC filing 原文或结构化 XBRL/财报接口，不得凭记忆或二手摘要作结论。
2. **数字必须溯源**：报告中的每个财务/薪酬数字必须注明出处（filing 类型 + 章节 + accession number，或工具来源与日期）。
3. **每条发现附来源链接**：第 4 维（员工、投资者社区、负面新闻、技术口碑、Guidepoint）的**每一条发现都必须附上具体来源 URL**（员工评价页面链接、X 原帖 status URL、新闻文章 URL、SEC 8-K URL 等）或 Guidepoint 的 `source_attribution.markdown`。没有来源链接的条目不得写入报告。
4. **边际变化比绝对值更重要**：重点跟踪最近 2–3 个报告期的会计指标、合同资产/负债与资本化趋势变化。
5. **外国私人发行人适配**：使用 `20-F` 替代 `10-K`、`6-K` 替代 `10-Q`；无 `DEF 14A` 时，直接审阅 `20-F` 的 `Item 6.B`（Compensation）获取高管薪酬与激励条款。
6. **诚实标注数据缺口**：拿不到的数据写「本轮未取得」并说明原因；通路受限或无覆盖的平台直接写「未覆盖」（例如 Reddit 当前无通路，报告里必须明确写明「Reddit 未覆盖」，严禁写成「查过没有」或尝试伪造 UA/代理/cookie 绕过）。
7. **多源交叉验证**：社交与职场负面评价须多平台交叉印证；单一来源不足以下定论；`researchdata` 返回 `ok: false` 时按 `hint` 调整重试，仍失败则如实标注未取得。

---

## 四个维度的取数与核查要点

### 维度 1：会计红旗分析（10 项必查）

- **数据源**：美国本土公司读最新 `10-K`（近 2 期）+ `10-Q`（近 3 期）；外国私人发行人（如 BABA、PDD、JD、TCOM、BIDU）读 `20-F` + `6-K`。
- **Dalton 取数调用**：

```python
# 1. 检索监管申报列表（美股本土 10-K/10-Q；外国发行人改传 form="20-F" / form="6-K"）
researchdata.finance(route="sec", operation="list-filings", ticker="{TICKER}", form="10-K", limit=2)
researchdata.finance(route="sec", operation="list-filings", ticker="{TICKER}", form="10-Q", limit=3)

# 2. 结构化三表与财务比率
researchdata.finance(route="sec", operation="financials", ticker="{TICKER}", form="10-K", limit=5, params={"statement": "all"})
researchdata.finance(route="sec", operation="ratios", ticker="{TICKER}")

# 3. 分 Item 读取正文（10-K Item 7 MD&A / Item 8 附注；10-Q Part I Item 2）
researchdata.finance(route="sec", operation="read-item", ticker="{TICKER}", form="10-K", item="7", params={"max_chars": 40000})
researchdata.finance(route="sec", operation="read-item", ticker="{TICKER}", form="10-Q", item="2", params={"max_chars": 40000})

# 4. 全文关键词定位（收入确认、资本化、关联方、审计师等；可按需配 params 里的 window 与 max_matches）
researchdata.finance(route="sec", operation="search-text", ticker="{TICKER}", form="10-K", query="revenue recognition")
researchdata.finance(route="sec", operation="search-text", ticker="{TICKER}", form="10-K", query="capitalized")

# 5. 自然语言事实检索（替代旧 ask 路由）
researchdata.finance(route="sec", operation="smart-facts", ticker="{TICKER}", query="contract assets and contract liabilities", limit=10)
```

- **10 项必查清单**（完整细则见 [`references/four-dimensions.md`](references/four-dimensions.md)）：
  1. 收入确认政策（激进确认、YoY 变更、合同资产 vs 合同负债剪刀差）
  2. 一次性 / 非经常性收益（占税前利润比例、是否掩盖主业恶化）
  3. 资本化 vs 费用化（研发/内部软件资本化比例、折旧摊销年限延长）
  4. 应收账款质量（DSO 趋势 vs 收入增速、坏账准备比例、关联方应收）
  5. 存货质量（DIO 趋势、存货减值、原材料/在制品/成品结构）
  6. **现金流与利润质差**：不重复重写公式，**直接调用 `.agents/skills/dalton-financial-lenses/SKILL.md` 的盈利质量脚本 `earnings_quality.py`** 计算 Sloan 应计比率、`CFO / NI` 比率与现金转换周期 `CCC`，并结合财务附注核查 CFO 与净利润主要差异项及 SBC 加回后的真实盈利。
  7. 自由裁量假设（商誉减值 DCF 假设、长期资产减值触发、Level 3 公允价值资产占比）
  8. 表外项目（租赁义务、VIE、担保/承诺/或有负债、非合并合营/联营）
  9. **偿债能力与现金流健康度**：不重复重写公式，**净现金/净负债双口径与“大存大贷”检查直接参照 `.agents/skills/dalton-financial-lenses/SKILL.md` 的 `netcash.py`**（Definition A/B、营运资本、其他应收/应付 >20% 预警），再补充 `Net Debt / EBITDA`、FCF 趋势、FCF Yield、Interest Coverage（EBIT / Interest Expense）与 Debt Maturity Profile（近 1–2 年到期债务）。
  10. 边际变化（近 2–3 期趋势、会计政策/估计突然变更、审计师更换或保留意见）

### 维度 2：高管薪酬与激励分析（`DEF 14A` / 外国发行人 `20-F Item 6.B`）

- **数据源**：最新 Proxy Statement（`DEF 14A`）；**外国私人发行人无 `DEF 14A` 时改查 `20-F` 的 `Item 6.B`（Compensation）**。`DEF 14A` 篇幅较长时先用 `search-text` 定位 CD&A 与 Summary Compensation Table，禁止用 `WebFetch` 猜 SEC URL。
- **Dalton 取数调用**：

```python
researchdata.finance(route="sec", operation="list-filings", ticker="{TICKER}", form="DEF 14A", limit=2)
researchdata.finance(route="sec", operation="search-text", ticker="{TICKER}", form="DEF 14A", query="Summary Compensation Table")
researchdata.finance(route="sec", operation="search-text", ticker="{TICKER}", form="DEF 14A", query="threshold")
# 外国发行人无 DEF 14A 时读 20-F（先取完整 Item 6，再在正文内定位 6.B Compensation 小节）：
researchdata.finance(route="sec", operation="read-item", ticker="{TICKER}", form="20-F", item="6", params={"max_chars": 40000})
```

- **必查内容**：CEO/CFO 总薪酬结构及比例；短期/长期激励挂钩的**全部财务与经营 KPI、权重、考核周期、threshold / target / maximum 门槛及对应 payout 倍数、peer group 与 Non-GAAP 调整项具体定义**（不可概括）；红旗信号（过度挂钩收入、大额 discretionary bonus、宽泛 Non-GAAP、绝对股价替代相对 TSR、加速 vesting / golden parachute）；过去 3 年 realized pay 与股价对齐度及 Say-on-Pay 投票结果（详情见 [`references/four-dimensions.md`](references/four-dimensions.md)）。

### 维度 3：管理层与董事会变动（过去 2 年）

- **数据源**：最近 2 份 Proxy Statement 董事/高管名单对比 + `8-K`（重点 Item 5.02）+ 两年新闻检索。
- **Dalton 取数调用**：

```python
researchdata.finance(route="sec", operation="eight-k", ticker="{TICKER}", limit=20)
researchdata.finance(route="sec", operation="list-filings", ticker="{TICKER}", form="8-K", limit=20)
web_search(query='"{COMPANY}" executive departure OR "CFO change" OR "board change"', start_date="<YYYY-MM-DD(两年前)>", end_date="<YYYY-MM-DD(今天)>")
```

- **必查内容**：高管离任/新任清单（CFO、Chief Accounting Officer / Controller 变更为重大红旗）；董事会独立董事比例、审计委员会变更、激进投资人（activist investor）进入董事会；离职理由模式（如模糊的 "spend more time with family"、短期密集离职、新任高管集中来自同一关联背景）。

### 维度 4：社交媒体、员工、产品口碑与专家信号

- **数据源与 Dalton 路由（付费源默认关闭，不绕过未覆盖平台）**：

| 来源 | Dalton 调用 | 关键规则 |
|---|---|---|
| Blind（免费） | `researchdata.community(route="employee", operation="blind", company="{COMPANY}", pages=2)`<br>`researchdata.community(route="employee", operation="summary", company="{COMPANY}", pages=40)` | Blind 第 31 条起正文为锁死占位文本（`body_locked: true`），只信评分与前 30 条；附原帖/聚合来源 URL |
| Indeed / Glassdoor（付费） | **默认不开启** | 仅当用户明确要求时才可传 `allow_paid=True` 调用 `operation="indeed"` 或 `operation="glassdoor"`；否则可先用 `web_search` 查公开可见评分与公司主页 URL |
| X / Twitter | `researchdata.community(route="x-timeline", operation="search", query='"{COMPANY}" (fraud OR short OR "red flag")', since="<两年前>", until="<今天>", count=25)`<br>辅以 `web_search(query='"{COMPANY}" (fraud OR short OR "red flag") site:x.com', start_date="<两年前>", end_date="<今天>")` | 按原帖 status URL 去重，记录发布时间；不依赖 `last30days` 替代 X 检索 |
| 投资者社区（StockTwits / HN / YouTube / 新闻） | `researchdata.community(route="last30days", operation="search", query="{COMPANY} {TICKER}", days=30, quick=True)` | 仅作近 30 天社区情绪与焦点证据 |
| Reddit | **当前无通路** | 报告中**必须明写「Reddit 未覆盖」**，严禁伪造 UA/代理/cookie/RSS |
| 新闻 / 监管 / 诉讼 / 做空报告 | `web_search(query='"{COMPANY}" ("SEC investigation" OR restatement OR whistleblower OR "auditor resigned" OR "short report")', start_date="<两年前>", end_date="<今天>")`<br>需要读报告或公告全文时调用 `WebFetch(url="<具体URL>", prompt="提取核心指控、诉讼进展或薪酬评价")` | 必须核对结果发布日期；所有 URL 均须来自搜索或工具返回 |
| Guidepoint 专家访谈 | `researchdata.research(route="guidepoint", operation="search_library", query="{COMPANY} competitive position customer churn management execution", limit=8, params={"keywords": ["{TICKER}"]})` | 重点查客户流失/砍单、渠道压货、管理层与财务文化、份额变化；verbatim 引文 ≤20 词并附 `source_attribution.markdown`；无结果时放宽到行业/竞品查 1 次，仍无则写「Guidepoint 无覆盖」 |

---

## 做空数据（Short Interest & Short Volume）

在四维分析之外，**新增核查目标公司的市场做空拥挤度与做空交易活动**（美股适用；非美股或无覆盖时如实标注「做空数据未覆盖」）：

1. **取数入口**：
   - 调用直接工具 `get_company_overview(symbol="{TICKER}")`，在返回的 `### Share Structure` 部分读取最新 `Short Interest`（含 `settlement_date`）、`Short % of Float`、`Days to Cover` 与 `Short Volume Ratio`（含 `date`）。
   - 如需观察多期历史变化趋势，可在 `execute_code` 中从 `tools.price_data` 导入 `get_short_data`（传 `symbol="{TICKER}"`、`data_type="both"`、`limit=20`），分别获取美股 FINRA 双周报 `short_interest` 序列与每日场外 `short_volume` 序列。
2. **解读要点**：
   - **拥挤度与逼空风险**：`Short % of Float` 偏高（如 >10%–15%）或 `Days to Cover` 较高时，既说明空头已有共识、借券或已趋紧，也提示存在空头回补（short squeeze）风险；需区分「已被充分定价的共识空头」与「低关注度早期红旗」。
   - **边际变化印证**：结合维度 1–4 判断——若 `Short Interest` 连续多期抬升且伴随基本面/会计质量恶化（如应收账款、资本化率、合同资产背离），可作为空头资金入场的交叉印证；`Short Volume Ratio`（每日场外做空成交占比）短线冲高通常反映事件前后抛压或做市对冲加剧，不能单独替代长期做空逻辑，需在报告中与双周 `Short Interest` 区分口径列示。

---

## 输出规范与保存位置

1. **输出位置**：按 `dalton-financial-lenses`、`dalton-full-coverage`、`dalton-filing-review` 等研究类 skill 惯例，完整 Markdown 报告保存至当前工作区 `drafts/<YYYY-MM-DD>-<TICKER>-short-screen.md`（例如 `drafts/2026-11-15-BR-short-screen.md`）。
2. **报告结构**：严格按 [`references/output-template.md`](references/output-template.md) 组织（核心判断 + 一至四维 + 做空数据概况 + 五、风险信号汇总表 + 资料来源及未覆盖声明）。
3. **飞书文档**：默认只保存在工作区；**仅当用户在本次对话明确要求输出飞书文档时**，调用 `feishu_create_doc(title="{COMPANY}（{TICKER}）做空风险初筛（YYYY-MM-DD）", markdown=...)` 创建飞书文档并返回链接。
