---
name: dalton-stock-move
description: "解释某只股票一段时间内的大幅涨跌：先区分板块/大盘带动还是公司自身原因，再按公告、新闻、卖方、专家与社媒找证据，给出对投资逻辑的含义与五词结论。用户想知道股票为什么涨跌时使用；有现成数据时几分钟内可完成，不要求先有该公司的覆盖工作区。"
---

# Dalton 股价异动归因（`dalton-stock-move`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读（`agent.md` 托管块 → `thesis.md` → `debate.md` → 按需 `drivers.md`、`management.md`、`verdicts.md`、`agenda.md`）；收尾按 `.agents/skills/dalton-agenda/SKILL.md` 的三步。

在聊天里回答“为什么涨跌”时，**同样先跑量化脚本（`scripts/move_quant.py` 几秒即可出结果，盘前/盘后加 `--session pre|post`）**，并按本流程的证据来源与方法取证，但不写工作区研究文件和知识层，结尾说明可以交给分析员在公司工作区做正式归因入库；在公司工作区里做正式归因时走完整九步流程。

## 目标与原则

股价异动时，**先做量化分解（对比同组中位数、板块 ETF 与指数期货，分清是板块/宏观 beta、个股 alpha 还是两者兼有，以及放量程度），再按 filing/同行财报 → 隔夜宏观与跨资产时间线 → 收盘总结（TMTB/交易台）与卖方/专家 → 新闻 → 社媒的取证方法找原因**，最后给出归因判断、对本公司 thesis/debate 的含义与事件五词结论（遵循“价格变动先分解再当证据”原则）。

- **硬性门槛**：第 1、2 步量化脚本必须成功且 `data_ok=true` 才能下结论；若最新日线/盘外分钟线未出或数据缺失，立即停止归因并写明原因，严禁凭训练记忆补全。
- **全部定性线索带出处**：第 3–6 步每条 filing、新闻、卖方观点、专家访谈、社媒讨论必须带日期、链接或 `doc_id`，且核对发布时间在检索窗口内。
- **价格触发自动任务的不打扰原则（Beta No-Push Rule）**：当由价格触发自动任务（`dalton-<t>-move-up.json` / `dalton-<t>-move-down.json`）唤醒，且量化判定为 `sector_beta`、同时第 3–6 步确认无个股层面事件时，只用 `knowledge_append_verdict` 写一条 `NO_CHANGE`（`progress="none"`），**不向 PM 推送通知**。

---

## 盘前 / 盘后 / 盘中异动的取证方法与去 Beta 方法

当用户询问“盘前为什么跌/涨”、“盘后为什么异动”或“今天盘中为什么跳水”时，按以下取证方法推进（作为调查方法，而非机械死板的单步路由）：

1. **先量化问题所指时段的涨跌（盘外分钟线）**：
   - 运行 `move_quant.py analyze --ticker {TICKER} --session pre|post|regular`（或在工具调用中开启 `extended_hours=True` 获取含盘前盘后的 1m/5m/30m 分钟线），准确量化**问题所指时段**（如盘前 `04:00–09:30 ET` 相对前一日正股收盘、盘后 `16:00–20:00 ET` 相对当日正股收盘）的涨跌幅与成交量。
   - 同步对照**同组中位数**、**板块 ETF**（如 `QQQ`、`SMH`、`XLK`）以及**指数期货**（`NQ=F`、`ES=F`、`YM=F`、`RTY=F`）在同一时段的涨跌。若不支持盘外分钟线的市场/标的（如港股、A 股或纯指数），按脚本退回提示改用常规时段或对应期货/ETF 代理。
2. **对齐隔夜宏观与跨资产 30 分钟时间线**：
   - 拉取原油（`BZ=F`、`CL=F`）、黄金（`GC=F`）、10 年期美债收益率（`^TNX`）、美元指数（`DX-Y.NYB`）、波动率（`^VIX`）及指数期货（`NQ=F`、`ES=F`）的隔夜/盘内 30 分钟线（`interval="30min", extended_hours=True`）。
   - 将目标股票盘前/盘后/盘中拐点与上述跨资产走势按 30 分钟时间线对齐：排查是否在地缘冲突推升油价、美债收益率跳升、或纳指期货急挫的同一时刻同步异动。
