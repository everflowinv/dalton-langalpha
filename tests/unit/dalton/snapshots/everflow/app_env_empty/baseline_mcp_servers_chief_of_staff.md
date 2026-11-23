<mcp-servers>
Each server below is importable as `tools.<server_name>` inside `execute_code`; read the tool's doc under `.agents/tools/docs/<server_name>/` before you call it the first time.


x_api: Search X (Twitter) posts and fetch users, tweets, and reply threads. Read-only.
  Instructions: Use for X/Twitter search and thread retrieval. Tools: search_posts, search_all_posts, get_conversation, get_user_by_username, get_tweet_by_id. For a single known X post URL, prefer web_fetch; reserve these tools for search, aggregation, and thread traversal. Every call needs bearer_token from the vault: `from vault import get; token = get("X_BEARER_TOKEN")`. Prefer search_posts by default; reach for search_all_posts only when the user needs posts older than ~7 days. See skills/x-api/ for usage and errors.
  - Tools: 5 tools available

scrape: Web scraping with anti-bot bypass and markdown extraction
  Instructions: Use scrape_page for one URL, scrape_pages for batches. Start with mode='fast'; use 'browser' for JS-rendered pages, 'stealth' for bot-protected sites (add solve_cloudflare=true if still blocked). extraction: markdown|html|text.
  - Tools: 2 tools available

