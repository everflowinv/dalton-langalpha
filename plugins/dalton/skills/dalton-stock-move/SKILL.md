---
name: dalton-stock-move
description: "解释某只股票一段时间内的大幅涨跌：先搜当天与前一天的新闻和 X，再查公告、卖方、专家与同行，给出对投资逻辑的含义与五词结论。用户想知道股票为什么涨跌时使用，不要求先有覆盖工作区。"
---

# Dalton 股价异动归因（`dalton-stock-move`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

在聊天里回答“为什么涨跌”时，**第一步同样先搜当天与前一天的最新新闻（`web_search`）和 X（`x_search`）拿到“今天发生了什么”，再结合量化脚本（`scripts/move_quant.py` 几秒即可出结果，盘前/盘后加 `--session pre|post`）排查基本面、卖方与同行**，但不写工作区研究文件和知识层，结尾说明可以交给分析员在公司工作区做正式归因入库；在公司工作区里做正式归因时走完整九步流程。

## 目标与原则

股价异动时，**第一步必须先用 `web_search`（`start_date` / `end_date` 限定在当天和前一天）和 X 搜索（`x_search` 或 PTC `community(route="x-timeline", ...)`）搜最新新闻和 X（推特），先拿到“今天发生了什么”，再去查基本面（Filing / 公司公告）→ 卖方与专家（AlphaEngine / Sales Notes / TMTB / Guidepoint）→ 同行联动与价量去 Beta 量化分解（对比同组中位数、板块 ETF 与指数期货，分清是板块/宏观 beta、个股 alpha 还是两者兼有，以及放量程度）**，最后给出归因判断、对本公司 thesis/debate 的含义与事件五词结论。

- **硬性门槛**：量化脚本 `move_quant.py` 必须成功且 `data_ok=true` 才能下结论；若最新日线/盘外分钟线未出或数据缺失，立即停止归因并写明原因，严禁凭训练记忆补全。
- **全部定性线索带出处**：每条新闻、社媒讨论、filing、卖方观点、专家访谈必须带日期、链接或 `doc_id`，且核对发布时间在检索窗口内。
- **价格触发自动任务的不打扰原则（Beta No-Push Rule）**：当由价格触发自动任务（`dalton-<t>-move-up.json` / `dalton-<t>-move-down.json`）唤醒，且量化判定为 `sector_beta`、同时新闻/X/公告/卖方确认无个股层面事件时，只用 `knowledge_append_verdict` 写一条 `NO_CHANGE`（`progress="none"`），**不向 PM 推送通知**。

---

## 盘前 / 盘后 / 盘中异动的取证方法与去 Beta 方法

当用户询问“盘前为什么跌/涨”、“盘后为什么异动”或“今天盘中为什么跳水”时，在第一步搜完当天与前一天的最新新闻（`web_search`）与 X（`x_search`）后，按以下取证方法推进（作为调查方法，而非机械死板的单步路由）：

1. **量化问题所指时段的涨跌（盘外分钟线）**：
   - 运行 `move_quant.py analyze --ticker {TICKER} --session pre|post|regular`（或在工具调用中开启 `extended_hours=True` 获取含盘前盘后的 1m/5m/30m 分钟线），准确量化**问题所指时段**（如盘前 `04:00–09:30 ET` 相对前一日正股收盘、盘后 `16:00–20:00 ET` 相对当日正股收盘）的涨跌幅与成交量。
   - 同步对照**同组中位数**、**板块 ETF**（如 `QQQ`、`SMH`、`XLK`）以及**指数期货**（`NQ=F`、`ES=F`、`YM=F`、`RTY=F`）在同一时段的涨跌。若不支持盘外分钟线的市场/标的（如港股、A 股或纯指数），按脚本退回提示改用常规时段或对应期货/ETF 代理。
2. **对齐隔夜宏观与跨资产 30 分钟时间线**：
   - 拉取原油（`BZ=F`、`CL=F`）、黄金（`GC=F`）、10 年期美债收益率（`^TNX`）、美元指数（`DX-Y.NYB`）、波动率（`^VIX`）及指数期货（`NQ=F`、`ES=F`）的隔夜/盘内 30 分钟线（`interval="30min", extended_hours=True`）。
   - 将目标股票盘前/盘后/盘中拐点与上述跨资产走势按 30 分钟时间线对齐：排查是否在地缘冲突推升油价、美债收益率跳升、或纳指期货急挫的同一时刻同步异动。
