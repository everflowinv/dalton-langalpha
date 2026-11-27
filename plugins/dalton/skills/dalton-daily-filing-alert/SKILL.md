---
name: dalton-daily-filing-alert
description: "无人值守的覆盖池公告日报：扫描美股 SEC 与中港公告，分层摘要，并用 automation_report 提交卡片。"
---

# 覆盖池公告日报

本技能无人值守完成每日覆盖池公告扫描、明细解析、分层摘要与卡片提交，全过程只交付这一张卡片，直接执行，无需提问或创建子任务。

安装路径是 `.agents/skills/dalton-daily-filing-alert/`。下面的命令用这个目录里的 `scripts/daily_filing_alert.py`。每条命令都带 `--workspace-dir <workspace>`。`<workspace>` 用本轮工作区根目录，不靠向上查找说明文件来猜。

接收方在任务的 `metadata.report_profile` 里，配置名是 `daily-filing-alert`。卡片和工具参数都不写接收方。

覆盖池随技能走：`watchlist.txt`（美股）、`watchlist_cn_hk.txt`（A 股与港股）。本轮保持这两份名单不变，也不运行导入脚本。

状态保存在 `state/daily-filing-alert/`：`last_check.json`、`cik_map.json`（SEC 即美国证券交易委员会的公司唯一编号 CIK 映射缓存）、`enrich_cache.json`、`filings.json`。`last_check.json` 在 SEC `scan`（美股扫描）成功返回时按代码写入，确认发送之后保持不变。整份 SEC 代码表拉不到且没有缓存时，`scan` 返回 `ok`（执行成功标志）为 false，也不写 `last_check.json`；若个别代码对应多个 CIK（映射不唯一），脚本跳过该代码并在其 `errors`（代码级错误列表）中记录。`scan-cn-hk`（中港公告筛选命令）不写 `last_check.json`。草稿只放在 `temp/daily-filing-alert/`。

正文先直接给判断，再写事实。自行撰写的标题、摘要、导语和最终回复采用正面、直接的陈述，避免推诿、免责或“非……而……”式否定措辞。栏目、数字、来源和失败说明保持原样。交付的卡片文本不用 emoji（表情符号）。

## 分工与依赖

- 美股扫描、正文补齐、分层、更正公告过滤、回购正文/摘要/导语生成和卡片版面由脚本完成。模型按脚本结果填写 `payload`（待渲染卡片的输入数据），保持重点区与例行区归类不变，不在 `payload` 中手写任何回购数字。
- A 股和港股公告在 `ExecuteCode`（代码执行工具）里调用 `finance`（行情与公告接口）取回，写成一份 raw JSON（原始接口汇总数据）。`scan-cn-hk` 按窗口筛选、去重，写入 `id`（公告编号）、A 股正文（`body_excerpt` 正文摘录、`digest` 要点摘要、`ndr` 翌日披露结构化解析结果）以及回购校验结果 `buyback_results`（按公告 ID 或 URL 链接为键）。
- 港股及尚未完成解析的回购/翌日披露 PDF（便携式文档）由 `scripts/buyback_parse.py`（港交所/巨潮 FF305 翌日披露报表解析脚本，FF305 为港交所翌日披露报表编号）下载校验，并将结果写回同一份扫描输出文件（`--scan-output`）；解析时先用 `pdfplumber`，缺失时回退至 `pypdf`（均为 PDF 文本提取库），均缺失或校验失败时记录 `validated: false`（未通过校验）与 `error`（错误原因）。
- 模型负责取回中港列表与 A 股正文、按规则运行扫描与 `buyback_parse.py`、撰写非回购摘要与精简 `payload`（回购条目只保留公告 `id`/`url` 与 `title` 标题、`date` 日期、`tag` 标签），再提交卡片。

## 窗口

先取香港日期和回看天数。周一 `lookback_days` 为 3，`window_note` 为「覆盖上周五至今」；其余工作日 `lookback_days` 为 1，`window_note` 为「覆盖过去 24 小时」。`report_run_id` 为 `daily-filing-alert-<YYYY-MM-DD>`。后面的 `{LOOKBACK_DAYS}`、`<report_date>`、`RUN_ID` 直接用这一步的字段，无需另算。顶层 `start`、`end` 只给美股。A 股和港股用 `cn_hk` 每一项自己的 `start`、`end`。

