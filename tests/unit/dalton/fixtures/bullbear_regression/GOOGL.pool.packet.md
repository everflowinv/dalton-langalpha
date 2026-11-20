# GOOGL 跟踪数据包 20261110-2250

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
- 来源：2Q26 10-Q（accession 0001652044-26-000071）：净利润 1121.07 亿/稀释 EPS 9.11≈12.31 亿股；报表税率 19.1% 含投资收益按法定税率计税，核心税率按 2025 年约 17%。2026 年一致 EPS 含上半年 OI&E 1357 亿（Q2 含 SpaceX 等未实现收益 990 亿），0y 隐含费用失真，看 +1y
- 假设对应财年：0y = FY2026，截至 2026-12，其他收入 0
- 假设对应财年：+1y = FY2027，截至 2027-12，其他收入 0
- 本财年（FY2026，period_end 2026-12-31，其他收入取 0y）：收入 100.0、EPS 10.0、税前 148.3、总费用 -48.3、经营利润率 148.3%、费用每多 100 亿 EPS -0.67、收入每多 1% EPS 0.07
- 下一财年（FY2027，period_end 2027-12-31，其他收入取 +1y）：收入 120.0、EPS 12.0、税前 178.0、总费用 -58.0、经营利润率 148.3%、费用每多 100 亿 EPS -0.67、收入每多 1% EPS 0.08

## 内部库
- tmtb:googl-1 | 2026-11-10 | tmtb | GOOGL Note | research/tracker/20261110-2250/excerpts/tmtb_googl-1.txt
Alphabet revenue growth update.
- 全文读完没有别名、记为无关：0 篇
- 来源状态：`{"tmtb": {"operation": "keyword-search", "ok": true, "status": "ok", "returned": 1, "pages": 1, "incomplete": false, "error": null, "warnings": [], "limit": 60, "read_cap": 12, "request": {"route": "tmtb", "operation": "search", "query": "Alphabet Google GOOGL GOOG Gemini YouTube Waymo DeepMind TPU TPUs Pichai GCP Antigravity", "start": "2026-11-07", "end": "2026-11-10", "limit": 60, "params": {"media": "text", "search_mode": "keyword"}}, "pending_reads": []}, "sales-notes": {"operation": "keyword-search", "ok": true, "status": "ok", "returned": 0, "pages": 1, "incomplete": false, "error": null, "warnings": [], "limit": 60, "read_cap": 12, "request": {"route": "sales-notes", "operation": "search", "query": "Alphabet Google GOOGL GOOG Gemini YouTube Waymo DeepMind TPU TPUs Pichai GCP Antigravity", "start": "2026-11-07", "end": "2026-11-10", "limit": 60, "params": {"media": "text", "search_mode": "keyword"}}, "pending_reads": []}, "funda-ai": {"operation": "keyword-search", "ok": true, "status": "ok", "returned": 0, "pages": 1, "incomplete": false, "error": null, "warnings": [], "limit": 60, "read_cap": 12, "request": {"route": "funda-ai", "operation": "search", "query": "Alphabet Google GOOGL GOOG Gemini YouTube Waymo DeepMind TPU TPUs Pichai GCP Antigravity", "start": "2026-11-07", "end": "2026-11-10", "limit": 60, "params": {"media": "text", "search_mode": "keyword"}}, "pending_reads": []}, "company-wiki": {"operation": "list", "ok": true, "status": "ok", "returned": 0, "pages": 1, "incomplete": false, "error": null, "warnings": [], "limit": 100, "read_cap": 12, "request": {"route": "company-wiki", "operation": "list", "limit": 100, "params": {"indexed_since": "2026-11-07", "media": "text"}}, "pending_reads": []}}`

## AlphaEngine
固定查询：
- Alphabet Google | 窗口 2026-11-04 至 2026-11-10 | complete=true | cursor=None
- 谷歌 Gemini 云 TPU | 窗口 2026-11-04 至 2026-11-10 | complete=true | cursor=None
- alphaengine:ae-googl-1 | 2026-11-10 | GOOGL AE Report

