# 业绩会：AI 的直接业务影响

业绩会材料来自 prepare 已经完成的扫描。本轮不再发起一次扫描，也不再消耗文章额度。全文用 `SKILL.md` 的 candidate 分页阅读。

扫描范围是 AlphaEngine Earnings Calls，类型 `IND_SECOND_5FA442AE`。关键词先在该类型内发现，再按 `doc_id` 去重。关键词覆盖 AI、artificial intelligence、generative AI、GenAI、machine learning、large language model、LLM、agentic AI、AI infrastructure、人工智能、大模型、智能体、AI 算力。检索不带市场、行业或市值条件；窗口用 `rank_date` 的香港时间，缺 `rank_date` 时才用 `publish_time`。

只有关键词命中的 transcript 会进入全文预算。预算最多 80 篇。超过 80 篇时，输出按 `coverage.selection_policy` 为 `relevance_ranked_top_n` 的顺序保留前 80，同分再看 `rank_date`。读取 `coverage.selection_score` 和 `selected_documents[].selection_rank`。来源说明使用 `report.md` 的固定句子，并写明跳过篇数。`get_document` 的失败保留在 `coverage.fetch_errors`，本轮不为同一篇自动再拉一次。

`coverage` 的口径是：关键词命中后、按相关性排序、在 80 篇预算内做的全文扫描。来源说明使用这个口径。

## 1. 每个候选都要判断

`candidate_doc_ids` 和 earnings JSON 的 `candidates` 全部审阅，不另设 12 篇上限。没有命中 AI 词群的 transcript 只留在 coverage 里，不建记录，也不用来凑正文。

候选带有 `context`、`negative_impact_hits`、`qa_cue_hits`、`negative_qa_priority`。上下文含义不完整、数字口径不清，或转写质量造成歧义时，读该 `doc_id` 的全文分页。

正面、负面和正负混合的经营影响都可以成为 signal。高优先级包括：AI 冲击收入、订单或客户，造成价格或份额压力，替代原有产品，费用或 capex 超支，毛利率下降，项目延期，执行风险上升。结论偏负面仍然保留。

存在投资相关的 actual、明确的 management guidance 或 target、客户赢单或需求迁移、可验证的生产部署，或有实质内容的分析师质疑时，不因“AI 贡献没有单独量化”而丢弃。用 `evidence_status`、方向和 Q&A 完整度保留不确定性。泛泛提到 AI、转写误命中、只有产品口号而没有投资含义的候选，disposition 为 `reject`。

一个 signal 都没有时，栏目写“本窗口没有找到可验证的 AI 直接影响披露”。

## 2. Q&A

`negative_qa_priority` 为 true 的候选，以及正文出现上一节高优先级负面影响的候选，必须阅读全文分页里的相关 Q&A。自动截取的 `context` 不够。

单独记下卖方分析师的 AI 质疑：原文里的姓名和机构、具体担忧、引用的经营指标或假设、管理层回答。原文没有姓名或机构时不补。

管理层回应记为三个状态之一：

- `direct`：正面回答。
- `partial`：部分回答。
- `evasive_or_unanswered`：回避或未回答。

并写明尚待验证的关键 KPI。分析师的问题说明资本市场在关注什么，本身不证明负面影响已经发生。管理层否认或尚无数据时，双方口径分开写，标成 `analyst concern` 或 `management claim`。这两种都不是 `actual`。

没有相关追问时，Q&A 三项留空，正文写“未发现相关分析师质疑”。

## 3. 每条 disposition

`earnings_dispositions` 的形状是 `{"records":[...]}`。每一个候选恰好一条，条数等于 `coverage.ai_candidates`，`doc_id` 与候选集合一一对应。`signal`、`watchlist`、`reject` 的数量之和等于这个条数。对不上时不写日报，也不调用信号保存。

每条只使用这些字段：

- `doc_id`
- `entity`
- `disposition`：`signal`、`watchlist` 或 `reject`
- `signal_direction`：`positive`、`negative` 或 `mixed`
- `evidence_status`：`actual`、`guidance/target` 或 `qualitative`。争议项另在 Q&A 字段标 `management claim` 或 `analyst concern`，已核实的那一层仍用这三个值。
- `reason`
- `key_evidence`
- `qa_checked`：布尔值。负向 Q&A 义务已经按上一节读完全文时为 true。没有这项义务、并且 context 足够判断时为 false。

`signal` 没有数量上限，全部进入 `signals.md` 的记录。`watchlist` 和 `reject` 不进入信号库。记录里不放全文、转写或路径。

`signal` 的 `signal_direction` 按经营影响填写，负面保持 `negative`。没有方向性经营影响的 `reject`，`signal_direction` 仍从这三个值里选最接近被排除表述的一个，并在 `reason` 写明它没有投资含义。

## 4. 栏目里的公司

每家公司按这个顺序写：

- **方向**：`正面`、`负面`或`混合`。
- **收入端**：AI 产品收入、ARR、订单、客户、价格、转化率或增量需求。没有则写“未量化”。
- **成本/费用端**：人员效率、成本节省、毛利率、opex、capex、重组或岗位变化。正负影响都写。没有则写“未量化”。
- **Q&A 争议**：分析师对 AI 冲击、投入回报、费用或 capex、毛利或执行风险的质疑，以及管理层回答完整度。没有相关追问则写“未发现相关分析师质疑”。
- **证据强度**：已实现的 actual、管理层 guidance 或 target，或定性表述。目标保持为目标。
- **投资含义**：新增收入、需求或份额迁移、收入被替代、价格压力、成本杠杆、费用或资本开支负担、毛利率拖累、执行风险，或仅产品叙事。
- 来源写公司、季度和 AlphaEngine `doc_id`。关键数字来自已经读过的 transcript 正文。

正文不按行业或市值删减。定量和投资含义最强的最多 8 家详细写，编号从 1 开始。其余高质量 signal 放入“其他机会雷达”，逐家一行。雷达里的公司仍然入库。