```bash
python3 .agents/skills/dalton-daily-filing-alert/scripts/daily_filing_alert.py \
  --json window --workspace-dir <workspace>
```

记下 `start`（开始日期）、`end`（结束日期）、`limit`（单次返回条数上限）和 `cn_hk`（中港股票查询配置列表）。`start` 与 `end` 是美股边界：报告日减去 `lookback_days` 到报告日，两端都包含。SEC `scan` 参数仍传 `--days`，无需传入这两个日期。`window` 会为 `watchlist_cn_hk.txt` 中的每只 A 股与港股生成完整 `cn_hk` 条目：`ticker`（传给 `finance` 的 5 位港股或 6 位 A 股代码）、`code`（卡片展示代码）、`name`（公司简称）、`market`（市场标识）、`start`、`end`，模型无需手工补齐。A 股在 `lookback_days` 为 1 时 `start` 和 `end` 都是报告日，只含当天；为 3 时含报告日和它前面的两天。港股 `start` 是报告日减去 `lookback_days`，`end` 是报告日的下一天。

自检可以单独跑。自检失败就按文末失败节处理，停止后续扫描。

```bash
python3 .agents/skills/dalton-daily-filing-alert/scripts/daily_filing_alert.py \
  --json self-test --workspace-dir <workspace>
```

## 1. 扫描美股

```bash
python3 .agents/skills/dalton-daily-filing-alert/scripts/daily_filing_alert.py \
  --json scan --workspace-dir <workspace> \
  --days {LOOKBACK_DAYS} \
  --output temp/daily-filing-alert/<report_date>-sec.json
```

成功时 `ok` 为 true。读 `--output` 那个文件。标准输出只是状态。命令中断时从该文件恢复，同一轮只运行一次 `scan`。

每条扫描在一次运行里只跑一次。

## 2. 取 A 股和港股公告

对 `cn_hk` 里的每一个 `ticker` 调用一次。`start` 和 `end` 用该项自己的日期，`limit` 用窗口里的 `limit`。参数名用 `ticker`、`start`、`end`、`limit`。一次失败就重试这一次；仍然失败就在 raw JSON 里把该代码标成 `ok: false`，继续其余代码。

```python
from tools.researchdata import finance

finance(
    route="cn-hk",
    operation="filings",
    ticker="300308",
    start=A_START,
    end=A_END,
    limit=50,
)
finance(
    route="cn-hk",
    operation="filings",
    ticker="00700",
    start=HK_START,
    end=HK_END,
    limit=50,
)
```

`A_START`、`A_END` 用 300308 那一项的 `start`、`end`。`HK_START`、`HK_END` 用 00700 那一项的 `start`、`end`。其余代码同样用各自在 `cn_hk` 中的条目。

一次返回的条数等于 `limit` 时，把该代码的日期区间拆成更小的几段，用同样的 `route`、`operation`、`ticker`、`limit` 再调用，只改 `start` 和 `end`。每一段都放进 `calls`，`ticker` 保持不变；那次达到 `limit` 的原始调用省略，只放拆分后的各段。脚本按标题、日期和链接合并去重。某一段的条数仍然等于 `limit`，就继续拆这一段。已经拆到单独一天并且仍然等于 `limit` 时停止，该代码会标成截断，写进确认发送后的缺口说明。严禁编造未返回的公告。

`finance` 的 `cn-hk` 只有列表，需要通过工具补取正文或解析 PDF。对 A 股公告的 `pdf_link`（公告链接）取正文；每条 A 股公告只调用一次 `WebFetch`（网页抓取工具）取回全文，即使标题里看不出翌日披露或回购也要取，正文开头可能才是翌日披露报表。

```python
WebFetch(url, prompt)
```

