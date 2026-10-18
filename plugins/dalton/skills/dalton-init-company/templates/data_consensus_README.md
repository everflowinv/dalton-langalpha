<!--
写入者：分析师 agent（取数时）或夜间预检探针（P4）或旧认知迁移脚本（P1.4）
何时写：每次调用 researchdata.finance(route="estimates", ticker=T) 获取卖方一致预期时
读写模式：只追加新快照文件（每个时间点一个新 JSON 文件，严禁修改或覆盖历史快照）
参考规范：P0D-P1 §P0-D.6、PK.1 工作区文件布局
-->

# 卖方一致预期快照（`data/consensus/`）

Yahoo Finance（`yfinance`）的一致预期接口不提供历史 as-of 查询。为了追踪市场一致预期的修正轨迹（Earnings Revision），每次拉取 `researchdata.finance(route="estimates", ticker="<TICKER>")` 时，必须将返回结果存为本目录下的时间戳快照文件。

## 命名与写入规则

- **文件名格式**：`data/consensus/<YYYY-MM-DD_HHMM>.json`（UTC 或统一时区时间戳，例如 `2026-10-18_0930.json`）。
- **只追加不覆盖**：每次取数生成一个新文件，严禁修改已有快照文件。
- **口径标注**：
  - 正文引用一致预期数字时一律标为 `street_estimate`，写明来源 `"Yahoo Finance via yfinance"`、`fetched_at`、期间键（`0q`、`+1q`、`0y`、`+1y`）与分析师人数。
  - 快照内的 `implied_model` 为模型推导值（默认使用最近一季税率），引用时必须标为 `model-derived`，不得当作卖方一致预期。
  - 一致预期仅作参照，不得自动覆盖我们的模型假设（house case）。

## 快照 JSON Schema

快照文件内容为 `estimates` 路由返回的原始 JSON 对象，并在顶层附加 `_meta` 字段：

```json
{
  "_meta": {
    "route": "finance/estimates",
    "ticker": "META",
    "fetched_at": "2026-10-18T09:30:00Z",
    "source": "Yahoo Finance via yfinance"
  },
  "ticker": "META",
  "earnings_estimates": {},
  "revenue_estimates": {},
  "eps_trend": {},
  "eps_revisions": {},
  "price_targets": {},
  "recommendations": {},
  "earnings_dates": [],
  "implied_model": {}
}
```