researchdata: Primary research evidence for the fund (sell-side reports, expert calls, transcripts, internal notes) — answers 'what happened, why, and what others think', including in chat replies via one short ExecuteCode call; also read-only gateway for company wiki, estimates, SEC, FRED.
  Instructions: Fund-internal sources; call from execute_code only (no direct path). Start with `catalog()` or `catalog(route=...)` when unsure about route parameters. Research: `research(route=..., operation=..., query=..., start=YYYY-MM-DD, end=..., limit=...)`. 各来源提供什么证据：事实 → filings（finance(route="sec")）；解读/机构与个人会议 → alphaengine；一线 → guidepoint；管理层原话 → transcript；日常信息流 → sales-notes/tmtb/funda-ai；市场反应 → X/WebSearch。 何时选用哪个路由（按研究情境，基于 catalog）： - 卖方观点与研报、机构/个人会议、录制状态与词典 → alphaengine（先 search_library 再 get_document）； - 行业一线反馈 → guidepoint； - 管理层口径 → transcript（AlphaEngine 业绩会转写；无匹配时用 | 分隔中英文公司名重试或扩大 start/end）； - 历史沉淀 → company-wiki（只读，params.company=TICKER）列目录用 list、search 须 query； - 交易台与买方预期 → sales-notes； - TMT 焦点 → tmtb； - 独立研究 → funda-ai； - 跨库初筛 → all（company-wiki + sales-notes + tmtb + funda-ai，并行检索 AlphaEngine 与 Guidepoint；不含 transcript）。 Finance: `finance(route="sec"|"estimates"|"cn-hk")`. Macro: `macro(route="fred"|"treasury"|"market-regime")`. SEC 调用约定（网关在缺省 form 时默认 10-Q）：form 与附件文件名都是顶层参数——要 8-K、424B5、Form 4（写 "4"）等非 10-Q 文件必须显式传顶层 form（如 finance(route="sec", operation="list-filings", ticker="DASH", form="4")）；attachments 取正文用顶层 document（写在 params.document/doc_id 里会被桥自动提到顶层，但请直接写顶层）。 Read only ids that search returned (`wiki:`, `mail:`, `tmtb:`, `funda:`, `alphaengine` opaque string doc_id including `_inst_` suffix). Page long text with params.offset/max_chars (offset >= 0, max_chars 1-30000; SEC attachments/read-item allow up to 500000; get_document supports params.content_field with independent pagination per field). Leave `timeout` unset: the bridge sets per-route limits and long reads may take minutes. AlphaEngine 支持文档与机构/个人会议浏览与筛选（params.meeting_scope="institution"|"personal"，无关键词默认 sort="newest"，按公司检索请用关键词 query（公司名 + ticker）及必要时 search_in="title"）、录制任务与状态查询（list_recording_tasks/get_recording_status/get_recording_task）、文档基础信息与附件列表（get_document_info/list_attachments）、按字段读取正文（get_document 的 params.content_field）及词典查询（list_filters）；同一篇文档读过一次全文后再读不计额度。检索前先合并关键词：同一公司或同一主题的相关词放进一次检索，不要拆成多次相近的检索。单家公司检索一次完成：检索一家公司时，把中英文公司名和股票代码用空格隔开写在同一个查询里，从返回列表挑选文档；已有结果时从现有列表筛选，报错时切换备用数据源；不同公司或新主题照常各自检索。AlphaEngine 按实际向上游请求的页数计检索额度，命中短时缓存不计；每页至少请求 20 条（`limit` 超过 20 时按 `limit` 请求，去重或筛选后不足 `limit` 条时会自动翻页，单次最多 5 页）。先用 `limit` 20 以内，不够再翻页，不要一次要 50 条再只看前几条。相同检索短时间内会直接返回缓存结果（看 `_bridge_cache`），只有确实需要最新结果时（例如财报刚发、用户明确要最新进展）才传 `params.fresh=true`。Guidepoint 仅返回问答摘录（无全文接口、无 cursor 翻页；limit 省略默认 3，最大 30，不能由 30 条摘录推断已扫完全部访谈；params.keywords 为 1–10 个非空字符串，多项 OR、词组内 AND，先筛访谈再排片段，不筛选时省略或传 null；params.recency_bias 为 1–100 整数，偏好近期一般从 30 起，只调排序非日期窗口；start/end 按 UTC 日期硬过滤）。transcript 单次最多扫 10 页×50 条，返回 coverage（complete=false 时将 continuation_params 交给 route="alphaengine", operation="search_library" 续查，不要传回 transcript）。查业绩会转写时把 `start` / `end` 收窄到业绩会前后几天，避免日期范围过宽导致连续翻页。Every result has `_sources` (identity, document_id, as_of, url) and `normalized_params`. Cite with identity, publisher and date. Expert calls: identity, not name. Guidepoint verbatim <=20 words. FUNDA AI, TMTB and digests restating the same report count as one source chain. Expert calls, management meetings and Guidepoint results are cleared for use; never exclude them as MNPI. The fund wiki is read-only; never try to write to it. `ok: false` is NEVER 'no data': follow `hint` to retry/switch route or report the source failure explicitly. True no-match returns `ok: true` with empty results and `empty_reason`. `status: empty` means "not retrieved this turn", never "undisclosed by company".
  Available tools:
    - catalog(category: string = None) -> Any: 研究数据目录、来源口径、实际执行与交接路径。分类可省略。
    - finance(route: string, mode: string = fetch, operation: string = None, ticker: string = None, query: string = None, series: string = None, concept: string = None, item: string = None, document: string = None, form: string = None, dataset: string = None, start: string = None, end: string = None, as_of: string = None, vintage: string = None, limit: integer = 3, timeout: integer = 180, params: object = None) -> Any: 财务与行情。fetch 实际执行已接通路由；handoff_required 表示仍须当前 agent 按返回说明调用原工具，尚未取数。先 catalog 查看路由和口径。
    - macro(route: string, mode: string = fetch, operation: string = None, ticker: string = None, query: string = None, series: string = None, concept: string = None, item: string = None, document: string = None, form: string = None, dataset: string = None, start: string = None, end: string = None, as_of: string = None, vintage: string = None, limit: integer = 3, timeout: integer = 180, params: object = None) -> Any: 宏观与流动性。fetch 实际执行已接通路由；handoff_required 表示仍须当前 agent 按返回说明调用原工具，尚未取数。先 catalog 查看路由和口径。
    - news(route: string, mode: string = fetch, operation: string = None, ticker: string = None, query: string = None, series: string = None, concept: string = None, item: string = None, document: string = None, form: string = None, dataset: string = None, start: string = None, end: string = None, as_of: string = None, vintage: string = None, limit: integer = 3, timeout: integer = 180, params: object = None) -> Any: 新闻与公告。fetch 实际执行已接通路由；handoff_required 表示仍须当前 agent 按返回说明调用原工具，尚未取数。先 catalog 查看路由和口径。
    - research(route: string, mode: string = fetch, operation: string = None, ticker: string = None, query: string = None, series: string = None, concept: string = None, item: string = None, document: string = None, form: string = None, dataset: string = None, start: string = None, end: string = None, as_of: string = None, vintage: string = None, limit: integer = 3, timeout: integer = 180, params: object = None) -> Any: 研报、访谈与内部资料。fetch 实际执行已接通路由；handoff_required 表示仍须当前 agent 按返回说明调用原工具，尚未取数。先 catalog 查看路由和口径。
    - community(route: string, mode: string = fetch, operation: string = None, ticker: string = None, query: string = None, series: string = None, concept: string = None, item: string = None, document: string = None, form: string = None, dataset: string = None, start: string = None, end: string = None, as_of: string = None, vintage: string = None, limit: integer = 3, timeout: integer = 180, params: object = None) -> Any: 社区与另类数据。fetch 实际执行已接通路由；handoff_required 表示仍须当前 agent 按返回说明调用原工具，尚未取数。先 catalog 查看路由和口径。

