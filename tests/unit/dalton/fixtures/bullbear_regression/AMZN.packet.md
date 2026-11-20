# AMZN 跟踪数据包 20261110-2250

- 窗口：2026-11-07 至 2026-11-10（美东）
- 采集时间：2026-11-10T22:50:00-05:00
- 内部库重试起点：无
- 一致预期快照：`data/consensus/2026-11-10_2250.json`
- 是否允许短路结束（short_circuit_eligible）：false
- 新闻和待核实线索仍要在研究步骤里核对，采集阶段不短路。

## 自动触发项
- 发现 1 份新 SEC filing

## 一致预期
- 收盘 2026-11-10：400.0，1 日 1.2%，5 日 2.5%
- 修正差值：`{"eps_0q_vs_7d_pct": null, "eps_0q_vs_30d_pct": null, "eps_+1q_vs_7d_pct": null, "eps_+1q_vs_30d_pct": null, "eps_0y_vs_7d_pct": null, "eps_0y_vs_30d_pct": null, "eps_+1y_vs_7d_pct": 0.0, "eps_+1y_vs_30d_pct": null, "fy1_revisions_7d": {"up": 1, "down": 0}}`

## 隐含费用
- 来源：2Q26 10-Q（accession 0001018724-26-000026）：Q2 稀释股数 109.0 亿，上半年其他收入 690.6 亿（Anthropic 优先股重估 628 亿等）；FY2025 10-K 有效税率 19.6%。Yahoo 0y=FY2026（含上半年 Anthropic 重估收益）、+1y=FY2027；一致 EPS 为 GAAP，H2 与 FY27 基本不含投资收益
- 假设对应财年：0y = FY2026，截至 2026-12，其他收入 69.1
- 假设对应财年：+1y = FY2027，截至 2027-12，其他收入 0
- 本财年（FY2026，period_end 2026-12-31，其他收入取 0y）：收入 100.0、EPS 10.0、税前 136.9、总费用 32.2、经营利润率 67.8%、费用每多 100 亿 EPS -0.73、收入每多 1% EPS 0.07
- 下一财年（FY2027，period_end 2027-12-31，其他收入取 +1y）：收入 120.0、EPS 12.0、税前 164.2、总费用 -44.2、经营利润率 136.9%、费用每多 100 亿 EPS -0.73、收入每多 1% EPS 0.09

## 内部库
- tmtb:amzn-1 | 2026-11-10 | tmtb | AMZN Note | research/tracker/20261110-2250/excerpts/tmtb_amzn-1.txt
Amazon revenue growth update.
- 全文读完没有别名、记为无关：0 篇
- 来源状态：`{"tmtb": {"operation": "keyword-search", "ok": true, "status": "ok", "returned": 1, "pages": 1, "incomplete": false, "error": null, "warnings": [], "limit": 60, "read_cap": 12, "request": {"route": "tmtb", "operation": "search", "query": "Amazon AMZN AWS Jassy Trainium Bedrock Kuiper Garman Rufus", "start": "2026-11-07", "end": "2026-11-10", "limit": 60, "params": {"media": "text", "search_mode": "keyword"}}, "pending_reads": []}, "sales-notes": {"operation": "keyword-search", "ok": true, "status": "ok", "returned": 0, "pages": 1, "incomplete": false, "error": null, "warnings": [], "limit": 60, "read_cap": 12, "request": {"route": "sales-notes", "operation": "search", "query": "Amazon AMZN AWS Jassy Trainium Bedrock Kuiper Garman Rufus", "start": "2026-11-07", "end": "2026-11-10", "limit": 60, "params": {"media": "text", "search_mode": "keyword"}}, "pending_reads": []}, "funda-ai": {"operation": "keyword-search", "ok": true, "status": "ok", "returned": 0, "pages": 1, "incomplete": false, "error": null, "warnings": [], "limit": 60, "read_cap": 12, "request": {"route": "funda-ai", "operation": "search", "query": "Amazon AMZN AWS Jassy Trainium Bedrock Kuiper Garman Rufus", "start": "2026-11-07", "end": "2026-11-10", "limit": 60, "params": {"media": "text", "search_mode": "keyword"}}, "pending_reads": []}, "company-wiki": {"operation": "list", "ok": true, "status": "ok", "returned": 0, "pages": 1, "incomplete": false, "error": null, "warnings": [], "limit": 100, "read_cap": 12, "request": {"route": "company-wiki", "operation": "list", "limit": 100, "params": {"indexed_since": "2026-11-07", "media": "text"}}, "pending_reads": []}}`