## SEC
- sec:0000000001-26-000001 | 8-K | 2026-11-10
- 状态：`[{"form": "8-K", "ok": true, "count": 1, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "GOOGL", "form": "8-K", "limit": 3}, "floor": "2026-11-05"}, {"form": "10-Q", "ok": true, "count": 0, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "GOOGL", "form": "10-Q", "limit": 3}, "floor": "2026-11-05"}, {"form": "10-K", "ok": true, "count": 0, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "GOOGL", "form": "10-K", "limit": 3}, "floor": "2026-11-05"}, {"form": "S-3ASR", "ok": true, "count": 0, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "GOOGL", "form": "S-3ASR", "limit": 3}, "floor": "2026-11-05"}, {"form": "424B2", "ok": true, "count": 0, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "GOOGL", "form": "424B2", "limit": 3}, "floor": "2026-11-05"}, {"form": "424B5", "ok": true, "count": 0, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "GOOGL", "form": "424B5", "limit": 3}, "floor": "2026-11-05"}, {"form": "FWP", "ok": true, "count": 0, "incomplete": false, "status": "ok", "error": null, "request": {"route": "sec", "operation": "list-filings", "ticker": "GOOGL", "form": "FWP", "limit": 3}, "floor": "2026-11-05"}]`

## 新闻
- 状态：pending_agent_search，需要代理用 WebSearch/WebFetch 打开原文
- 窗口：2026-11-07 至 2026-11-10
- Alphabet Google analyst estimates price target downgrade upgrade
- Google antitrust search remedies ad tech Gemini TPU news

## X
- 账号池 1 个；噪声 0 条，提交前不写入 seen
- likes、reposts、replies、views 按 x-timeline 归一化结果保存；bookmarks 上游没有返回，记为 null，排序只用 likes+3*reposts。
- 查询：($GOOGL OR $GOOG) min_faves:150 -filter:replies
- 查询：($GOOGL OR $GOOG) (TPU OR capex OR cloud OR search OR EPS OR estimates OR margin OR multiple OR valuation OR Gemini) min_faves:40 -filter:replies
- 查询：(Alphabet OR Google) (TPU OR "Google Cloud" OR "search revenue" OR capex) (EPS OR estimates OR margin OR backlog OR growth) min_faves:60 -filter:replies
- x:x-googl-pool | pool | @@Analyst1 | 2026-11-10T13:00:00-05:00 | 赞 80 转 8 收藏 None | 信号词 0
  $GOOGL cloud channel check
- x:x-googl-1 | search | @analyst1 | 2026-11-10T12:00:00-05:00 | 赞 50 转 5 收藏 None | 信号词 2
  $GOOGL EPS and revenue beat
- 作者资料待补充：
  - @Analyst1 | 本地作者缓存没有这个账号
  - analyst1 | 本地作者缓存没有这个账号
- 状态：`[{"query": "($GOOGL OR $GOOG) min_faves:150 -filter:replies", "operation": "search", "ok": true, "count": 1, "has_more": false, "request": {"route": "x-timeline", "operation": "search", "query": "($GOOGL OR $GOOG) min_faves:150 -filter:replies", "since": "2026-11-07", "until": "2026-11-12", "count": 40}}, {"query": "($GOOGL OR $GOOG) (TPU OR capex OR cloud OR search OR EPS OR estimates OR margin OR multiple OR valuation OR Gemini) min_faves:40 -filter:replies", "operation": "search", "ok": true, "count": 1, "has_more": false, "request": {"route": "x-timeline", "operation": "search", "query": "($GOOGL OR $GOOG) (TPU OR capex OR cloud OR search OR EPS OR estimates OR margin OR multiple OR valuation OR Gemini) min_faves:40 -filter:replies", "since": "2026-11-07", "until": "2026-11-12", "count": 40}}, {"query": "(Alphabet OR Google) (TPU OR \"Google Cloud\" OR \"search revenue\" OR capex) (EPS OR estimates OR margin OR backlog OR growth) min_faves:60 -filter:replies", "operation": "search", "ok": true, "count": 1, "has_more": false, "request": {"route": "x-timeline", "operation": "search", "query": "(Alphabet OR Google) (TPU OR \"Google Cloud\" OR \"search revenue\" OR capex) (EPS OR estimates OR margin OR backlog OR growth) min_faves:60 -filter:replies", "since": "2026-11-07", "until": "2026-11-12", "count": 40}}, {"pool": "@Analyst1", "operation": "tweets", "ok": true, "count": 1, "kept": 1, "has_more": false, "request": {"route": "x-timeline", "operation": "tweets", "handle": "@Analyst1", "since": "2026-11-07", "until": "2026-11-11", "count": 20}}]`

collect 不修改 seen.json、last_run.json、updates.log。