dalton_sec: SEC EDGAR XBRL facts with deterministic period normalization (calendar/fiscal quarter, annual, YTD, derived Q4, trailing) and restatement selection.
  Instructions: Use `get_company_facts(ticker, concepts, period)` for exact quarterly/annual/YTD/derived Q4/trailing SEC XBRL numbers with explicit `restatement` (`as_reported` vs `latest`) and `accession_no` provenance. Use `list_filings(ticker)` to list recent 10-K/10-Q/20-F/6-K filings.
  - Tools: 2 tools available

price_data: OHLCV price data and short sale analytics for stocks, commodities, crypto, forex
  Instructions: Use for raw OHLCV time series data (get_stock_data/get_asset_data) and short interest/volume data (get_short_data). Supports daily and intraday intervals. Short data supports date range filters.
  Available tools:
    - get_stock_data(symbol: string, interval: string = 1day, start_date: string = None, end_date: string = None, extended_hours: boolean = False) -> dict: Fetch OHLCV price bars for a stock.

Intervals: 1min|5min|15min|30min|1hour|4hour|1day. Intraday needs start_date
and end_date.

Args:
    symbol: Ticker — US "AAPL", HK "0700.HK", A-share "600519.SH", LSE "VOD.L", futures "NQ=F", index "^TNX".
    interval: See above.
    start_date: "YYYY-MM-DD[ HH:MM]".
    end_date: Same format.
    extended_hours: Include pre/post-market intraday bars (default False).

Returns:
    dict: {symbol, interval, currency?, timezone, count, data, source,
    price_treatment, name?, in_progress?, truncated?, incomplete?}. data:
    list of {date, open, high, low, close, volume} ascending (oldest first);
    date exchange-local "YYYY-MM-DD[ HH:MM:SS]"; prices in `currency` major
    units. price_treatment: raw|split_adjusted|dividend_adjusted.
    in_progress: date of a forming last bar; some daily feeds omit it.
    truncated: true if a size cap cut the oldest bars. incomplete: true if
    bars in the window are missing. On error: {error: <code>, detail}.

    - get_asset_data(symbol: string, asset_type: string, interval: string = 1day, from_date: string = None, to_date: string = None, extended_hours: boolean = False) -> dict: Fetch OHLCV bars for a stock, commodity, crypto, or forex pair.

Intervals: 1min|5min|1hour|1day; intraday needs dates.