3. **排查同组大公司前一晚盘后财报与公告**：
   - 盘前或开盘异动高频源于**同组龙头前一晚盘后（AMC）或当日盘前（BMO）发布的财报、指引或资本开支口径**（例如 META 盘后财报影响 GOOGL/SNAP/PINS 次日盘前）。必须核查同组核心公司前晚至当日的 8-K / 财报新闻稿与盘后股价表现。
4. **完整精读当天的收盘总结与交易台评论（TMTB / Sales Notes）**：
   - 先用 `research(route="tmtb", operation="search_library", query=..., start=..., end=...)` 与 `research(route="sales-notes", operation="search_library", ...)` 搜索当日与前一日的科技/宏观收盘总结及交易台晨报/晚报；
   - **命中后必须调用 `research(route="tmtb"|"sales-notes", operation="read", document=...)`（或 `get_document`）读取全文**，绝不能只看搜索列表的标题或片段摘要。
5. **去 Beta 与持续性检验方法（参照中国消费周报口径）**：
   - **先除各自市场基准再取同组中位数**：当同组包含跨市场标的（或需剥离大盘指数影响）时，先用每只股票收益除以各自所属市场基准（`(1 + r_stock) / (1 + r_MarketBench) - 1`，如美股对 `SPY`/`QQQ`、港股对恒指/恒科、A 股对沪深 300）得到去市场 beta 后的相对收益，再取同组中位数；
   - **检查是否单一驱动**：对比去 beta 前后的同组离散度与方向一致性，判断异动是由单一宏观/行业因子统一下杀，还是由公司个体事件驱动；
   - **持续性看 4 周与 13 周**：除当前异动窗口外，同步拉取 `4w`（约 1 个月）与 `13w`（约 1 个季度）的去 beta 累计超额表现，判断本次异动是短期情绪冲击/均值回归，还是 4 周与 13 周持续强弱趋势的延续。

---

## 九步执行流程

### 第 1 步：解析目标、窗口与方向

从用户输入或价格触发指令中解析：
1. **目标代码（Ticker）**：如 `META`、`AMZN`。
2. **量化时间窗口（`--window`）与定性检索窗口**：
   - **定性检索窗口永远比量化窗口向前多延伸 2–3 天**（以覆盖前一日盘后公告、周末事件或跨时区研报）。

| 用户说法 | `move_quant.py --window` | 定性检索窗口（Filing / 新闻 / 卖方 / 社媒） |
|---|---|---|
| “今天” / “today” | `1d` | 过去 2–3 天（`end - 2d` 至 `end`） |
| “昨天” / 指定单日 `YYYY-MM-DD` | `1d --end YYYY-MM-DD` | 该日前 2 天至该日 |
| “这周” / “上周” | `1w` | 窗口起点前 2 天至窗口终点 |
| “这个月” | `1m` | 窗口起点前 3 天至窗口终点 |
| “最近”（未指明） | `1w` | 过去 2 周 |
| 指定区间 `YYYY-MM-DD to YYYY-MM-DD` | `YYYY-MM-DD to YYYY-MM-DD` | 起点前 2 天至终点 |

3. **大盘普跌/普涨防重**：若当天由价格触发唤醒，先读本工作区 `verdicts.md`（及必要时查兄弟工作区当日结论）看今天是否已有同类宏观/板块归因；若有则直接引用宏观背景，聚焦排查本公司有无独立个股事件。

### 第 2 步：价量与同组对比量化（第 1、2 步合一，必须成功）

1. **量化前先读市场状态快照（`market-regime`）**：
   - 在运行量化脚本前，先在沙箱中调用 `macro(route="market-regime", operation="snapshot")` 读取最新市场状态（美股顶部/底部框架 `stage` 与 `summary`、`put_hedge` 提醒、动量 `momentum.reading`、A 股/港股/亚洲读数及 `as_of` 日期），把当前所处的市场阶段与情绪/拥挤度读数写进“大盘/板块 beta”的判断依据；若返回 `stale=true`，必须在归因中显式注明数据滞后及 `as_of` / `warnings` 说明。