`url` 用该条的 `pdf_link`。`prompt` 要求返回公告正文原文，保留翌日披露报表的章节、回购股数、金额、价格区间，以及注销或持作库存股的去向。公开页面打不开时，在该条写 `enrich_error`（正文补取错误字段），内容以「取正文失败:」开头，然后继续其余公告。请求保持匿名公开访问，禁止附带 `cookies`（网站身份凭证）、`Authorization`（请求鉴权头）或登录态。

把取回的原文写进该条的 `notice_text`。脚本写入 `pdf_url`、`body_excerpt`；标题含「翌日披露」，或正文开头含「翌日披露報表」时，再写入 `ndr` 和 `digest`。解析失败时脚本写入 `enrich_error`。

把全部结果写成 `temp/daily-filing-alert/<report_date>-cn-hk-raw.json`。`rows` 用工具返回的列表；工具放在 `data` 里时照原样留下，脚本两种都认。同一 `ticker` 拆分后的各段都留在 `calls` 里（省略被拆分的原始调用）。顶层 `end` 写香港报告日。

```json
{
  "start": "YYYY-MM-DD",
  "end": "YYYY-MM-DD",
  "limit": 50,
  "calls": [
    {"ticker": "300308", "ok": true, "rows": [
      {"date": "YYYY-MM-DD", "title": "H股公告-翌日披露报表", "category": "回购", "pdf_link": "https://static.cninfo.com.cn/finalpage/YYYY-MM-DD/a.pdf", "notice_text": "翌日披露報表……"}
    ]},
    {"ticker": "00700", "ok": true, "rows": [
      {"date": "YYYY-MM-DD", "title": "翌日披露", "pdf_link": "https://www.hkexnews.hk/1.pdf"}
    ]},
    {"ticker": "300502", "ok": false, "error": "timeout"}
  ]
}
```

然后交给脚本筛选（默认读 `watchlist_cn_hk.txt`）。脚本保持离线，只处理本地 JSON。

```bash
python3 .agents/skills/dalton-daily-filing-alert/scripts/daily_filing_alert.py \
  --json scan-cn-hk --workspace-dir <workspace> \
  --days {LOOKBACK_DAYS} \
  --input temp/daily-filing-alert/<report_date>-cn-hk-raw.json \
  --output temp/daily-filing-alert/<report_date>-cn-hk.json
```

### 回购公告解析（A 股与港股）

A 股在 raw JSON 中提供 `notice_text`（公告正文文本）时，`scan-cn-hk` 会自动完成翌日披露与回购校验，并将结果写入扫描输出文件 `temp/daily-filing-alert/<report_date>-cn-hk.json` 的 `buyback_results`（按公告 `id`/`url` 为键，含 `validated` 校验通过标志、`is_buyback` 是否回购、`total_amount_raw` 原始合共金额、`total_shares_raw` 原始合共股数、`currency` 币种、`groups` 股份类别/币种分组、`is_amendment` 是否更正公告）。`scan-cn-hk` 对港股条目**强制忽略**任何 `notice_text`，港股一律交由 `buyback_parse.py` 下载真实 PDF 校验；若对同一 `--output` 重跑 `scan-cn-hk`，脚本会保留既有的 `validated: true` 扫描记录（本轮被更正过滤移除的原公告除外），避免被后续占位记录覆盖。

港股公告及尚未在 `scan-cn-hk` 完成正文解析的 PDF 链接，只要标题（或类别）含「翌日披露」「Next Day Disclosure」「股份购回」「股份購回」「购回」「購回」「回购」「回購」「买回」「買回」「repurchase」「buyback」等词，必须调用 `scripts/buyback_parse.py` 并加上 `--workspace-dir` 与 `--scan-output`，从真实 PDF 下载校验并把结果直接回写进同一份扫描输出文件（瞬时抓取失败时保留既有的 `validated: true` 扫描记录）。非回购公告（如「证券变动月报表」）跳过此脚本。

```text
python3 .agents/skills/dalton-daily-filing-alert/scripts/buyback_parse.py \
  --workspace-dir <workspace> \
  --scan-output temp/daily-filing-alert/<report_date>-cn-hk.json \
  <url1> [<url2> ...]
# 仅离线调试已提取正文时使用：
python3 .agents/skills/dalton-daily-filing-alert/scripts/buyback_parse.py --text-json temp/daily-filing-alert/<report_date>-buyback-text.json
```