3. **排查同组大公司前一晚盘后财报与公告**：
   - 盘前或开盘异动高频源于**同组龙头前一晚盘后（AMC）或当日盘前（BMO）发布的财报、指引或资本开支口径**（例如 META 盘后财报影响 GOOGL/SNAP/PINS 次日盘前）。必须核查同组核心公司前晚至当日的 8-K / 财报新闻稿与盘后股价表现。
4. **完整精读当天的收盘总结与交易台评论（TMTB / Sales Notes）**：
   - 先用 `research(route="tmtb", operation="search", query=..., start=..., end=...)` 与 `research(route="sales-notes", operation="search", ...)` 搜索当日与前一日的科技/宏观收盘总结及交易台晨报/晚报；
   - **命中后必须调用 `research(route="tmtb"|"sales-notes", operation="read", document=...)` 读取全文**，绝不能只看搜索列表的标题或片段摘要。
5. **去 Beta 与持续性检验方法（参照中国消费周报口径）**：
   - **先除各自市场基准再取同组中位数**：当同组包含跨市场标的（或需剥离大盘指数影响）时，先用每只股票收益除以各自所属市场基准（`(1 + r_stock) / (1 + r_MarketBench) - 1`，如美股对 `SPY`/`QQQ`、港股对恒指/恒科、A 股对沪深 300）得到去市场 beta 后的相对收益，再取同组中位数；
   - **检查是否单一驱动**：对比去 beta 前后的同组离散度与方向一致性，判断异动是由单一宏观/行业因子统一下杀，还是由公司个体事件驱动；
   - **持续性看 4 周与 13 周**：除当前异动窗口外，同步拉取 `4w`（约 1 个月）与 `13w`（约 1 个季度）的去 beta 累计超额表现，判断本次异动是短期情绪冲击/均值回归，还是 4 周与 13 周持续强弱趋势的延续。

---

## 九步执行流程

### 第 1 步：先搜最新新闻与 X（推特），拿到“今天发生了什么”

有异动时，**第一步必须立即搜索当天（`TODAY`）与前一天（`YESTERDAY = TODAY - 1d`）的最新新闻和 X（推特）**，先搞清楚“今天发生了什么”，再去查基本面、卖方和同行：

1. **最新新闻检索（`web_search`，`start_date` / `end_date` 限定在当天和前一天）**：
   调用直连工具 `web_search`（定义见 `src/dalton/web_search/provider.py:546-553` 与 `src/tools/web/providers/tavily.py:445-452`）：
   ```python
   web_search(
       query=f"{TICKER} {COMPANY_NAME} stock move news",
       start_date=YESTERDAY,
       end_date=TODAY,
       topic="news",
   )
   ```
   - `start_date` 传前一天 `YYYY-MM-DD`（`TODAY - 1d`），`end_date` 传当天 `YYYY-MM-DD`（`TODAY`）。
   - 逐条核对结果的实际发布日期是否在窗口内，剔除窗口外的旧闻。
   - 每条保留的新闻必须附上标题、来源媒体、发布日期、原始链接，并标明是**一手报道**（公司新闻稿、路透/彭博首发、监管原文）还是**二手转载**。