## AlphaEngine
固定查询：
- Amazon AWS Trainium capex retail margin | 窗口 2026-11-04 至 2026-11-10 | complete=true | cursor=None
- 亚马逊 AWS 云 资本开支 零售利润率 | 窗口 2026-11-04 至 2026-11-10 | complete=true | cursor=None
- alphaengine:ae-amzn-1 | 2026-11-10 | AMZN AE Report

## SEC
- sec:0000000001-26-000001 | 8-K | 2026-11-10
- 状态：`[{"form": "8-K", "ok": true, "count": 1, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "AMZN", "form": "8-K", "limit": 3}, "floor": "2026-11-05"}, {"form": "10-Q", "ok": true, "count": 0, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "AMZN", "form": "10-Q", "limit": 3}, "floor": "2026-11-05"}, {"form": "10-K", "ok": true, "count": 0, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "AMZN", "form": "10-K", "limit": 3}, "floor": "2026-11-05"}, {"form": "S-3ASR", "ok": true, "count": 0, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "AMZN", "form": "S-3ASR", "limit": 3}, "floor": "2026-11-05"}, {"form": "424B2", "ok": true, "count": 0, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "AMZN", "form": "424B2", "limit": 3}, "floor": "2026-11-05"}, {"form": "424B5", "ok": true, "count": 0, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "AMZN", "form": "424B5", "limit": 3}, "floor": "2026-11-05"}, {"form": "FWP", "ok": true, "count": 0, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "AMZN", "form": "FWP", "limit": 3}, "floor": "2026-11-05"}]`

## 新闻
- 状态：pending_agent_search，需要代理用 WebSearch/WebFetch 打开原文
- 窗口：2026-11-07 至 2026-11-10
- Amazon analyst estimates price target downgrade upgrade AWS
- Amazon AWS Trainium Anthropic capex Kuiper retail news

## X
- 账号池 0 个；噪声 0 条，提交前不写入 seen
- likes、reposts、replies、views 按 x-timeline 归一化结果保存；bookmarks 上游没有返回，记为 null，排序只用 likes+3*reposts。
- 查询：$AMZN min_faves:150 -filter:replies
- 查询：$AMZN (AWS OR capex OR Trainium OR EPS OR estimates OR margin OR backlog OR retail OR advertising OR Anthropic) min_faves:40 -filter:replies
- 查询：(Amazon OR AWS OR Jassy) (capex OR backlog OR Trainium OR "operating margin" OR "free cash flow") (EPS OR estimates OR margin OR growth OR guide) min_faves:60 -filter:replies
- x:x-amzn-1 | search | @analyst1 | 2026-11-10T12:00:00-05:00 | 赞 50 转 5 收藏 None | 信号词 2
  $AMZN EPS and revenue beat
- 作者资料待补充：
  - analyst1 | 本地作者缓存没有这个账号
- 状态：`[{"query": "$AMZN min_faves:150 -filter:replies", "operation": "search", "ok": true, "count": 1, "has_more": false, "request": {"route": "x-timeline", "operation": "search", "query": "$AMZN min_faves:150 -filter:replies", "since": "2026-11-07", "until": "2026-11-12", "count": 40}}, {"query": "$AMZN (AWS OR capex OR Trainium OR EPS OR estimates OR margin OR backlog OR retail OR advertising OR Anthropic) min_faves:40 -filter:replies", "operation": "search", "ok": true, "count": 1, "has_more": false, "request": {"route": "x-timeline", "operation": "search", "query": "$AMZN (AWS OR capex OR Trainium OR EPS OR estimates OR margin OR backlog OR retail OR advertising OR Anthropic) min_faves:40 -filter:replies", "since": "2026-11-07", "until": "2026-11-12", "count": 40}}, {"query": "(Amazon OR AWS OR Jassy) (capex OR backlog OR Trainium OR \"operating margin\" OR \"free cash flow\") (EPS OR estimates OR margin OR growth OR guide) min_faves:60 -filter:replies", "operation": "search", "ok": true, "count": 1, "has_more": false, "request": {"route": "x-timeline", "operation": "search", "query": "(Amazon OR AWS OR Jassy) (capex OR backlog OR Trainium OR \"operating margin\" OR \"free cash flow\") (EPS OR estimates OR margin OR growth OR guide) min_faves:60 -filter:replies", "since": "2026-11-07", "until": "2026-11-12", "count": 40}}]`

collect 不修改 seen.json、last_run.json、updates.log。
