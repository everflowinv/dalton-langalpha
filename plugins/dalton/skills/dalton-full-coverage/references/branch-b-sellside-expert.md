# 支线 B：卖方、内部认知与专家访谈（`branch-b-sellside-expert.md`）

本支线输出 `research/<D>-fc/eb.md`，正文引用统一使用 `[引用1]`…`[引用N]`，与文末 `## 引用` 节一一对应。重点服务 **Q3、Q4、Q5、Q7、Q8、Q9、Q10、Q12**（具体以 `research/<D>-fc/questions.md` 分工为准）。动手前必读 `references/branch-common.md`。

---

## 产出章节结构（B1–B7，按顺序撰写）

### B1. AlphaEngine 卖方研报与纪要精读
1. **先查分类过滤器**：调用 `researchdata.research(route="alphaengine", operation="list_filters")` 获取当前有效的行业名与文档分类列表，再从返回结果里挑选所需的 `type_ids`。
2. **按类别分次检索**：每次只查一个类别（外资深度报告、卖方点评、会议/调研纪要分开检索），调用 `researchdata.research(route="alphaengine", operation="search_library", query=..., start=..., end=..., params={"type_ids": [...], "sort": "relevance"})`（时间范围通过顶层 `start` 与 `end` 传入，默认覆盖过去 12 个月；`sort` 与 `type_ids` 放在 `params` 内）。
3. **核心结论必须 `get_document` 读全文**：`search_library` 仅用于召回，严禁只看标题或 snippet 下结论。挑出 6–10 篇代表性多空研报与纪要读取正文。
4. **逐篇结构化记录**：机构名称、署名分析师、发布日期、标题、`doc_id`、核心论点与关键假设、评级与目标价（如有）。

### B2. 卖方一致预期地图与分歧点
- **一致预期基准**：结合 `researchdata.finance(route="estimates", ticker=T)` 与卖方模型，列出未来两年（`0y`、`+1y`）营业收入、核心分部收入、营业利润率、EPS 及核心 KPI 的卖方一致预期中位数与高低区间，标明日期与分析师家数。
- **2–4 个核心分歧点**：找出卖方之间明确打架的 2–4 个关键变量，每个写清：乐观派（Bull）假设多少、依据是什么；悲观派（Bear）假设多少、依据是什么。
- **过去 12 个月评级与目标价变化路径**：梳理过去一年主要外资行/大行何时上调或下调了评级与目标价、从多少调到多少、当时的核心理由是什么。
- **最近一次集体预期修正的触发事件**：卖方最近一次普遍上修或下修盈利预测是由哪次财报、指引或行业事件触发的。

### B3. 基金内部认知（四库联搜与 `company-wiki`）
- 调用 `researchdata.research(route="all", operation="search", query="...", params={"layer": "all", "media": "all"})` 及 `route="company-wiki"`（查公司页 `params={"company": TICKER}` 与所属行业页）。
- 命中后调用 `operation="read"` 读取正文（多票日报如 TMTB、sales-notes 按 `next_offset` 翻页摘取本公司段落）。
- **合规与去重纪律**：
  - 基金 `company-wiki` 里的专家访谈与管理层小范围交流已通过合规流程，照常使用，注明日期、专家身份（不写姓名）及后续验证情况；
  - FUNDA AI、TMTB、销售邮件若转述同一份卖方研报，只算一条来源链，不重复计票；
  - 梳理基金内部过去一年对该公司的历史观点演进，作为判断“市场共识与我们自身认知如何变化”的锚点。

### B4. Guidepoint 专家访谈
- 调用 `researchdata.research(route="guidepoint", operation="search_library", query="<英文研究问题>", params={"keywords": ["<TICKER>"]})`。
- **英文构造检索词**，围绕以下五个一线维度检索：
  1. 客户真实采用率、续费与流失原因（Adoption & Churn）；
  2. 竞争对手赢单/输单真实反馈（Win/Loss Dynamics）；
  3. 渠道库存、折扣返利与供应链信号（Channel & Supply Chain）；
  4. 前高管或资深从业者对公司组织执行力与文化的评价（Execution & Culture）；
  5. 真实定价权与合同条款结构（Pricing & Contract Terms）。
- 若无目标公司直接专家纪要，放宽至主要竞品或上下游行业视角重试一次，并在正文中标明“行业旁证”。
- **原文引用红线**：每处直接引用 **不超过 20 个英文词**，并紧跟专家身份、访谈日期与 transcript ID / `source_attribution`。

### B5. 卖方盲点（2–4 条）
- 提炼 **2–4 条对公司基本面或长期价值至关重要、但当前卖方研报普遍忽略或权重给错的盲点**（例如：会计折旧年限变更掩盖真实成本、SBC 长期摊薄、渠道塞货、或某项隐含资产/新业务未进入卖方分部估值），并解释为什么卖方没有充分讨论。

### B6. 逐题证据小结与缺口
- 按 `references/branch-common.md` 第四节格式，汇总 Q3、Q4、Q5、Q7、Q8、Q9、Q10、Q12 的支持证据、最强反证与未决缺口。

### B7. 引用与完成标记
- 在文末 `## 引用` 节按 `[引用1]`…`[引用N]` 顺序列出全部引用（研报带 `doc_id`，专家访谈带 transcript ID 与身份，内部库带 `document_id`，与正文一一对应），末行写 `<!-- 完成 -->`。