脚本逐份严格校验：第一、二章节均先把所有编号标记（含仅剩编号的独立行，如 `1).`、`2).`）计入应有条数，再逐条解析，不跳过无法解析的行；第一章节要求编号与事件同处单行且数字格式完整，第二章节每笔交易要求编号与日期同行，两章节的所有数字均执行千分位分组、边界与格式字符检查（数字内部出现空白、断行或 `Unicode Cf` 统一码不可见格式字符如 `U+2060`、`U+200B`、`U+200C`、`U+200D`、`U+FEFF` 时直接判错）。同时核对逐行之和与 PDF「合共」一致、合计股数与拟注销/库存股一致；若正文「狀態：更正」（或「状态：更正」）或前三行非空文本含「更正」「修訂/修订」「補充/补充」，置 `is_amendment: true`（无论更正后是否仍含回购交易）。多股份类别或多币种时按 `groups` 分组返回、各自汇总；非回购翌日披露同样保留并核对第一章节「證券代號」（`classes[].stock_code`）；任一项检查未通过即整条置为 `validated: false` 并写 `error`，不做部分接受。模型无需从 `stdout`（标准输出）抄写任何回购数字到 `payload`。

读 `scan-cn-hk` 的 `--output`。公告字段有 `id`、`title`、`type`（公告类别）、`date`、`url`；A 股另有 `pdf_url`、`body_excerpt`、`digest`、`ndr`、`enrich_error`；`buyback_results` 保存所有回购/翌日披露的校验记录。筛选沿用各市场日期边界，锚点是香港报告日（命令 `--as-of`，未传时取当前香港时间）和 `--days`；若 raw JSON 顶层 `end` 与香港报告日不一致，脚本仍以香港报告日为准并在 `day_errors`（当日缺口与错误列表）记录缺口。A 股在 `--days` 为 1 时只留报告日当天；港股从报告日减去 `--days` 留到报告日的下一天。同一公司窗口内若有标题含「更正」「修訂/修订」「補充/补充」（A 股亦检查正文状态栏与前三行）的回购/翌日披露公告，`scan-cn-hk` 只保留更正公告，并将被过滤掉的其余回购/翌日披露公告记入该代码的 `errors` 与 `day_errors`。某代码 `ok` 为 false，或 raw JSON 里没有这个代码，结果里的 `errors` 有说明，`announcements`（公告列表）为空。`truncated`（截断标志）为 true 表示还有一段的返回条数达到 `limit`。合并后的总条数可以多于 50。`day_errors` 和各代码 `errors` 留到最终回复的缺口说明，卡片内省略。

## 3. 写 payload 并生成卡片

把 `temp/daily-filing-alert/<report_date>-payload.json` 写成一个对象：

```json
{
  "date": "YYYY-MM-DD",
  "weekday": "周X",
  "window_note": "覆盖过去 24 小时",
  "lead": "代码 + 事件 + 金额，约 40 字以内",
  "stats": {"us": {"scanned": 0}, "cn": {"scanned": 0}, "hk": {"scanned": 0}},
  "sections": [
    {"key": "us", "title": "美股 · SEC", "companies": [
      {"code": "TICKER", "name": "TICKER", "summary": "一句话",
       "items": [{"tag": "8-K", "text": "具体事项", "date": "YYYY-MM-DD", "url": "https://www.sec.gov/...", "amount_usd": 7110000}]}
    ]},
    {"key": "cnhk", "title": "A股 / 港股", "companies": [
      {"code": "300308", "name": "中际旭创", "summary": "一句话",
       "items": [{"tag": "A股公告", "text": "要点", "date": "YYYY-MM-DD", "url": "https://static.cninfo.com.cn/finalpage/YYYY-MM-DD/a.pdf"}]}
    ]}
  ],
  "footer": "数据源：SEC EDGAR · 巨潮公告 · HKEXnews"
}
```

