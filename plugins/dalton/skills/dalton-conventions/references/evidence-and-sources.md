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
| `cn-hk` (`filings`) | `cninfo:{id}` | `filing` | `"巨潮资讯"` | 公告日期 |

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

- **目标公司的历史财务数字只取一手法定底表**（SEC 10-K/10-Q/20-F 或官方财报附注）：研报、新闻或摘要里引用的目标公司历史财务数据只能作线索，入表与成稿前必须用 `dalton_sec` 或 `researchdata.finance(route="sec")` 核对底表。
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