2. **X（推特）最新讨论检索（`x_search` 及 PTC `x-timeline`，限定在当天和前一天）**：
   调用直连工具 `x_search`（定义见 `src/dalton/web_search/x_search.py:615-621`）：
   ```python
   x_search(
       query=f"{TICKER} OR {COMPANY_NAME}",
       from_date=YESTERDAY,
       to_date=TODAY,
       handles=HANDLES,
   )
   ```
   或在 PTC 沙箱中调用 `researchdata.community` 的 `x-timeline` 路由（定义见 `scripts/dalton/researchdata_bridge.py:2181-2216`）：
   ```python
   community(
       route="x-timeline",
       operation="search",
       query=f"{TICKER} {COMPANY_NAME}",
       since=YESTERDAY,
       until=TODAY,
       count=20,
   )
   ```
   - 只保留有实质内容的帖子（管理层/官方账号表态、具体业务数据拆解、行业上下游交叉验证、知名机构/基本面投资人分析、突发事件一线反应），过滤纯情绪宣泄与 meme。
   - `x_search` 的综合摘要可能编造事实（例：把未发布的财报说成已发布），只能当线索；结论只引用有链接的具体帖子，并用公告、新闻或研究库核实。
   - 附上作者 handle、帖子链接（及发帖时间，如有），明确区分**有信息量的专业观点（informed opinion）**与**散户情绪**。
   - 若目标公司为港股、A 股或中概股，同步调用 `researchdata.community(route="xueqiu", operation="posts", ticker=CODE, pages=2)`（港股传 5 位代码如 `"00700"`、`"06181"`，A 股传 `"SH600519"` / `"SZ300750"`；如需全文或评论可追加 `operation="post"` / `"comments"`）拉取异动日前后的讨论流，区分中文 KOL 深度拆解与散户情绪，且只引用带原帖链接（`url`）和发帖时间（`created_at`）的帖子。

3. **大盘普跌/普涨防重**：若当天由价格触发唤醒，先读本工作区 `verdicts.md`（及必要时查兄弟工作区当日结论）看今天是否已有同类宏观/板块归因；若有则直接引用宏观背景，聚焦排查本公司有无独立个股事件。
4. **历史单日与历史区间补搜**：第 1 步固定先搜现实中的当天与前一天（`[YESTERDAY, TODAY]`）以掌握最新进展；若用户问的是历史单日（“昨天”或指定某一天 `YYYY-MM-DD`）或历史区间，在第 2 步确定新闻/X 事件窗口 `[NEWS_START, NEWS_END]`（历史单日为事件日前一天至事件日 `end - 1d` 至 `end`，历史区间为窗口起点前 1 天至终点）后，**必须再按 `[NEWS_START, NEWS_END]` 补跑一次 `web_search` 和 `x_search`**（详见第 6 步）。

### 第 2 步：解析目标、窗口与方向

在第 1 步拿到当天与前一天的即时线索后，明确后续深度排查的量化时间窗口（`--window`）、事件新闻/X 补搜窗口 `[NEWS_START, NEWS_END]`（事件日前一天到事件日，或区间起点前 1 天到终点）与扩展定性检索窗口 `[SEARCH_START, SEARCH_END]`（向前延伸 2–3 天以覆盖前一日盘后公告、周末事件或跨时区研报）：

| 用户说法 | `move_quant.py --window` | 新闻/X 窗口 `[NEWS_START, NEWS_END]`（先搜今日，历史事件再按事件窗口补搜） | 扩展定性窗口 `[SEARCH_START, SEARCH_END]`（Filing / 卖方 / 历史区间） |
|---|---|---|---|
| “今天” / “today” / 价格触发 | `1d` | `TODAY - 1d` 至 `TODAY` | 过去 2–3 天（`end - 2d` 至 `end`） |
| “昨天” / 指定单日 `YYYY-MM-DD` | `1d --end YYYY-MM-DD` | 先搜 `TODAY - 1d` 至 `TODAY`，再补搜 `NEWS_START` 至 `NEWS_END`（`end - 1d` 至 `end`） | 该日前 2 天至该日 |
| “这周” / “上周” | `1w` | 先搜 `TODAY - 1d` 至 `TODAY`，再补搜 `NEWS_START` 至 `NEWS_END`（窗口起点前 1 天至终点） | 窗口起点前 2 天至窗口终点 |
| “这个月” | `1m` | 先搜 `TODAY - 1d` 至 `TODAY`，再补搜 `NEWS_START` 至 `NEWS_END`（窗口起点前 1 天至终点） | 窗口起点前 3 天至窗口终点 |
| “最近”（未指明） | `1w` | 先搜 `TODAY - 1d` 至 `TODAY`，再补搜 `NEWS_START` 至 `NEWS_END`（窗口起点前 1 天至终点） | 过去 2 周 |
| 指定区间 `YYYY-MM-DD to YYYY-MM-DD` | `YYYY-MM-DD to YYYY-MM-DD` | 先搜 `TODAY - 1d` 至 `TODAY`，再补搜 `NEWS_START` 至 `NEWS_END`（起点前 1 天至终点） | 起点前 2 天至终点 |