- `date`、`weekday`、`window_note` 用窗口命令的字段。
- `stats`（统计区）只填 `scanned`（已扫描家数）：美股用 `tickers_scanned`，A 股用 `cn_stocks_scanned`，港股用 `hk_stocks_scanned`。某个市场这次没扫就省略该 key（字段）。省略 `hit`（命中家数）。
- `lead`（导语）只点今天最重要的一两件事，约 40 个汉字以内，写清代码、事件和金额：
  - 存在回购、Form 4（内部人交易申报表）买入或 A 股/港股增持时，`lead` 整句由脚本按优先级自动生成（回购优先，其次买入/增持），忽略模型提交的 `lead`：回购仅根据结构化的已校验「合共」金额，Form 4 买入与 A 股/港股增持根据结构化字段（如 `amount_usd` 美元金额、`total_amount` 结构化总额与 `currency`）；多股份类别或多币种分组时仅取金额最大的单个组并标明股份类别，跨组金额互不相加；回购措辞按交易日数生成（1 天写「单日回购」，多天写「N 个交易日回购」，如 `09992 单日回购 5,040 万港元`、`09992 2 个交易日回购 1,563 万港元`）；缺少已校验金额时只写事件（例如 `09992 回购股份`），绝不从 `text` 或标题提取金额；
  - 当日存在任何涉及翌日披露或回购的公司时（不论是否确认回购），`lead` 完全由脚本生成（忽略模型导语，无优先事件时省略 `lead`）；仅在既无优先事件、当日也无任何涉及翌日披露/回购的公司时，脚本才采用模型撰写的 `lead`（总长不超过 40 字，超出则截断）。
- `footer`（页脚）始终完全由脚本生成（`数据源：SEC EDGAR · 巨潮公告 · HKEXnews`，其中 EDGAR 为美国证券交易委员会电子数据收集与检索系统，HKEXnews 为港交所披露易），忽略模型提交的 `footer`。
- `items[].tag` 用原名：8-K（重大事项临时报告）、6-K（外国发行人临时报告）、10-K（年报）、10-Q（季报）、Form 4、权益变动（Form 4 买入）、Form 144（限售股拟减持申报表）、13D / 13G（重大持股变动报告）、回购、A股公告、港股公告。SEC 结果里的 `type` 已把 4 和 4/A 写成 Form 4。
- `items[].url` 用结果里的 `url`。
- `items[].amount_usd` 只在知道美元整数时填写。税款代扣、常规授予、人民币及港元金额都省略。人民币保留「元人民币」，港元保留「港元」，各自保留原币种，禁止换算。
- 美股 `name` 用代码本身。A 股和港股 `code` 用窗口 `cn_hk[].code`，`name` 用中文名。
- Form 4 规则（先用 `parsed` 预解析字段）：
  - **二级市场买入一律列为重点**：`parsed.tx_type`（交易类型）为 `PURCHASE`（买入）或交易 `code` 为 `P` 的条目，`tag` 设为 `「权益变动（Form 4 买入）」`，并在该 `item`（公告条目）上附 `"tx_type": "PURCHASE"`（若只有单笔交易代码也可附 `"code": "P"`），`text` 写内部人姓名与职务、股数、单价，`amount_usd` 填买入金额；公司 `summary`（摘要）首句点明谁买入、合计股数和金额；同一公司买入与卖出分开成条，卖出条目仍用 `「Form 4」` 标签并保留原始 `tx_type`（例如 `"SALE"` 卖出）。
  - `parsed.tx_type` 保留原始交易类型。`parsed.summary` 为空，或只有 `DERIVATIVE ACQUISITION`（衍生品获授）且没有金额，视为常规授予，同一家公司合成一条 `item`。`parsed.summary` 为 `GIFT`（赠与）、`DISPOSE`（处置）或其他未单独成句的原始类型时，按这个类型写，保持原始类别。金额为 $0 或股数为 0 的条目跳过输出。行权后卖出只写卖出结果和套现金额。税款代扣同样合成一条。多位内部人卖出时，`summary` 写合计股数和金额，`items` 按金额从大到小。
