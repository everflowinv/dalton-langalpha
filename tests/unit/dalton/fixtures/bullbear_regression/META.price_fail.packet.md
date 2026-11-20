# META 跟踪数据包 20261110-2250

- 窗口：2026-11-07 至 2026-11-10（美东）
- 采集时间：2026-11-10T22:50:00-05:00
- 内部库重试起点：无
- 一致预期快照：`data/consensus/2026-11-10_2250.json`
- 是否允许短路结束（short_circuit_eligible）：false
- 新闻和待核实线索仍要在研究步骤里核对，采集阶段不短路。
- 取数失败或未完成：
  - estimates：partial: field_status={"earnings_estimate": "ok", "revenue_estimate": "ok", "eps_trend": "ok", "eps_revisions": "ok", "price_targets": "ok", "price": "error"}

## 自动触发项
- 无

## 一致预期
- 收盘 None：None，1 日 None%，5 日 None%
- 修正差值：`{"eps_0q_vs_7d_pct": null, "eps_0q_vs_30d_pct": null, "eps_+1q_vs_7d_pct": null, "eps_+1q_vs_30d_pct": null, "eps_0y_vs_7d_pct": null, "eps_0y_vs_30d_pct": null, "eps_+1y_vs_7d_pct": null, "eps_+1y_vs_30d_pct": null, "fy1_revisions_7d": {"up": null, "down": null}}`

## 隐含费用
- 来源：2Q26 8-K 附件 99.1：净利润 158.48 亿美元 / 稀释 EPS 6.18 美元 ≈ 25.6 亿股；有效税率 16%；经营利润 187.75 亿与税前 187.56 亿几乎相等，其他收入按 0
- 假设对应财年：0y = FY2026，截至 2026-12，其他收入 0
- 假设对应财年：+1y = FY2027，截至 2027-12，其他收入 0
- 本财年（FY2026，period_end 2026-12-31，其他收入取 0y）：收入 100.0、EPS 10.0、税前 30.5、总费用 69.5、经营利润率 30.5%、费用每多 100 亿 EPS -3.28、收入每多 1% EPS 0.33
- +1y：+1y 缺少 period_end，无法确认财年，未计算隐含费用

## 内部库
- 无新文档
- 全文读完没有别名、记为无关：0 篇
- 来源状态：`{"tmtb": {"operation": "keyword-search", "ok": true, "status": "ok", "returned": 0, "pages": 1, "incomplete": false, "error": null, "warnings": [], "limit": 60, "read_cap": 12, "request": {"route": "tmtb", "operation": "search", "query": "Meta META Zuckerberg Muse Instagram WhatsApp Hyperion Prometheus", "start": "2026-11-07", "end": "2026-11-10", "limit": 60, "params": {"media": "text", "search_mode": "keyword"}}, "pending_reads": []}, "sales-notes": {"operation": "keyword-search", "ok": true, "status": "ok", "returned": 0, "pages": 1, "incomplete": false, "error": null, "warnings": [], "limit": 60, "read_cap": 12, "request": {"route": "sales-notes", "operation": "search", "query": "Meta META Zuckerberg Muse Instagram WhatsApp Hyperion Prometheus", "start": "2026-11-07", "end": "2026-11-10", "limit": 60, "params": {"media": "text", "search_mode": "keyword"}}, "pending_reads": []}, "funda-ai": {"operation": "keyword-search", "ok": true, "status": "ok", "returned": 0, "pages": 1, "incomplete": false, "error": null, "warnings": [], "limit": 60, "read_cap": 12, "request": {"route": "funda-ai", "operation": "search", "query": "Meta META Zuckerberg Muse Instagram WhatsApp Hyperion Prometheus", "start": "2026-11-07", "end": "2026-11-10", "limit": 60, "params": {"media": "text", "search_mode": "keyword"}}, "pending_reads": []}, "company-wiki": {"operation": "list", "ok": true, "status": "ok", "returned": 0, "pages": 1, "incomplete": false, "error": null, "warnings": [], "limit": 100, "read_cap": 12, "request": {"route": "company-wiki", "operation": "list", "limit": 100, "params": {"indexed_since": "2026-11-07", "media": "text"}}, "pending_reads": []}}`