2. 按 `.agents/skills/dalton-stock-move/references/peer-groups.md` 的优先级解析同组（peer group）和板块 ETF（优先读 `../Home/dalton/coverage.yaml`），运行统一量化脚本：

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

### 第 3 步：Filing / 公司公告排查

在定性检索窗口（量化起点前推 2–3 天至终点）内排查一手公告与交易所披露：
- **美股本土公司**：调用 `dalton_sec.list_filings(ticker=TICKER, forms=["8-K", "10-Q", "10-K", "S-3ASR", "424B2", "424B5", "Form 4", "SC 13D", "SC 13G"], since=SEARCH_START, until=SEARCH_END, limit=10)`。
- **在美上市外国私人发行人（ADR）**：额外包含 `6-K`、`20-F`。
- 若有新 filing，用 `researchdata.finance(route="sec", ...)` 或 `get_sec_filing` 读取对应 Item / 附件正文（如 8-K EX-99.1），列出 filing 类型、提交日期、Item 号、accession 与链接。
- **港股与 A 股公司（`route="cn-hk"`）**：
  1. **法定公告检索（`operation="filings"`）**：在 PTC 中调用 `researchdata.finance(route="cn-hk", operation="filings", ticker=CODE_5_OR_6, start=SEARCH_START, end=min(SEARCH_END, TODAY), limit=20)`（`end` 不得晚于当天 `TODAY`，否则整次调用失败；港股传 5 位零填充纯数字如 `"00700"`，A 股传 6 位纯数字如 `"600519"`，不带 `.HK`/`.SS`/`.SZ` 后缀），调取巨潮资讯（cninfo）公告标题、日期、类别与 `pdf_link` 原文链接；若港股披露易（HKEXnews）公告（如业绩公告、内幕消息、翌日披露报表、股权变动）未被 `cn-hk` 的 `filings` 覆盖，必须使用 `web_search`（限定 `site:hkexnews.hk` 及检索窗口）查披露易并核对官方原文链接。
  2. **场内资金流、两融、龙虎榜、回购与 AH 溢价核查（`operation="ask"`）**：每次调用只查**单一意图**（严禁把多个意图拼成一条 query）：资金流类查询必须带上具体异动日/窗口日期（例如 `researchdata.finance(route="cn-hk", operation="ask", query="宁德时代 300750 2026-10-21 主力资金净流入和成交额", limit=20)`）；两融（`query=f"{code_or_name} {event_date} 两融"`）、龙虎榜（`query=f"{code_or_name} {event_date} 龙虎榜"`）、回购（`query=f"{code_or_name} 回购"`）、AH 溢价（`query="AH溢价"`）各自单独调用一条 `ask`。必须检查返回的 `summary` 与实际来源：若东财接口失败回退至同花顺当日即时口径，成稿必须标明口径，拿不到指定历史日数据则如实写明缺口，不得把即时/最新值当作窗口内历史日终数。若涉及财务数字，平台返回表仅作二手初值线索，核心财务数字必须回法定公告原文核实；回报计算须核对前/后复权、分红、拆股与交易/报表币种。
- 若窗口内无新公告，明确写“窗口内无新 filing / 公告”。

### 第 4 步：新闻检索

使用 `web_search` 带显式日期范围（`date_after=SEARCH_START`、`date_before=SEARCH_END`）检索公司与行业催化剂：
- 逐条核对结果的实际发布日期是否在定性窗口内，剔除窗口外的旧闻。
- 每条保留的新闻必须附上标题、来源媒体、发布日期、原始链接，并标明是**一手报道**（公司新闻稿、路透/彭博首发、监管原文）还是**二手转载**。

### 第 5 步：卖方观点、专家访谈与内部研究库

