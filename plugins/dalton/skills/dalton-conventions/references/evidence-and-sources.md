# 来源层级、身份、去重与合规（Evidence & Sources）

## 1. 基金 6 级来源层级与禁用来源

当不同来源对同一事实或数字存在冲突时，严格按以下 6 级权威度从高到低采信（高于 `research-conventions` 的默认排序）：

1. **Level 1 — 监管法定披露（`filing`）**：SEC 10-K/10-Q/8-K/20-F、港交所披露易公告、巨潮资讯法定财报。
2. **Level 2 — 公司官方材料（`company`）**：业绩新闻稿（Earnings Release）、投资者演示文稿（IR Deck）、业绩电话会管理层正式发言（Prepared Remarks & Confirmed Q&A）。
3. **Level 3 — 具名机构研报与官方统计（`broker_report` / `official_data`）**：具名卖方深度/点评研报（AlphaEngine、Sales Notes 原件）、央行/FRED/财政部/行业协会官方统计。
4. **Level 4 — 第三方独立产业数据与同行交叉验证**：上下游上市公司披露、供应链独立调研、行业监测数据库。
5. **Level 5 — 专家访谈与渠道调研（`expert`）**：Guidepoint 专家纪要、基金 Wiki 与 AlphaEngine 中的专家访谈（必须标明专家身份、日期与验证状态，经营数字标"单一来源估计"）。
6. **Level 6 — 财经新闻与社媒讨论（`news` / `derived`）**：新闻报道、TMTB、FUNDA AI 摘要等**仅作研究线索**，涉及财务或经营数字时必须回溯到 Level 1–5 原始出处。

**绝对禁用来源**：**百度百科**（任何情况下不得作为事实、历史沿革或行业数据的来源）。

---

## 2. 来源身份与 `researchdata` 的 `_sources.identity` 对照表

各来源提供的证据与 `research` 路由选用规则，统一以 `<mcp-servers>` 中 `researchdata` 的说明与 `.agents/tools/docs/researchdata/research.md`（由 `scripts/dalton/researchdata_bridge.py` 的 `RESEARCH_EVIDENCE_AND_ROUTE_GUIDE` 单一来源生成）为准，此处不再重复抄录。

所有工具返回的 `_sources` 条目按以下规则映射来源身份（`identity`）：

| 路由与操作（route / operation） | `document_id` 格式 | `identity`（来源身份） | `publisher`（发布方） | `as_of` 口径 |
|---|---|---|---|---|
| `alphaengine` (`search_library` / `get_document`) | `alphaengine:{doc_id}` | 研报 → `broker_report`；公告 → `company`；业绩/调研纪要 → `company`；专家纪要 → `expert`；新闻 → `news` | `sources[0]` 机构名 | `publish_time` |
| `guidepoint` (`search_library`) | `guidepoint:{sha1}` | `expert` | `"Guidepoint · {title}, {company}"`（**不写姓名**） | `date` (`call_date`) |
| `transcript` (`transcript`) | `alphaengine:{doc_id}` | 公司电话会 → `company`；卖方回访 → `broker_report` | `sources` | `publish_time` |
| `company-wiki` (`search` / `read` / `list`) | `wiki:<id>` | 专家访谈 → `expert`；管理层纪要 → `company`；券商研报/卖方交流/Earnings Update → `broker_report`；微信文章 → `news`；内部笔记 → `internal_note` | frontmatter `source`（无则写`"基金 wiki"`） | `date` (`document_date`) |
| `sales-notes` (`search` / `read`) | `mail:<id>` / `digest:<id>` | 单封邮件 → `broker_report`；汇总层 `layer=digest` → `derived` (`independent=false`) | 发件人 `original_source` | `date` (`document_date`) |
| `tmtb` (`search` / `read`) | `tmtb:<id>` | `news`（第三方通讯） | `"TMTB"` | `date` |
| `funda-ai` (`search` / `read`) | `funda:<id>` | `derived` (`independent=false`) | `"FUNDA AI"` | `published_at` |
| `all` (`search` 联搜：本地 4 库 + AlphaEngine + Guidepoint) | 保留各源 `document_id`，合并 `alternate_document_ids` | 按所属原始源规则映射 | 各源发布方 | 各源日期 |
| `sec` (`researchdata` 或 `dalton_sec`) | `sec:{accession}` | `filing` | `"SEC EDGAR"` | `filing_date` (`filed`) |
| `estimates` (`consensus`) | `estimates:yahoo:{ticker}:{fetched_at}` | `street_estimate` | `"Yahoo Finance via yfinance"` | `fetched_at` |
| `fred` / `treasury` | `fred:{series}` / `treasury:{dataset}` | `official_data` | `"FRED"` / `"US Treasury"` | `observation_date` |
| `market-regime` (`snapshot` / `us_topbottom` / `crowding` / `cn_leverage`) | `regime:<op>:<date>` | `derived`（OpenClaw 监控二手读数） | `"OpenClaw"` | 数据日期（`date` / `trade_date` / `as_of`） |
| `cn-hk` (`filings`) | `cn-hk:{ticker}:{date}:{index}` | `filing` | `"cninfo"`（巨潮资讯；港股未覆盖时补 HKEXnews 原文链接） | 公告日期 (`date`) |
| `cn-hk` (`ask`) | 无独立归档 ID（平台聚合表） | 二手初值线索（非 `filing`，核心财务数须回公告核实） | 底层平台（AkShare / 东财 / 同花顺 / 新浪等，见 `plan.source`） | 报告期 / 查询日（无历史 as-of 保证） |