- 其他美股表单按 `headline`（正文标题）、`cover_summary`（封面摘要）、`item_labels`（事项标签）、`body_excerpt` 的顺序写具体事项。这些字段都空时，`text` 写「未取得正文或具体事项」。禁用 `accession`（SEC 档案编号）充当标题，严禁按表单类型编造事件。
- A 股与港股及回购规则：
  - 标题含翌日披露或回购类关键词（如「翌日披露」「Next Day Disclosure」「股份购回」「股份購回」「购回」「購回」「回购」「回購」「买回」「買回」「repurchase」「buyback」）的公告：模型在 `payload.json` 中**只需将该公告列为条目并带上 `id`（若有）、`url` 与 `title`、`date`、`tag`**（如 `{"tag": "回购", "title": "<公告标题>", "date": "YYYY-MM-DD", "url": "<pdf_link>", "id": "<ann_id>"}`），**只填以上字段，禁止填写任何回购数字、`text`、`line`、`total_amount`、`total_shares`、`groups`、`trades`、`ndr`、`buyback` 等字段**。
  - `render-card`（卡片渲染命令）会自动读取 `--cn-hk` 扫描输出文件中的 `buyback_results` 与 `results`，在入口先剥离 `payload` 中所有 `_` 开头的内部字段，对回购/翌日披露条目（含命中普通扫描公告但模型在 `tag`/`title`/`text` 携带回购特征的条目）**完全忽略模型提供的 `text`、`line`、数值字段与标题数字，并将 `tag` 限制在安全白名单内**，只按 `id`/`url` 匹配扫描记录并复核公司代码、解析出的「證券代號」（`groups[].stock_code`、`classes[].stock_code` 与顶层 `stock_code`）、ID 与链接一致性及分组与逐行金额自洽性，卡片链接与标题一律取扫描记录的正式链接与标题，日期严格校验 `YYYY-MM-DD`（不合法时回退扫描日期或留空），公司 `name` 优先取覆盖池名单与扫描记录：
    - 已校验且为回购（`validated: true`、`is_buyback: true`、身份与自洽复核均通过）：条目正文、公司 `summary` 中的回购句与 `lead` 中的回购部分全部由脚本用结构化字段生成（多组分别列出，导语取最大单组；不跨公告累加）。
    - 已校验且确认为非回购翌日披露（`validated: true`、`is_buyback: false`，如期权行权）：同样保留并核对第一章节「證券代號」（`classes[].stock_code`），一致时条目正文与摘要由脚本使用解析器生成的 `digest`，按普通公告处理（若同时为更正公告，仍会触发移除同公司原回购公告）；不一致按身份冲突处理。
    - 扫描记录缺失、身份冲突（公司代码、解析出的「證券代號」、URL 或 ID 不一致）、自洽复核失败或校验未通过（`validated: false`）：条目正文改写为固定句「<扫描标题或通用标题>（金额未能从原文核对）」（无匹配扫描记录时标题固定为通用标题 `回购相关公告`，`tag` 限制在安全白名单内且均不带模型数字），公司 `summary`（`发布回购公告，金额未能从原文核对`）与 `lead`（`<代码> 回购股份`）均不写金额，并在 `day_errors` 列出该条目（公司无条目但 `summary` 含回购金额时同样替换并写入 `day_errors`）。
  - 按扫描记录逐家公司确定更正关系：同公司扫描记录（`buyback_results` 或 `results`）里有标题含「更正」「修訂/修订」「補充/补充」、正文「狀態：更正」或 `is_amendment: true` 的公告（即使该更正公告确认为无回购的翌日披露），无论 `payload` 是否保留更正条目，`render-card` 均会移除同公司被更正的原回购公告，按剩余条目重建摘要，并将过滤掉的原公告写入返回的 `day_errors`。解析失败缺口同样写进最终回复的缺口说明。
  - **渲染输出前去数字保护**：若公司在扫描记录中有任何翌日披露或回购公告（不论是否确认回购），或 `payload` 任意条目的 `tag`/`title`/`text`（或 `summary`）含回购关键词，则该公司判定为涉及翌日披露/回购的公司，**这类公司的模型文字不会显示数字**。输出前该公司所有来自模型的显示字段（条目 `text`/`title`/`tag`/`date`、公司 `name`、`summary`）里的**任何数字字符**（阿拉伯数字、全角数字、中文数字大小写与异体「〇零一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟萬億兩貳參叄貮陸」等）一律删除，**且除显示文字外，模型提供的所有数值/金额/数量/币种字段（`amount_usd`、`total_amount`、`total_amount_raw`、`shares`、`price`、`currency` 等）一律清除**；导语、合计、排序只用代码生成的已校验数据；仅保留脚本根据已校验扫描结果生成的字段与句子中的数字，条目日期用扫描记录日期（无扫描日期时留空），公司名用名单；`lead` 与 `footer` 按上文规则由脚本生成。
  - 公告类型/标签为「增持」（非回购类型）时，`item[].tag` 设为 `「增持」`（或 `A股公告` / `港股公告`），即使条目带 `total_amount` 也不按回购处理；`summary` 首句点明增持；减持类公告保持减持描述与 `「权益变动」` / 原公告标签。没有扫描记录的普通条目只要正文或标题出现回购关键词（不用「未回购」等否定短语整体排除），即按未核对回购处理。
  - 其余非回购 A 股 `text` 优先用 `digest`，没有则用 `body_excerpt`，再没有才用标题；其余港股按 `title` 和 `type` 写一句 `text`，数字和日期以标题为准，省略标题未列出的金额。`summary` 只写正文或标题里已经出现的增持、业绩或股份变动。正文里的数字、股票代码与期间直接写成普通文本，格式见 dalton-conventions 技能的 `references/writing.md` §5 第 4 条。