### 第 3 步：基本面与 Filing / 公司公告排查

在拿到新闻与 X 的即时线索后，排查一手基本面公告与交易所披露：
- **美股本土公司**：调用 `dalton_sec.list_filings(ticker=TICKER, forms="8-K,10-Q,10-K,S-3ASR,424B2,424B5,4,SC 13D,SC 13G", limit=10, as_of=SEARCH_END)`（`forms` 为逗号分隔字符串，无 `since`/`until` 参数，用 `as_of` 指定截止日，返回后按 `filing_date` 过滤 `[SEARCH_START, SEARCH_END]` 窗口内的公告）。
- **在美上市外国私人发行人（ADR）**：`forms` 额外包含 `,6-K,20-F`。
- 若有新 filing，用 `researchdata.finance(route="sec", ...)`（如 `finance(route="sec", operation="read-item", ticker=TICKER, form="8-K", item="2.02")`、`finance(route="sec", operation="eight-k", ticker=TICKER)`）或 `get_sec_filing` 读取对应 Item / 附件正文（如 8-K EX-99.1），列出 filing 类型、提交日期、Item 号、accession 与链接。
- **港股与 A 股公司（`route="cn-hk"`）**：
  1. **法定公告检索（`operation="filings"`）**：在 PTC 中调用 `researchdata.finance(route="cn-hk", operation="filings", ticker=CODE_5_OR_6, start=SEARCH_START, end=min(SEARCH_END, TODAY), limit=20)`（`end` 不得晚于当天 `TODAY`，否则整次调用失败；港股传 5 位零填充纯数字如 `"00700"`，A 股传 6 位纯数字如 `"600519"`，不带 `.HK`/`.SS`/`.SZ` 后缀），调取巨潮资讯（cninfo）公告标题、日期、类别与 `pdf_link` 原文链接；查不到，或者需要仅英文版的公告时，再用 `web_search(query=f"site:hkexnews.hk {TICKER}", start_date=SEARCH_START, end_date=SEARCH_END)`。
  2. **场内资金流、两融、龙虎榜、回购与 AH 溢价核查（`operation="ask"`）**：每次调用只查**单一意图**（严禁把多个意图拼成一条 query）：资金流类查询必须带上具体异动日/窗口日期（例如 `researchdata.finance(route="cn-hk", operation="ask", query="宁德时代 300750 2026-10-21 主力资金净流入和成交额", limit=20)`）；两融（`query=f"{code_or_name} {event_date} 两融"`）、龙虎榜（`query=f"{code_or_name} {event_date} 龙虎榜"`）、回购（`query=f"{code_or_name} 回购"`）、AH 溢价（`query="AH溢价"`）各自单独调用一条 `ask`。必须检查返回的 `summary` 与实际来源：若东财接口失败回退至同花顺当日即时口径，成稿必须标明口径，拿不到指定历史日数据则如实写明缺口，不得把即时/最新值当作窗口内历史日终数。若涉及财务数字，平台返回表仅作二手初值线索，核心财务数字必须回法定公告原文核实；回报计算须核对前/后复权、分红、拆股与交易/报表币种。
- 若窗口内无新公告，明确写“窗口内无新 filing / 公告”。

### 第 4 步：卖方观点、专家访谈与内部研究库

通过 PTC（在沙箱代码中调用 `researchdata`）：
1. **AlphaEngine 卖方研报与点评**：
   如需按文档类别过滤，先调用 `research(route="alphaengine", operation="list_filters")` 获取合法的 `type_ids`，再传入 `params={"type_ids": [...], "sort": "relevance"}`（`alphaengine` 不支持 `document_categories` 参数）：
   ```python
   filters = research(route="alphaengine", operation="list_filters")
   research(
       route="alphaengine",
       operation="search_library",
       query=f"{COMPANY_NAME} {TICKER}",
       start=SEARCH_START,
       end=SEARCH_END,
       limit=20,
       params={"type_ids": sell_side_type_ids, "sort": "relevance"},
   )
   ```
   对解释本次异动最相关的研报调用 `research(route="alphaengine", operation="get_document", params={"doc_id": DOC_ID, "offset": 0, "max_chars": 30000})` 读取正文。