---

## 3. 来源链去重（Source Chain Deduplication）

- **同源转述只算一条**：同一份大行研报或管理层表态被邮件（`sales-notes`）、汇总（`digest`）、`TMTB`、`FUNDA AI` 或社媒多次转述时，在证据计数与支撑力度上**只算 1 条来源**，并优先引用最早或最完整的原始报告。
- **单源标注**：当某个行业数字、供应链传闻或竞争判断只有 1 家独立来源（尤其是单一券商或单一专家）时，正文必须显式标注**"单源观点"**或**"单一来源估计"**。
- **热度不等于共识**：转发量、点赞数或多封汇总邮件重复提及同一传闻，绝不构成"市场共识"或"多源交叉验证"。

---

## 4. 关键数字的双源交叉验证

- 支撑核心 Thesis 支柱、驱动树核心假设或估值结论的**关键非财报数字**（如市场份额、单客获客成本、特定产品毛利率、未披露的某业务流水），至少需要 **2 个独立来源**交叉验证。
- 若暂时只有 1 个独立来源，必须标明"单源待验证"，并在 `agenda.md` 中列入下一项核实计划。

---

## 5. 买方门槛与五类预期身份的严格区分

正文中引用任何预期、目标或门槛数字时，必须明确区分以下五类身份，**严禁混用或把分歧数字简单算术平均成所谓"市场共识"**：

1. **公司指引（Company Guidance / Company Claim）**：管理层在财报或电话会给出的正式区间（注明日与口径，如 GAAP vs Non-GAAP）。
2. **卖方一致预期（Street Consensus，`street_estimate`）**：如 Yahoo Finance / Visible Alpha / Bloomberg 汇总的均值与中位数（须写明取数时间 `fetched_at` 与样本分析师人数）。
3. **具体机构预测（Named Broker Estimate，`broker_report`）**：如"Morgan Stanley（2026-10-12）预计 FY26 收入为……"。不同卖方存在明显分歧时，直接呈现分歧区间（如"多头卖方看 X，空头卖方看 Y"），**不得自行取平均数冒充共识**。
4. **明确的买方门槛（Explicit Buy-Side Bar / Bogey）**：仅当研报或销售笔记（Sales Notes）明确写出买方预期（如"买方 bogey 在 +18% YoY"）时才可标为买方门槛，并注明来源机构与日期。
5. **作者判断 / 模型推导（House View / `judgement` / `model-derived`）**：我们自己的预测或由 `implied_model` 倒推的数字，必须与外部预期严格分开。

---

## 6. 电话会提问口径（Analyst Question Caveat）

- 在阅读业绩电话会纪要（Transcript）或调研纪要时，如果某个数字是由**提问分析师在问题中说出**（例如："如果假设你们 Q4 AI 收入达到 50 亿美元……"），而管理层在回答中**没有明确确认**该数字，引用时必须标注：**"分析师提问口径，管理层未确认"**，严禁将其当作公司披露或管理层认可的事实。

---

## 7. "本轮未取得" vs "公司未披露"

- **严禁把检索未命中直接写成"公司未披露"**：当工具未搜到某项数据时，只能表述为**"本轮未取得"**。
- 最终报告中的"未找到 / 本轮未取得"，必须如实限定在**本轮已实际检索的数据源与时间范围**内（例如："本轮检索 FY24–FY25 10-K 及近 90 天 AlphaEngine 研报未取得该分部退货率数据"）。

---

## 8. 数字底表原则（Primary Table Rule）