- 卡片上省略警告。抓取超时、接口报错、返回条数达到 `limit`、CIK 不唯一或回购 PDF 解析失败都留到最终回复的缺口说明。
- 窗口内没有新动态时，两个 section 的 `companies` 用空数组，`stats` 照常填。仍要提交这张灰色卡片，`material`（实质内容标记，保持为 yes 以确认卡片有效提交）用 `yes`。

分层、条目/摘要重建、更正过滤和 `lead` 生成由脚本（`render-card`）按上文规则执行：实质公告、Form 4 买入条目（`tx_type: "PURCHASE"` / `code: "P"` / `「权益变动（Form 4 买入）」`）、A 股/港股增持条目、已确认存在回购交易的回购公司及未核对的翌日披露/回购条目均进重点区；扫描解析确认无回购的翌日披露按普通公告分类与排版（仍受涉及翌日披露公司的去数字保护）；Form 4 已明确 `tx_type` / `code`（例如 `DERIVATIVE ACQUISITION`、`A`）时严格按交易类型分类，仅 A 股/港股公告才依据正文「增持」判断；只有内部人卖出/常规授予且合计低于 1000 万美元的公司进例行区。回购公告不参与公司标题跨公告金额累计，其他类型（如 Form 4）保持现状。

```bash
python3 .agents/skills/dalton-daily-filing-alert/scripts/daily_filing_alert.py \
  --json render-card --workspace-dir <workspace> \
  --payload temp/daily-filing-alert/<report_date>-payload.json \
  --cn-hk temp/daily-filing-alert/<report_date>-cn-hk.json \
  --output temp/daily-filing-alert/<report_date>-card.json
```

`ok` 为 true 后，读 `card.json` 的对象作为 `CARD`。同一天重试沿用同一个 `RUN_ID` 和同一份卡片字节。`card_conflict`（卡片内容冲突）表示磁盘上的卡片和这次渲染不同，保留原卡片文件，不再修改 `payload` 重新渲染。

## 4. 提交

调用 `automation_report`，只通过工具直调，无需从 Python 导入或用 Bash 调用。`config` 以及用户、工作区、线程、运行和自动化身份由服务器注入。`cards` 只含这一张卡片。

```python
automation_report(action="submit", report_run_id=RUN_ID, material="yes", cards=[CARD])
automation_report(action="status", report_run_id=RUN_ID)
```

只有返回里的 `report_run_id` 与 `RUN_ID` 一致，`content_kind` 为 `cards`，并且 `ok` 为 true、`delivery_confirmed` 为 true、`status` 为 `sent`，才算发送成功。