2. **卖方销售邮件与 TMTB**：
   同一时间窗口检索 `research(route="sales-notes", operation="search", query=f"{COMPANY_NAME} {TICKER}", start=SEARCH_START, end=SEARCH_END, limit=10)` 与 `research(route="tmtb", operation="search", query=f"{COMPANY_NAME} {TICKER}", start=SEARCH_START, end=SEARCH_END, limit=10)`，提取买方持仓、whisper、资金流或大行 desk 点评；**搜索命中后必须用 `research(route="tmtb"|"sales-notes", operation="read", document=...)`（如 `research(route="tmtb", operation="read", document=DOC_ID)` 或 `research(route="sales-notes", operation="read", document=DOC_ID)`）完整读取正文**，不得只凭标题或摘要下判断。
3. **Guidepoint 专家访谈与一线反馈**：
   同一时间窗口检索 `research(route="guidepoint", operation="search_library", query=f"{COMPANY_NAME} {TICKER}", start=SEARCH_START, end=SEARCH_END, limit=10)`（或用 `research(route="all", operation="search", query=f"{COMPANY_NAME} {TICKER}", ticker=TICKER, start=SEARCH_START, end=SEARCH_END, limit=10)` 一次联搜本地库、AlphaEngine 与 Guidepoint），核查供应链、渠道、客户或竞品的一线变化；引用时写访谈日期与专家身份（不写姓名），原文引用不超过 20 词，专家给出的经营数字标为“单一来源估计”。
4. **身份与状态纪律**：
   - 列出机构、日期、作者、标题和 `doc_id`，提炼评级/目标价变化与核心论点。
   - **卖方观点必须明确写成“卖方说法”（如“据 Morgan Stanley（2026-10-16 [doc_id]）观点……”），绝不当作已验证事实。**
   - 严格区分并分别写明三种异常/空状态：`Desktop 未登录`、`工具调用失败`、`窗口内无新研报`，不得将未登录或报错混写为“无新研报”。

### 第 5 步：同行联动与价量去 Beta 量化分解（必须成功）

1. **量化前先读市场状态快照（`market-regime`）**：
   - 在沙箱中调用 `macro(route="market-regime", operation="snapshot")` 读取最新市场状态（美股顶部/底部框架 `stage` 与 `summary`、`put_hedge` 提醒、动量 `momentum.reading`、A 股/港股/亚洲读数及 `as_of` 日期），把当前所处的市场阶段与情绪/拥挤度读数写进“大盘/板块 beta”的判断依据；若返回 `stale=true`，必须在归因中显式注明数据滞后及 `as_of` / `warnings` 说明。
2. **运行同组与板块量化脚本**：
   按 `.agents/skills/dalton-stock-move/references/peer-groups.md` 的优先级解析同组（peer group）和板块 ETF（优先读 `../Home/dalton/coverage.yaml`），运行统一量化脚本：

```bash
python3 .agents/skills/dalton-stock-move/scripts/move_quant.py analyze \
  --ticker {TICKER} \
  --window "{WINDOW}" \
  --session {SESSION} \
  --coverage ../Home/dalton/coverage.yaml \
  --json
```

（`--session` 默认为 `regular`；盘前异动传 `--session pre`，盘后异动传 `--session post`。若用户指定了对比同组、ETF 或指数期货，可追加 `--peers A,B,C`、`--etf XLK`、`--futures NQ=F,ES=F`。）

- 检查返回的 JSON：
  - `ok` 必须为 `true`，且 `data_ok` 必须为 `true`。若最新一根日线尚未生成导致 `data_ok: false`，**立即停止归因**，向用户或日志说明“最新交易日行情尚未就绪（`data_ok=false`），暂不下归因结论”。
  - 记录核心量化指标：未复权收盘价 `close`、复权计算的区间涨跌幅 `change_pct`、逐日明细 `daily[]`、当日成交量对前 20 日均量倍数 `volume_vs_adtv`、同组各成员涨跌 `peers[]`、同组中位数 `peer_median`、板块 ETF 涨跌 `etf_change`、超额收益 `alpha_vs_peers` 与 `alpha_vs_sector`、60 日日超额标准差 `sigma_60d_pct`、单日 `day_z`、窗口 `window_z`，以及分类判定 `assessment`（`sector_beta` / `stock_specific` / `mixed`）。