- **目标公司的历史财务数字只取一手法定底表**（美股 SEC 10-K/10-Q/20-F、港股 HKEX 披露易业绩公告与年报/中报、A 股巨潮资讯法定定期报告与公告附注）：研报、新闻、摘要或 `researchdata.finance(route="cn-hk", operation="ask")` 平台财务表里引用的目标公司历史财务数据只能作初筛线索与初值，入表与成稿前美股必须用 `dalton_sec` 或 `researchdata.finance(route="sec")` 核对底表，港股与 A 股必须用 `researchdata.finance(route="cn-hk", operation="filings", ticker=..., start=..., end=..., limit=...)` 调取官方公告原文/PDF（港股若 `cn-hk filings` 未覆盖则用 `web_search` 查 HKEXnews `site:hkexnews.hk` 原文链接）核实。
- **拿不到就写"本轮未取得"**：如果某个细分指标底表未披露，直接写"未披露 / 本轮未取得"，**严禁用口径相近但不同的数字顶替**（例如不得用总 GMV 顶替自营 GMV，不得用含税收入顶替不含税收入）。

---

## 9. 合规口径（全文照录）

> 专家访谈、管理层交流（包括转入基金 wiki 的材料）以及 Guidepoint 的检索结果，都已经过用户的合规流程，可以使用（2026-10-15 确认，2026-10-18 owner 再次确认范围包括 Guidepoint）。这包括以公司在职员工口吻发言的专家，以及高管小范围交流。照常使用，不要自行以"重大非公开信息"为由排除。
>
> 引用时写日期和专家身份（写身份，不写姓名），并注明可靠性（已证实、被反驳、互相矛盾）。专家给出的经营数字，标为"单一来源估计"，与公司披露和卖方数据分开写。
>
> Guidepoint 原文引用不超过 20 个词，并附 `source_attribution`；不得用于 KYC 或合规筛查。

适用范围已定：wiki 里的专家访谈和管理层交流、AlphaEngine 里的专家和调研纪要、Guidepoint 检索结果，一律可用并标专家身份。

---

## 10. 原文引用上限（Verbatim Quote Limits）

- **Guidepoint 专家材料**：单次直接原文引用**不超过 20 个英文词**（或等长中文），且必须附带 `source_attribution`（专家职位/公司身份 + 访谈日期，不写姓名）。
- **其他受版权保护的研报与纪要**：直接原文引用**不超过连续 3 句话**；超出部分必须使用分析性转述并标注来源身份与日期。

---

## 11. 港股与 A 股（`cn-hk`）数据口径与核验纪律

针对港股（HKEX）与 A 股（上交所/深交所/北交所）标的，调用 `researchdata.finance(route="cn-hk", ...)` 时必须严格执行以下四条口径规则：

1. **平台财务表属二手初值，核心数字必须回法定公告核实**：
   - `researchdata.finance(route="cn-hk", operation="ask", query=..., limit=...)` 返回的利润表、资产负债表、现金流量表、财务指标、估值与分红回购汇总（底层源自 AkShare、东方财富、同花顺、新浪、腾讯等公开聚合接口）均属于**二手数据，仅可作为初筛与初值线索**。
   - 凡进入数字底表（`facts.md`）、财务镜头、财报点评或正式报告的**核心财务数字**（营业收入、毛利、营业利润、归母净利润、Non-IFRS/经调整利润、现金及定期存款、受限资金、理财/FVPL、有息负债、总股本、分红与实际回购金额），必须调用 `researchdata.finance(route="cn-hk", operation="filings", ticker=..., start=..., end=..., limit=...)` 获取巨潮资讯（cninfo）法定公告 PDF（`pdf_link`）或原文核实。
   - **港股披露易（HKEXnews）退路**：若港股某份业绩公告、翌日披露报表（Next Day Disclosure Return）或通函未被 `cn-hk` 的 `operation="filings"` 覆盖，必须使用 `web_search` 检索港交所披露易（`site:hkexnews.hk` 或公司 IR 官网）并核对公告原文链接与正文。