未确认时，保留原卡片，沿用同一个 `RUN_ID`（`report_run_id`）和同一份 `cards` 再提交一次。`content_conflict`（正文冲突）表示这个 `report_run_id` 已经保存了不同正文，保持卡片原样。`code` 为 `not_automation_run`（非自动化触发运行）时停止：最终回复说明本次为手动或非自动化触发运行，省略卡片正文。

确认发送之后，有抓取或解析缺口就只写一句话，写上有新动态的公司家数，并补上缺口说明（例如「已提交卡片：9 家公司有新动态；300502 公告未取回」）。SEC 结果里任一公司的 `errors` 非空（含映射不唯一被跳过的代码），或 `scan-cn-hk` / `render-card` 结果里的 `day_errors` / 各代码 `errors` 非空（含更正过滤已删公告），务必写进这句，避免只交一张无说明的「无新动态」卡。没有缺口就只输出 `NO_REPLY`。交付后省略卡片正文复述。

## 失败

SEC 代码表不可用、美股扫描失败，或 A 股与港股的 `finance` 一次都没有写成 raw JSON：严禁编造公告或提交空卡片冒充成功。提交一行失败说明，`cards` 省略：

```python
automation_report(
    action="submit",
    report_run_id="daily-filing-alert-<report_date 或当天香港日期>",
    material="yes",
    failure_text="覆盖池公告日报第 N 步失败：原因",
)
```

失败文本确认发出后，最终回复只写 `NO_REPLY`。工具没能保存这行时，最终回复只留这一行。

部分代码的公告没取回、其余扫描成功时，仍然提交卡片，并把缺口写进确认发送后的那一句话。

## 口径

- 美股列表和正文来自 SEC EDGAR `submissions`（申报档案列表接口）与公告原文，由 `scan` 读取；代码到 CIK 精确匹配 `company_tickers.json`，若存在多个 CIK 则跳过并写入 `errors`。A 股和港股列表来自 `finance` 的 `cn-hk` `filings`，字段是日期、标题、类别和 `pdf_link`。A 股正文由 `WebFetch` 取回该链接，`scan-cn-hk` 生成 `body_excerpt`、`digest` 和翌日披露解析；港股及尚未解析的回购/翌日披露公告由 `buyback_parse.py` 提取回购明细。
- 回看按公告日期的日历日比较。SEC `scan` 的默认天数是 3，本轮改用窗口里的 `{LOOKBACK_DAYS}`，包含报告日减去该天数。`scan-cn-hk` 传入同一个天数。A 股只保留从报告日往前数的 `days` 个日历日，`days` 为 1 时只有报告日。港股保留报告日减去 `days` 到报告日的下一天。raw JSON 的 `end` 写香港报告日。
- 观察名单之外的公司跳过入卡。调试时可以加 `--tickers` 或 `--no-enrich`，定时运行省略这两项。

## 维护说明（信任边界）

- **信任边界**：校验针对的是模型填写的 `payload.json` 写错、抄错、混入金额或篡改结构（`payload` 内容视为不可信）；对 A 股而言，`scan-cn-hk` 离线校验 raw JSON 中模型通过 `WebFetch` 写回的 `notice_text`（依赖模型忠实写入原文）；对港股而言，`scan-cn-hk` 强制忽略任何 `notice_text`，只认 `buyback_parse.py` 下载并解析真实 PDF 后写入 `buyback_results` 的结果。以上校验假设脚本自己生成的扫描输出文件本身未被人为伪造。
- **渲染校验机制**：渲染入口先递归剥离 `payload` 中所有 `_` 开头的内部字段；扫描与渲染阶段均按编号统计应有事件/交易行数并逐条严格校验数字格式（含千分位与 `Unicode Cf` 检查）、逐行之和与「合共」自洽性及「證券代號」归属，任意异常整条置为 `validated: false`。按 `id`/`url` 匹配扫描记录时同步核对公司代码、ID 与链接一致性，链接、标题和日期取自扫描记录；未核对条目使用受限标题与标签白名单；对涉及翌日披露或回购的公司执行上文去数字保护并清除全部模型数值/币种字段；更正过滤、`lead` 生成、页脚生成、重跑保留既有 `validated: true` 记录及「增持」独立分类均按上文规则执行。