## AlphaEngine
固定查询：
- Meta Platforms | 窗口 2026-11-04 至 2026-11-10 | complete=true | cursor=None
- Meta 脸书 Muse | 窗口 2026-11-04 至 2026-11-10 | complete=true | cursor=None
- 无

## SEC
- 无
- 状态：`[{"form": "8-K", "ok": true, "count": 0, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "META", "form": "8-K", "limit": 3}, "floor": "2026-11-05"}, {"form": "10-Q", "ok": true, "count": 0, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "META", "form": "10-Q", "limit": 3}, "floor": "2026-11-05"}, {"form": "10-K", "ok": true, "count": 0, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "META", "form": "10-K", "limit": 3}, "floor": "2026-11-05"}, {"form": "S-3ASR", "ok": true, "count": 0, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "META", "form": "S-3ASR", "limit": 3}, "floor": "2026-11-05"}, {"form": "424B2", "ok": true, "count": 0, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "META", "form": "424B2", "limit": 3}, "floor": "2026-11-05"}, {"form": "424B5", "ok": true, "count": 0, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "META", "form": "424B5", "limit": 3}, "floor": "2026-11-05"}, {"form": "FWP", "ok": true, "count": 0, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "META", "form": "FWP", "limit": 3}, "floor": "2026-11-05"}]`

## 新闻
- 状态：pending_agent_search，需要代理用 WebSearch/WebFetch 打开原文
- 窗口：2026-11-07 至 2026-11-10
- Meta Platforms analyst estimates price target downgrade upgrade
- Meta Platforms Muse capex financing regulation news

## X
- 账号池 0 个；噪声 0 条，提交前不写入 seen
- likes、reposts、replies、views 按 x-timeline 归一化结果保存；bookmarks 上游没有返回，记为 null，排序只用 likes+3*reposts。
- 查询：$META min_faves:150 -filter:replies
- 查询：$META (capex OR opex OR EPS OR estimates OR guidance OR margin OR FCF OR multiple OR valuation OR Muse) min_faves:40 -filter:replies
- 查询：(Meta OR Zuckerberg) (capex OR opex OR "free cash flow" OR depreciation OR Muse) (estimates OR EPS OR margin OR ROI OR revenue) min_faves:60 -filter:replies
- 无
- 状态：`[{"query": "$META min_faves:150 -filter:replies", "operation": "search", "ok": true, "count": 0, "has_more": false, "request": {"route": "x-timeline", "operation": "search", "query": "$META min_faves:150 -filter:replies", "since": "2026-11-07", "until": "2026-11-12", "count": 40}}, {"query": "$META (capex OR opex OR EPS OR estimates OR guidance OR margin OR FCF OR multiple OR valuation OR Muse) min_faves:40 -filter:replies", "operation": "search", "ok": true, "count": 0, "has_more": false, "request": {"route": "x-timeline", "operation": "search", "query": "$META (capex OR opex OR EPS OR estimates OR guidance OR margin OR FCF OR multiple OR valuation OR Muse) min_faves:40 -filter:replies", "since": "2026-11-07", "until": "2026-11-12", "count": 40}}, {"query": "(Meta OR Zuckerberg) (capex OR opex OR \"free cash flow\" OR depreciation OR Muse) (estimates OR EPS OR margin OR ROI OR revenue) min_faves:60 -filter:replies", "operation": "search", "ok": true, "count": 0, "has_more": false, "request": {"route": "x-timeline", "operation": "search", "query": "(Meta OR Zuckerberg) (capex OR opex OR \"free cash flow\" OR depreciation OR Muse) (estimates OR EPS OR margin OR ROI OR revenue) min_faves:60 -filter:replies", "since": "2026-11-07", "until": "2026-11-12", "count": 40}}]`

collect 不修改 seen.json、last_run.json、updates.log。