通过 PTC（在沙箱代码中调用 `researchdata`）：
1. **AlphaEngine 卖方研报与点评**：
   ```python
   research(
       route="alphaengine",
       operation="search_library",
       query=f"{company_name} {ticker}",
       start=search_start,
       end=search_end,
       limit=20,
       params={"document_categories": ["sell_side_report", "sell_side_comment"], "sort": "relevance"},
   )
   ```
   对解释本次异动最相关的研报调用 `get_document(params={"doc_id": ...})` 读取正文。
2. **卖方销售邮件与 TMTB**：
   同一时间窗口检索 `research(route="sales-notes", ...)` 与 `research(route="tmtb", ...)`，提取买方持仓、whisper、资金流或大行 desk 点评；**搜索命中后必须用 `research(route="tmtb"|"sales-notes", operation="read", document=...)`（或 `get_document`）完整读取正文**，不得只凭标题或摘要下判断。
3. **Guidepoint 专家访谈与一线反馈**：
   同一时间窗口检索 `research(route="guidepoint", operation="search_library", query=f"{company_name} {ticker}", start=search_start, end=search_end, limit=10)`（或用 `route="all"` 一次联搜本地库、AlphaEngine 与 Guidepoint），核查供应链、渠道、客户或竞品的一线变化；引用时写访谈日期与专家身份（不写姓名），原文引用不超过 20 词，专家给出的经营数字标为“单一来源估计”。
4. **身份与状态纪律**：
   - 列出机构、日期、作者、标题和 `doc_id`，提炼评级/目标价变化与核心论点。
   - **卖方观点必须明确写成“卖方说法”（如“据 Morgan Stanley（2026-10-16 [doc_id]）观点……”），绝不当作已验证事实。**
   - 严格区分并分别写明三种异常/空状态：`Desktop 未登录`、`工具调用失败`、`窗口内无新研报`，不得将未登录或报错混写为“无新研报”。

### 第 6 步：社交媒体（X）

在窗口内调用 `x_search(query=..., from_date=search_start, to_date=search_end, handles=...)` 直接检索 X（Twitter）社媒讨论（若配置了 `x_api` MCP 也可配合使用）：
- 只保留有实质内容的帖子（管理层/官方账号表态、具体业务数据拆解、行业上下游交叉验证、知名机构/基本面投资人分析、突发事件一线反应）。
- 过滤纯情绪宣泄与 meme。
- `x_search` 的综合摘要可能编造事实（例：把未发布的财报说成已发布），只能当线索；结论只引用有链接的具体帖子，并用公告、新闻或研究库核实。
- 附上作者 handle、帖子链接（及发帖时间，如有），明确区分**有信息量的专业观点（informed opinion）**与**散户情绪**。若目标公司为港股、A 股或中概股，调用 `researchdata.community(route="xueqiu", operation="posts", ticker=..., pages=2)`（港股传 5 位代码如 `"00700"`、`"06181"`，A 股传 `"SH600519"` / `"SZ300750"`；如需全文或评论可追加 `operation="post"` / `"comments"`）拉取异动日前后的讨论流，区分中文 KOL 深度拆解与散户情绪，且只引用带原帖链接（`url`）和发帖时间（`created_at`）的帖子。

### 第 7 步：并行组织原则

- 第 3–6 步定性检索互相独立：
  - 单只股票异动归因通常由主智能体直接并行/顺序调用工具完成，上下文最紧凑、开销最低；
  - 若涉及复杂事件或多只股票深度下钻，可派 2–4 个 `dalton-evidence` 子智能体并行取证（各自写 `research/<D>-stock-move/` 下的取证分片文件，主智能体汇总）。

### 第 8 步：综合归因与对 Thesis 的含义