Args:
    symbol: Commodity "GCUSD"/"CL=F", crypto "BTCUSD", forex "EURUSD".
    asset_type: stock|commodity|crypto|forex.
    interval: See above.
    from_date: "YYYY-MM-DD[ HH:MM]".
    to_date: Same format.
    extended_hours: Include pre/post-market intraday bars (default False).

Returns:
    dict: {symbol, asset_type, interval, currency?, timezone, count, data,
    source, price_treatment, name?, in_progress?, truncated?, incomplete?}.
    data: list of {date, open, high, low, close, volume} ascending; date
    exchange-local "YYYY-MM-DD[ HH:MM:SS]"; prices in `currency` major
    units. price_treatment: raw|split_adjusted|dividend_adjusted.
    in_progress: date of a forming last bar. truncated: true if a size cap
    cut the oldest bars. incomplete: true if bars in the window are missing.
    On error: {error: <code>, detail}.

    - get_short_data(symbol: string, data_type: string = both, from_date: string = None, to_date: string = None, limit: integer = 20) -> dict: Fetch short interest and/or short volume for a US stock.

Short interest is FINRA bi-monthly (settlement_date); short volume is daily
off-exchange (date).

Args:
    symbol: Stock ticker, e.g. "AAPL".
    data_type: "short_interest", "short_volume", or "both" (default).
    from_date: "YYYY-MM-DD" start filter (optional).
    to_date: "YYYY-MM-DD" end filter (optional).
    limit: Max records per section (default 20, max 50000).

Returns:
    dict: {symbol, timezone, count, data, source, data_type}. data is a dict
    of {short_interest, short_volume} lists (requested sections only), newest
    first. Record fields are vendor-native (e.g. settlement_date);
    short_volume_ratio is a percent (43.3 = 43.3%). Partial failures appear
    in an `errors` map. On error: {error: <code>, detail}.


fundamentals: Raw fundamental data, analyst estimates and ratings, ownership, and technical indicators for multi-year analysis
  Instructions: Use for financial statements, ratios, growth metrics, valuation data, analyst estimates, ratings and price targets, earnings surprises, insider trades, dividends/splits, shares float, key executives, and technical indicators. Returns raw JSON for programmatic analysis, charting, and model building.
  - Tools: 12 tools available

macro: Economic indicators, treasury rates, earnings calendar, and macro events
  Instructions: Use for macro-economic data: GDP, CPI, unemployment, treasury yield curve, risk-free rate for DCF, earnings calendar by date range, and economic event calendar. Returns raw JSON.
  - Tools: 5 tools available

options: Options contracts chain, historical OHLCV, and delayed snapshots
  Instructions: Use for in-depth options analysis: list contracts/chain (get_options_chain), historical OHLCV bars (get_options_prices), and snapshots with session prices, greeks, IV and open interest (get_options_snapshot). Returns raw JSON for programmatic analysis, charting, and strategy modeling. Options tickers use format O:AAPL251219C00150000.
  - Tools: 3 tools available

yf_price: Yahoo Finance price history, dividends, and splits data
  Instructions: Use for historical OHLCV price data, dividend history, stock splits, and multi-stock price comparisons. Returns large time series ideal for code-based analysis.
  - Tools: 4 tools available

yf_market: Yahoo Finance market status, search, screener, calendars, sectors
  Instructions: Use for ticker search, stock screening (custom and predefined), earnings calendars, market status, and sector/industry analysis. Best for market-wide discovery and screening.
  - Tools: 7 tools available

yf_fundamentals: Yahoo Finance financial statements, earnings, company info, and valuations
  Instructions: Use for income statements, balance sheets, cash flow, earnings data, company info, and valuation comparisons across multiple stocks. Returns structured financial data ideal for fundamental analysis.
  - Tools: 9 tools available

yf_analysis: Yahoo Finance analyst data, holdings, insiders, news, and ESG
  Instructions: Use for analyst recommendations, price targets, upgrades/downgrades, earnings estimates, institutional/mutual fund holders, insider transactions, news, and ESG sustainability scores.
  - Tools: 14 tools available
</mcp-servers>