2. **Bridge 参数 Schema 与代码格式约束**：
   - `cn-hk` 只用 `operation`（`"ask"` 或 `"filings"`）、`ticker`、`query`、`start`（`YYYY-MM-DD`）、`end`（`YYYY-MM-DD`）、`limit`（`1..100`）。
   - `operation="filings"` 必传 `ticker`、`start`、`end`：`filings` 的 `end` 不得晚于当天（`TODAY`），否则整次调用失败（返回 `ok: false`）；当检索窗口终点做跨日外推（如 `end_plus_1d`）时，必须取 `end=min(end_plus_1d, TODAY)`。`ticker` 必须传**纯数字代码**——港股传 **5 位零填充数字**（如 `"00700"`、`"03690"`、`"09988"`），A 股传 **6 位纯数字**（如 `"600519"`、`"300750"`、`"000858"`），**不得带 `.HK` / `.SS` / `.SZ` 后缀**。
   - `operation="ask"` 必传自然语言 `query`，每次调用只查**单一意图**（不要在同一条 query 中拼接多个意图）：在 `query` 中写明 5 位港股代码、6 位 A 股代码或中文简称及单一意图；其中资金流类查询必须带具体日期（如 `"宁德时代 300750 2026-10-21 主力资金净流入和成交额"`），两融、龙虎榜、十大股东、分红、回购、三表与 AH 溢价等各自单独调用一条 `ask`（如 `"00700 港股利润表"`、`"00700 港股资产负债表"`、`"600519 2026-10-21 两融"`、`"600519 2026-10-21 龙虎榜"`、`"600519 十大股东"`、`"00700 分红"`、`"00700 回购"`、`"AH溢价"`）。
3. **复权、分红、拆股、资金流回退口径与币种核对**：
   - **`ask` 返回的 `summary` 与回退口径核查**：必须读取 `ask` 返回的 `summary` 和实际来源（`plan.source`）；例如东财资金流接口失败时会自动退到同花顺**当日即时**口径并在 `summary` 里注明，成稿必须标注实际口径，若拿不到指定历史日的数据就如实写明缺口，不得把即时/最新值当作窗口内历史日终数。
   - **回报与复权口径**：核对区间涨跌幅与历史股东回报时，必须核实前/后复权状态（`cn-hk` 底层 A 股日线默认前复权 `qfq`，港股日线默认不复权；当触发腾讯/新浪/东财跨源回退时，必须检查返回中的 `plan.source` 并在底稿注明复权口径），同时核查区间内的现金分红、送转股、拆股/合股除权。
   - **交易币种与报表币种严格分离**：港股大量公司以港币交易（`listing_currency="HKD"`）、以人民币或美元编制财报（`financial_currency="CNY"` 或 `"USD"`）；A 股以人民币交易并编制财报（`CNY`）。运行 `anchors.py` 及计算市值、EV、每股指标与三把财务镜头时，必须显式传入 `--listing-currency`、`--financial-currency` 及对应汇率（`--listing-fx-rate`、`--fx-rate`），**严禁把港币市值/股价与人民币报表金额按 1:1 混算或套错汇率**。
   - **A+H 双重上市股本对齐**：对 A+H 两地上市公司，总市值与稀释总股本必须覆盖全部已发行股份（A 股 + H 股总和，并参考 `operation="ask", query="AH溢价"` 核对两地价差），与合并财务报表口径保持一致。
4. **无历史 Point-in-Time（`as-of`）保证**：
   - `cn-hk` 的日期窗口仅过滤交易日或报告期，不代表历史某时点可见的未重述版本；涉及历史回测或跨期对比时需结合法定公告发布日期（`filed` / `date`）核实。

---

## 12. 市场状态与仓位拥挤度（`market-regime`）口径纪律

调用 `researchdata.macro(route="market-regime", operation="snapshot"|"us_topbottom"|"crowding"|"cn_leverage")` 引用市场状态、美股顶部/底部框架、指数拥挤度与 A 股杠杆监控时，必须严格遵守以下三条规则：

1. **二手读数必须标注数据日期（`as_of` / `date` / `trade_date`）**：
   - `market-regime` 是 OpenClaw 每日生成的监控二手读数（`document_id` 为 `regime:<op>:<date>`，`identity="derived"`，`publisher="OpenClaw"`），引用任何读数时必须显式标注对应的数据日期（如 `signals.json` 的 `date`、`as_of` 各分项日期、拥挤度的 `trade_date` 或去杠杆信号的 `date`）；当返回 `stale=true` 时，必须在正文中注明数据滞后及 `warnings`。
2. **顶部和底部的“阶段（`stage`）”是框架状态，不是预测**：
   - `us_topbottom`（`top_framework` / `bottom_framework`）中的 `stage`（如“无信号”、“预警”等）与 `summary` 仅代表监控框架当前所处的条件状态，绝不是对未来走势的概率预测，**严禁将其写成或解读为“上涨概率”或“下跌概率”**。
3. **拥挤度 `probability_pct` 属于源站统计，必须原样标注来源**：
   - `crowding` 返回的 `probability_direction`、`probability_pct` 与 `probability_horizon_days` 是源站（LeguLegu 乐估乐股历史分位回测）给出的历史统计值，引用时必须原样标注来源与统计口径，不得包装成我们自己的模型预测。