3. **排查同组同行财报与公告联动**：按上文“盘前 / 盘后 / 盘中异动的取证方法与去 Beta 方法”第 3 点核查同组核心公司前晚至当日的财报/8-K 与股价联动。

### 第 6 步：跨资产时间线、去 Beta 持续性检验与历史窗口补检

1. **跨资产时间线与去 Beta 持续性检验**：当需要进一步拆解盘内拐点或多周持续性时，按上文“盘前 / 盘后 / 盘中异动的取证方法与去 Beta 方法”第 1、2、5 点完成盘外分钟线对比、隔夜宏观 30 分钟时间线对齐及去市场基准后的 `4w`、`13w` 持续性检验。
2. **历史单日与多日窗口的新闻/X 补检**：若分析对象为历史单日（“昨天”或指定某一天 `YYYY-MM-DD`，事件窗口 `NEWS_START = end - 1d` 至 `NEWS_END = end`）或多日区间（如 `1w`、`1m`，事件窗口 `NEWS_START` 至 `NEWS_END` 为区间起点前 1 天至终点），在第 1 步 `[YESTERDAY, TODAY]` 之外，必须用 `web_search(query=f"{TICKER} {COMPANY_NAME} news", start_date=NEWS_START, end_date=NEWS_END, topic="news")` 与 `x_search(query=f"{TICKER} OR {COMPANY_NAME}", from_date=NEWS_START, to_date=NEWS_END, handles=HANDLES)` 按事件窗口补搜新闻与 X。

### 第 7 步：并行组织原则

- 定性检索互相独立，但**顺序上必须先跑第 1 步的最新新闻（`web_search`）和 X（`x_search`）拿到“今天发生了什么”**，再展开基本面公告、卖方与同行排查：
  - 单只股票异动归因通常由主智能体直接调用工具完成，上下文最紧凑、开销最低；
  - 若涉及复杂事件或多只股票深度下钻，可派 2–4 个 `dalton-evidence` 子智能体并行取证（各自写 `research/<D>-stock-move/` 下的取证分片文件，主智能体汇总）。

### 第 8 步：综合归因与对 Thesis 的含义

1. 按 `.agents/skills/dalton-knowledge/SKILL.md` 读序读取本工作区的 `thesis.md`、`debate.md`（及按需 `drivers.md`、`verdicts.md`）。
2. 综合第 1–6 步证据，回答：
   - **主要驱动**：个股事件 / 板块共振 / 宏观与利率 / 资金面与仓位挤压 / 原因暂缺，并给出催化剂主次排序；
   - **对 thesis 的含义**：这次异动背后的事实是否触及某个核心支柱（确认/警告/证伪阈值）或某条关键 debate？当前市场定价是对还是错（过度反应 / 反应不足 / 合理反映了基本面变化）？
   - **事件结论五词**：从 `NO_CHANGE`（无变化）、`THESIS_STRENGTHENED`（强化）、`THESIS_WEAKENED`（削弱）、`THESIS_BROKEN`（证伪）、`NEW_THESIS`（新论点）中选定一个，并用一句话写清理由。
   - **下一项验证**：接下来观察什么指标或事件、什么时间见分晓。
3. **说“证据不足 / 原因暂缺”之前的必查清单校验**：
   - 在给出“证据不足”或“原因暂缺”之前，**必须先显式列出还没查或未取到的相关来源**（逐项对照：①当天与前一天的最新新闻 `web_search` 与 X 社媒讨论 `x_search`，分析历史单日或多日区间时含按 `NEWS_START`–`NEWS_END` 补搜新闻和 X；②公司基本面公告与 Filing；③当日 TMTB、Sales Notes 全文 `operation="read"` 与卖方/专家；④同组大公司前一晚盘后财报/8-K 与目标时段盘外分钟线及同组/ETF/期货 `NQ=F`、`ES=F` 对比；⑤隔夜宏观 `BZ=F`/`CL=F`/`^TNX`/`DX-Y.NYB`/`^VIX` 30 分钟时间线）。只要有任一相关来源尚未核查，先补查该来源，禁止在未查完前直接下“证据不足”结论。