1. 按 `.agents/skills/dalton-knowledge/SKILL.md` 读序读取本工作区的 `thesis.md`、`debate.md`（及按需 `drivers.md`、`verdicts.md`）。
2. 综合第 2–6 步证据，回答：
   - **主要驱动**：个股事件 / 板块共振 / 宏观与利率 / 资金面与仓位挤压 / 原因暂缺，并给出催化剂主次排序；
   - **对 thesis 的含义**：这次异动背后的事实是否触及某个核心支柱（确认/警告/证伪阈值）或某条关键 debate？当前市场定价是对还是错（过度反应 / 反应不足 / 合理反映了基本面变化）？
   - **事件结论五词**：从 `NO_CHANGE`（无变化）、`THESIS_STRENGTHENED`（强化）、`THESIS_WEAKENED`（削弱）、`THESIS_BROKEN`（证伪）、`NEW_THESIS`（新论点）中选定一个，并用一句话写清理由。
   - **下一项验证**：接下来观察什么指标或事件、什么时间见分晓。
3. **说“证据不足 / 原因暂缺”之前的必查清单校验**：
   - 在给出“证据不足”或“原因暂缺”之前，**必须先显式列出还没查或未取到的相关来源**（逐项对照：①目标时段盘外分钟线及同组/ETF/期货 `NQ=F`、`ES=F` 对比；②隔夜宏观 `BZ=F`/`CL=F`/`^TNX`/`DX-Y.NYB`/`^VIX` 30 分钟时间线；③同组大公司前一晚盘后财报/8-K；④当日 TMTB 与 Sales Notes 全文 `operation="read"`；⑤公司公告与新闻；⑥X 社媒讨论）。只要有任一相关来源尚未核查，先补查该来源，禁止在未查完前直接下“证据不足”结论。
4. 按 `.agents/skills/dalton-stock-move/templates/stock-move.md` 写入 `research/<YYYY-MM-DD>-stock-move.md`，并在对话中输出相同结构（若为价格触发的静默 beta 轮次，见第 9 步）。

### 第 9 步：收尾三步与推送规则

0. **收尾前自检**：若拟定结论包含“证据不足 / 原因暂缺”，先列出所有未核查的相关来源并补齐取证；确认全部渠道均已核查后，在输出中附上已核查来源清单。
1. **写入事件结论**：调用 `knowledge_append_verdict`：
   - `verdict.verdict`：五词正式枚举（如 `"NO_CHANGE"`、`"THESIS_STRENGTHENED"` 等）；
   - `verdict.trigger.kind`：`"price_move"`（若由价格触发唤醒，`trigger.ref` 填入触发任务名或信号 id）；
   - `verdict.next_validation`：必填 `what` 与 `due`；
   - `verdict.progress`：有新实质证据填 `"material"` 或 `"incremental"`，纯板块 beta 无新事件填 `"none"`。
   - `agenda_ops`：把下一项验证写入议程（或更新已有验证项）。
2. **更新知识层**：
   - 若某条 debate 的倾向或证据因本次事件发生证实/证伪，或某个驱动变量有新读数，调用 `knowledge_update(doc="debate" | "drivers" | "thesis", ...)` 更新。
3. **价格触发推送纪律**：
   - **若判定为 `sector_beta` 且第 3–6 步确认无个股基本面事件**：只执行 `knowledge_append_verdict`（记 `NO_CHANGE`、`progress="none"`），**不向 PM 推送消息**。
   - **若为 `stock_specific`、`mixed`，或虽为板块波动但伴随本公司特定事件/影响 thesis**：推送归因摘要给 PM。

---

## 附：批量生成 Core 价格触发自动任务

上线或更新覆盖名单时，可用 `make_price_triggers.py` 为全部 Core 标的生成涨跌两条价格触发文件（`dalton-<t>-move-up.json` 与 `dalton-<t>-move-down.json`；包含港股/A 股标的时加 `--include-non-us`）：

```bash
python3 .agents/skills/dalton-stock-move/scripts/make_price_triggers.py \
  --coverage ../Home/dalton/coverage.yaml \
  --tier Core \
  --out-dir .agents/user/automations
```

- 分析师在本工作区直接写进 `.agents/user/automations/`（常设授权，写入后检查工具返回不含 `NOT SAVED`）；
- 研究总监从 Home 批量上线时，读取生成好的 JSON 并调用后端 REST helper `PUT /api/v1/dalton/automations/{name}`，不跨工作区直接写文件。
