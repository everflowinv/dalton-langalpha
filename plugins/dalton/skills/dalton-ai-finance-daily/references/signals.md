# 信号记录、历史比较与入库

信号库只保存本轮确认、并且有投资含义的 GitHub 与业绩会 signal，以及它们的证据和 `gemini-embedding-2` 向量。候选、没有信号的仓库、没有信号的 transcript 和全文不入库。调用顺序见 `SKILL.md`。

## 1. 记录字段

`signal_records` 的形状是 `{"records":[...]}`。每条只使用这些字段：

- `record_key`
- `source_type`：`github` 或 `earnings`
- `external_id`
- `event_time`：带时区的 ISO 时间，前 10 个字符是 `YYYY-MM-DD`
- `title`
- `entity`
- `industry`
- `url`：https 地址；没有则空字符串
- `summary`
- `evidence`
- `metrics`：数组，可以是空数组
- `signal_types`：数组
- `signal_direction`：`positive`、`negative` 或 `mixed`
- `evidence_status`
- `qa_concern`
- `management_response`
- `management_response_status`：空字符串、`direct`、`partial` 或 `evasive_or_unanswered`
- `tam_mapping`
- `investment_implication`
- `tags`：数组

`summary`、`tam_mapping`、`investment_implication` 和 `external_id` 都要有内容。不使用 `full_text`、`transcript`、`content`、`body`、`text`、`document`、`raw_text`、`path`、`file`、`db`、`api_key` 这些键。

GitHub 的 `record_key` 是 `github:<owner/repo>:<YYYY-MM-DD>`。日期是该条 `event_time` 的前 10 个字符。`external_id` 是 `owner/repo`。`event_time` 使用本批 GitHub 输出的 `generated_at`，表示本次采集观察时间。`metrics` 保留官方 `complete_through`、`daily_gain`、`gain_7d`、`daily_velocity_3d` 和 `velocity_change_pct`，供以后检索；这些数字不进入 GitHub 栏目。官方星数期间仍由 `complete_through` 表示。同一仓库出现新的事件日期时使用新的 `record_key`。`url` 是该仓库的 https 地址。`tags` 含一条 `upstream:` 加 `github.md` 里的小写标识。没有相关 Q&A 时，三个 Q&A 字段都是空字符串。

业绩会的 `record_key` 是 `earnings:<doc_id>`，`external_id` 是该 `doc_id`。`event_time` 沿用采集器的日期解析规则：优先 `rank_date`，缺失或无法解析时使用 `publish_time`；有时区则换算为香港时间，无时区则按香港时间解释。只有公历日期时，采用该日 `00:00:00+08:00` 的解析值，并在 `metrics` 中注明 `source_time_precision=date`。每个 `disposition` 为 `signal` 的候选恰好一条，`watchlist` 和 `reject` 不出现。

`management_response_status` 为空时，`qa_concern` 和 `management_response` 都为空。`direct` 和 `partial` 两边都有内容。`evasive_or_unanswered` 至少写明分析师的问题，回答文本可以为空。

## 2. 稳定标签

`signal_types` 使用下面的稳定英文标签，同义表达归一到这些词，不按日期新造标签。

正面：`ai_revenue`、`ai_arr_bookings`、`cost_productivity`、`headcount_displacement`、`margin_impact`、`ai_capex`、`gpu_compute_demand`、`agent_adoption`、`developer_tooling`、`workflow_automation`。

负面：`ai_revenue_pressure`、`ai_pricing_pressure`、`ai_demand_displacement`、`ai_cost_overrun`、`ai_opex_burden`、`ai_capex_burden`、`ai_margin_pressure`、`ai_execution_delay`、`analyst_ai_concern`。

新原语扩散的条目另加 `upstream_primitive_adoption`。采用条件见 `github.md`。本节的 matches 用来写趋势。

`signal_direction` 保持正面的 `positive`、负面的 `negative`、同时存在两边经营影响的 `mixed`。负面信号保持 `negative`。

## 3. 入库前的历史

`compare` 之后用 `read` 读完比较 JSON。正文是 `comparisons` 数组：每项有 `current` 和 `matches`。`matches` 含 `record_key`、`source_type`、`external_id`、`event_time`、`entity`、`signal_direction`、`evidence_status`、`summary`、`evidence`、`signal_types`、`shared_tags`、`embedding_similarity`。摘要里的 `comparison_count` 不代替这些 matches。

历史状态只用 compare 返回的三个 `before_ingest` 字段和 `search_modes`。`search_modes` 含 `empty_history` 时，表示入库前历史库为空、没有旧记录可比。这不表示向量未启用。历史库非空且 `search_modes` 含 `fts` 时，来源说明写“历史检索 FTS 回退”。其他模式按返回值填写，例如 `embedding`。

判断共性时：

- 可以跨日、跨行业，也可以看 GitHub 到企业采用的同一主题。
- 趋势至少有 2 个独立实体、方向相同。同一家公司的不同季度仍是一个实体。
- 企业当前经营趋势看最近 90 天。GitHub 与业绩会的跨源传导看最近 30 天。更早的相似记录写成“历史类比”。
- 方向、口径和 `evidence_status` 要一致。一个 `actual` 与一个远期 target 不并列成已经兑现的趋势。
- 正负记录都参与检索。方向相反时写成分歧、反向证据或利润池迁移。多个独立实体的相同负面影响可以写成负面行业趋势。
- 两个以上独立实体且跨行业时写“跨行业共性”。同行业证据足够时写“行业趋势”。只有一个实体时写“单公司/单项目信号”。

`#### 跨日共性与新趋势` 回答四件事：今天的信号与哪些历史项目或公司相似，证据有没有变强，开源开发者动量有没有传导到企业收入或降本，可能对应什么投资机会。上游采用的写法见 `github.md`。

即使形成不了趋势，单点高质量 signal 仍然入库，供以后的日期检索。