4. 按 `.agents/skills/dalton-stock-move/templates/stock-move.md` 写入 `research/<异动日 YYYY-MM-DD>-stock-move.md`，并在对话中输出相同结构（若为价格触发的静默 beta 轮次，见第 9 步）。

### 第 9 步：收尾三步与推送规则

0. **收尾前自检**：若拟定结论包含“证据不足 / 原因暂缺”，先列出所有未核查的相关来源并补齐取证；确认全部渠道均已核查后，在输出中附上已核查来源清单。
1. **写入事件结论**：调用 `knowledge_append_verdict(verdict=VERDICT, agenda_ops=AGENDA_OPS)`：
   - `verdict.verdict`：五词正式枚举（如 `"NO_CHANGE"`、`"THESIS_STRENGTHENED"` 等）；
   - `verdict.trigger.kind`：`"price_move"`（若由价格触发唤醒，`trigger.ref` 填入触发任务名或信号 id）；
   - `verdict.next_validation`：必填 `what` 与 `due`；
   - `verdict.progress`：有新实质证据或进展填 `"some"`（若彻底解答待验证问题填 `"resolved"`），纯板块 beta 无新事件填 `"none"`。
   - `agenda_ops`：把下一项验证写入议程（或更新已有验证项）。
2. **更新知识层**：
   - 若某条 debate 的倾向或证据因本次事件发生证实/证伪，或某个驱动变量有新读数，调用 `knowledge_update(doc="debate", ops=OPS, reason=REASON)`（`doc` 可为 `"debate"` | `"drivers"` | `"thesis"`）更新。
3. **价格触发推送纪律**：
   - **若判定为 `sector_beta` 且第 1–6 步确认无个股基本面事件**：只执行 `knowledge_append_verdict`（记 `NO_CHANGE`、`progress="none"`），**不向 PM 推送消息**。
   - **若为 `stock_specific`、`mixed`，或虽为板块波动但伴随本公司特定事件/影响 thesis**：推送归因摘要给 PM。

### 收尾门禁：汇报完成前必须过完成契约（`dalton_check_done`）

- 向 PM 汇报“完成”（或静默 beta 轮次记 `NO_CHANGE` 收尾）前，必须调用 `dalton_check_done(workflow="dalton-stock-move", params={"ticker": ..., "as_of": "<异动日 YYYY-MM-DD>"})`（`as_of` 传本次异动发生的那一天，与归因笔记 `research/<异动日 YYYY-MM-DD>-stock-move.md` 的日期一致）。
- 契约要求：`research/<异动日>-stock-move.md` 归因笔记、本轮 `trigger.kind="price_move"` 的 verdict；verdict 不是 `NO_CHANGE` 时，`thesis`/`debate`/`drivers`/`management` 至少一份须有本轮 `knowledge_update` 写入的新版本。
- `done=false` 时按 `missing` 先补齐再查（归因笔记缺失就补写、verdict 未写就 `knowledge_append_verdict`、非 `NO_CHANGE` 却没有知识更新就对受影响的文档 `knowledge_update`）；补不了的缺项只能汇报“未完成”并列出缺项与原因，不得汇报“完成”。

---

## 附：开启与管理 Core 价格触发及跨工作区自动任务

- **开启标准价格触发与跟踪套件**：研究总监从 Home 为覆盖公司启用价格异动触发（`dalton-<t>-move-up` / `dalton-<t>-move-down`）及持续跟踪任务时，直接调用 `dalton_coverage_tracking(action="enable", workspace_id=..., ticker=...)`。
- **研究总监从 Home 跨工作区调度自定义自动任务**：调用 `dalton_schedule_automation(action="upsert", workspace_id=..., name=..., instruction=..., trigger_type=..., ...)`（严禁从沙箱直接调需要用户登录态的后端 REST 接口，也不跨工作区直接写文件）。
- **分析师在本公司工作区内生成或修改自动任务**：可直接运行 `make_price_triggers.py` 或写 `.agents/user/automations/`（常设授权，写入后检查工具返回不含 `NOT SAVED`）：

```bash
python3 .agents/skills/dalton-stock-move/scripts/make_price_triggers.py \
  --coverage ../Home/dalton/coverage.yaml \
  --tier Core \
  --out-dir .agents/user/automations
```
