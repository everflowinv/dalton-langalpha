---
name: dalton-tob-industry-weekly
description: "无人值守的中美 ToB 软件与信息服务周报：按 120 家业绩五要素采集事实并成稿，并用 automation_report 提交纯文本。"
---

# China & Global ToB Tracker

无人值守。不提问，不创建子任务。安装路径是 `.agents/skills/dalton-tob-industry-weekly/`。命令用 `scripts/tob_industry_weekly.py`，A 股标题筛选用 `scripts/cninfo_a_scan.py`。写文件时带 `--workspace-dir <workspace>`。`<workspace>` 是本轮工作区根目录。

接收方在 `metadata.report_profile`，配置名是 `tob-industry-weekly`。技能正文和工具参数都不写接收方。

状态目录是 `state/tob-industry-weekly/`，下面有 `notes.jsonl`、`sessions.json`、`collection-runs/`、`snapshots/`、`a-filings.json`、`a-scan.json`。归档是 `research-outputs/tob-industry-tracker/YYYY-MM-DD.md`。`YYYY-MM-DD` 用当周周五（Asia/Hong_Kong），公式是运行日减去 `(weekday - 4) % 7`。周日跑、文件名用周五。`report_run_id` 是 `tob-industry-weekly-<friday>`。历史状态由主助手预先导入；本轮不导入、不重建历史状态。

两种模式由自动化指令写明。采集只入库，不成稿、不提交、不写快照。成稿提交卡片全文，确认发送后再归档，然后写快照。

正文先给判断。报告正文不用这些防御性否定：不代表、并非、而不是、不能、不构成、不应。栏目、证据等级和校验类型名保持原样。

## 口径

事件驱动。这份周报回答过去一周谁发了业绩、发得怎么样、管理层说了什么、卖方怎么看。覆盖池 120 家（A 56 / HK 16 / US 48），18 个细分，名单在 `references/universe.json` 和 `references/subsectors.json`。池外公司本周发业绩且属于软件或信息服务的，写入「池外亮点」。淡季没有业绩就写短版，只留下周预告。

金山办公 `688111.SH` 与金山软件 `3888.HK` 按主体分开。有赞代码是 `6051.HK`。公司名以披露源返回名为准。换代码时改 `universe.json`。

每家已发业绩的公司按五要素写，缺哪项写「未披露」，不推算：

1. 当期 beat/miss：营收、利润相对市场共识或公司指引，写明对比的是哪一个，给数字和幅度。
2. 指引 beat/miss：下季或全年指引相对预期，写明是区间还是单点。
3. 业绩重点：分业务线增速、毛利率、现金流、客户或续费指标（ARR、NRR、cRPO 等，按公司披露口径）。
4. 管理层发言：业绩会原话不超过 20 词，只放进本地全文稿；卡片里改写，并给一句话解读。
5. 卖方点评与预测修正：机构名、日期、口径、上修或下修、目标价变化。

预测修正从 AlphaEngine 卖方点评和新闻提取，是文本口径。每条带机构名和日期。`finance(route="estimates", operation="consensus", ticker=...)` 只作单家共识快照，修正方向仍以点评文本为准。

本报不计算 120 家涨跌幅。行情日期只用来标业绩窗口终点。

## 窗口

`plan` 分开给出 `price_window` 和 `source_window`，没有合并的 `window` 字段。

- 业绩窗口：`price_window.start` 是当周周五往前 7 天，`price_window.end` 是 `price_as_of`。三市交易日还没齐时，终点回到周五，正文按 `holiday_note` 写，请求日不当成已核对的行情日期。`price_as_of` 早于起点时不输出 `price_window`（`price_window_missing` 为 true），`holiday_note` 写明该交易日和窗口起点，并说明本期不输出业绩窗口。
- 信源窗口：`source_window.start` 是上一期截稿日，即当周周五减 7 天的上周五（香港日期），当天算在窗口内。`source_window.end` 是本次运行的香港日历日，`source_window.end_at` 是带 `+08:00` 的采集时刻。上周五当天的事实若已写进上期归档 `research-outputs/tob-industry-tracker/<上周五>.md`，成稿时读该文件全文，已写过的不再重复写；归档不存在就照常写。`notes --since` 仍返回这些记录。

交易日从市场快照读，只记录工具返回的日期：

```text
get_market_overview(region="cn", date=FRIDAY)
get_market_overview(region="hk", date=FRIDAY)
get_market_overview(region="us", date=FRIDAY)
```

非交易日会落到前一个交易日。读返回里的 `date`，三个市场分别交给 `record-sessions` 的 `--a-date`、`--hk-date`、`--us-date`，并带 `--as-of <friday>`。某个市场没有日期就省略对应参数。`sessions.json` 按报告周 `<friday>` 写在 `weeks` 里。本周没有记录的市场就是缺失，不读上一周的日期。`finance(route="cn-hk", operation="quote", ticker=...)` 只是现货备用，不接受历史日期，不拿它填交易日。

然后跑 `perf`。`price_as_of` 是本周三个交易日里最早且都不晚于请求日的那一天。`as_of` 在有交易日时等于 `price_as_of`。缺一个市场时 `price_as_of` 为空，`missing_count` 是缺的市场数，业绩窗口终点回到周五。这个 `price_as_of` 早于窗口起点时，`price_as_of` 留空，`price_window` 不输出，`as_of` 回到周五，原因在 `holiday_note`。

## 证据

按这个顺序扫：公司披露、AlphaEngine 纪要、AlphaEngine 卖方、网页。社区默认不跑。D 级不入库。

- A：公司披露，有口径和期间。可以直接进正文。
- B：业绩会、路演或调研里的口头数字，有基数和期间。
- C：卖方点评、专家、第三方估算。进报告时标「估算」或写清机构与日期。
- D：转述、无口径、无链接。丢掉。

B 和 C 入库要带 `--cross-check`。本周没有印证就传 `--cross-check none`，这条只进观察项。印证来自卖方、客户、渠道或上游，同一家公司换个说法不算。`earnings_call`、`roadshow_minutes`、`expert_call` 还要带 `--source-attribution`。逐字引用按英文词加中日韩字符计数，超过 20 个拒收。

进卡片正文要同时有：可核对数字、口径与期间、基数或同比基准。够不上的进观察项，并写清缺什么。

### 公司披露

- 美股：`finance(route="sec", operation="list-filings", ticker="MSFT", form="8-K", limit=20)`，再单独查 `form="10-Q"`。外国发行人再查 `form="6-K"`。`form` 放在顶层。这条没有 `start`/`end`，由你按信源窗口滤日期。正文用 `finance(route="sec", operation="attachments", ticker="MSFT", form="8-K", params={"accession": "0000789019-26-000010", "document_type": "EX-99.1", "max_chars": 100000})`。新闻稿只作交叉，数字回 filing。
- A 股：`finance(route="cn-hk", operation="filings", ticker="600588", start=START, end=END, limit=30)`。代码用 6 位数字，去掉 `.SH` / `.SZ`。`start` 与 `end` 都要，`end` 不得晚于香港当天。单次返回条数达到 `limit` 时，按更小日期段再查并合并。任一段仍顶满 `limit`，在「本期缺口」注明可能截断。不要用全市场分类翻页。
- 港股：同一条 `filings`。代码去掉 `.HK` 后左侧补零到 5 位（`0268.HK` → `00268`，`0020.HK` → `00020`，`6051.HK` → `06051`）。

A 股把每次 `finance(route="cn-hk", operation="filings")` 的返回原样放进 `state/tob-industry-weekly/a-filings.json`，格式见第 3 节，再交给筛选脚本。脚本按东八区换算 `announcementTime`，起止日都算在窗口内，只留业绩类标题，并按标题、日期、链接去重。它不打开网络连接。港股标题规则不同，港股公告由你直接阅读后 `add-note`，不进这个筛选。

### AlphaEngine

纪要：

```text
research(route="alphaengine", operation="search_library", query="用友 业绩发布会 管理层发言", start=START, end=END, limit=20, params={"sort": "relevance", "document_categories": ["meeting_minutes", "sell_side_comment"]})
```

美股可用英文正式名加 earnings call。`start` 与 `end` 成对。带 query 时 `sort` 只用 `relevance`。`limit` 传 20。查具体公司只传中文名或英文正式名，不传 `industry_names`，不传 `markets`。`institution_meetings` 或 `personal_meetings` 与卖方分类同用会得到 `E_SCOPE_CONFLICT`，这两类要单独调用。

全文：

```text
research(route="alphaengine", operation="get_document", params={"doc_id": "DOC_ID", "content_field": "original_text", "offset": 0, "max_chars": 30000})
```

`content_field` 可用 `original_text`、`question_answer` 或 `content`。卖方长文另一次 `get_document`，类别用 `sell_side_report`。纪要和点评只取最近 2 周；更旧的标发布日期，并写「背景，非本周增量」。纪要走 AlphaEngine。回退到 roic.ai 禁止。AlphaEngine 不可用时，纪要和卖方点评写「未取到」，缺口进卡片「⚠️ 本期缺口」。公告数字留在业绩栏，不拿公告顶替纪要或卖方。

Guidepoint 不是每周必扫。只有需要专家库时才用：`research(route="guidepoint", operation="search_library", query=..., start=..., end=..., limit=30, params={"keywords": [...], "recency_bias": 50})`。逐字引用不超过 20 个英文词，并写来源属性。不写专家姓名。

网页：`web_search(query=QUERY, start_date=START, end_date=END, time_range="week")`。不传 `country`、`language`、`domain_filter`。用来补财报日历、目标价新闻和池外亮点。

社区默认关闭。用户点名某家口碑时才用 `community(route="xueqiu", operation="search", query=QUERY)` 或 `community(route="last30days", operation="search", query=QUERY, days=7)`。社区内容只作发现。

## 1. 自检

```bash
python3 .agents/skills/dalton-tob-industry-weekly/scripts/tob_industry_weekly.py \
  --json self-test --workspace-dir <workspace> --network
```

`--network` 只确认脚本不去连公告接口。自检失败就停止。采集失败不提交报告。成稿在正文还没保存时，按第 6 节提交一行失败说明。

## 2. 周五与计划

```bash
python3 .agents/skills/dalton-tob-industry-weekly/scripts/tob_industry_weekly.py \
  --json anchor --workspace-dir <workspace> --as-of <as-of>
```

`<as-of>` 省略时用香港当天。输出的 `friday` 和 `report_run_id` 用于稿件名、归档、快照和提交。

```bash
python3 .agents/skills/dalton-tob-industry-weekly/scripts/tob_industry_weekly.py \
  --json plan --workspace-dir <workspace> --as-of <friday>
```

`plan` 分开给出 `price_window` 和 `source_window`。行情窗口终点是 `price_as_of`；终点早于起点时 `price_window` 为空。信源窗口从上一期截稿日（上周五，`source_window.start`）到本次采集时刻（`source_window.end_at`，香港时间）。上周五当天已写进上期归档的事实，成稿时按第 4 节不再重复写。

## 3. 采集

先记采集开始时间，再记交易日，再取公告和事实。这一步不写卡片，不调用 `automation_report`，不跑 `snapshot`。

```bash
python3 .agents/skills/dalton-tob-industry-weekly/scripts/tob_industry_weekly.py \
  --json start-collection --workspace-dir <workspace> --as-of <friday>
```

同一周再次调用 `start-collection` 时，已有的 `started_at` 保持不变，以最早的开始时间为准。

```bash
python3 .agents/skills/dalton-tob-industry-weekly/scripts/tob_industry_weekly.py \
  --json record-sessions --workspace-dir <workspace> --as-of <friday> \
  --a-date <a-date> --hk-date <hk-date> --us-date <us-date>

python3 .agents/skills/dalton-tob-industry-weekly/scripts/tob_industry_weekly.py \
  --json perf --workspace-dir <workspace> --as-of <friday>
```

A 股只按下面这一种格式写入 `state/tob-industry-weekly/a-filings.json`。键是 6 位代码。值是列表，每个元素是一次 `finance(route="cn-hk", operation="filings")` 的返回原样保存，含 `ok`，以及 `rows` 或 `data`。日期段被拆开时，同一家放多段；被拆分的那次顶满 `limit` 的原始调用不放进列表。列表为空，或名单里没有这家，记为 `missing`。任一元素的 `ok` 不是 `true`，这家记为 `error`，并保留返回里的错误文字（`error_details`）。全部 `ok` 为 `true` 才算成功：合并各段的 `rows` 或 `data` 后再筛选。`<source-start>` 用 `source_window.start`。

```json
{
  "companies": {
    "600588": [
      {"ok": true, "rows": [{"title": "2026年第三季度报告", "announcementTime": 1786671000000, "adjunctUrl": "finalpage/q3.pdf"}]},
      {"ok": true, "data": [{"title": "2026年半年度报告", "date": "2026-08-20", "adjunctUrl": "finalpage/h1.pdf"}]}
    ],
    "300378": [
      {"ok": false, "error": "bridge timeout"}
    ]
  }
}
```

上面用友 `600588` 是两段都成功，鼎捷 `300378` 是失败。56 家里没写到的公司是 `missing`。

```bash
python3 .agents/skills/dalton-tob-industry-weekly/scripts/cninfo_a_scan.py \
  --json screen --workspace-dir <workspace> \
  --filings state/tob-industry-weekly/a-filings.json \
  --start <source-start> --end <friday>
```

`incomplete` 为 true（同时 `ok` 为 false）时，把 `errors` 和 `missing` 里的公司写进「本期缺口」，不能当作“无公告”。`scanned` 是取数成功的家数。成功但没有业绩类标题的公司留在 `scanned`，不进 `errors` 或 `missing`。

筛选脚本的离线自检（日期换算与闭区间）在主自检之后跑：

```bash
python3 .agents/skills/dalton-tob-industry-weekly/scripts/cninfo_a_scan.py \
  --json self-test --workspace-dir <workspace>
```

过去一周发布业绩的公司（含池外亮点）逐条入库，一条记录一件事：

```bash
python3 .agents/skills/dalton-tob-industry-weekly/scripts/tob_industry_weekly.py \
  --json add-note --workspace-dir <workspace> \
  --ticker MSFT --company 微软 --market US \
  --event-type earnings --evidence-grade A --direction beat \
  --source-type company_disclosure \
  --claim 营收同比增加 --source-date <friday> --week-of <friday>
```

B 或 C 级加上 `--cross-check CROSS` 或 `--cross-check none`。纪要、路演、专家访谈加上 `--source-attribution TEXT`。`event-type` 可以是 `earnings`、`guidance`、`management`、`sellside`、`revision`、`calendar`。

采集结束只补 `finished_at`，不改 `started_at`，不写快照。还没有 `started_at` 时这条命令失败，先跑本节开头的 `start-collection`。

```bash
python3 .agents/skills/dalton-tob-industry-weekly/scripts/tob_industry_weekly.py \
  --json record-collection --workspace-dir <workspace> --as-of <friday> \
  --note "采集完成"
```

最后回复 `NO_REPLY`。

## 4. 成稿前的读数

标题、`temp/` 文件名、归档、快照和 `report_run_id` 用同一个周五。先看对比，再写正文。没有基线就整节不写「对比上期」。

```bash
python3 .agents/skills/dalton-tob-industry-weekly/scripts/tob_industry_weekly.py \
  --json diff --workspace-dir <workspace> --as-of <friday>

python3 .agents/skills/dalton-tob-industry-weekly/scripts/tob_industry_weekly.py \
  --json notes --workspace-dir <workspace> --since <source-start>
```

`<source-start>` 用 `plan` 的 `source_window.start`（上周五，当周周五减 7 天）。成稿按这个完整信源窗口读采集记录，周一到周四写入的记录也要进正文。`source_date` 或 `week_of` 有一个落在窗口内就保留，上周五当天同样保留。上周五当天的事实要再对上期归档 `research-outputs/tob-industry-tracker/<上周五>.md`：文件存在时读全文，已经写过的事实不再重复写；文件不存在就照常写。快照只有数量和公司代码，不拿它做这条去重。

成稿补扫采集开始之后新出的公告时，先读 `state/tob-industry-weekly/collection-runs/<friday>.json` 的 `started_at`，用它当补扫起点。命中的用 `add-note` 入库。

`perf` 的 `requested_as_of` 只是请求日。`price_as_of` 与请求日不同时，第一段写 `price_as_of`，全文按实际行情日期叙述。`price_window` 为空时按 `holiday_note` 写，本期不输出业绩窗口。

香港时间 16:00 截稿。美股周五盘前或盘后发布的业绩进下一期，在全文「信源与数据口径」里写明。

卡片按 `templates/card.md` 写到 `temp/tob-industry-weekly/<friday>-card.md`。全文按 `templates/report.md` 写到 `temp/tob-industry-weekly/<friday>.md`。栏目保留：本周总览、A股/港股业绩、美股软件业绩、信息服务、管理层重点发言、卖方点评与预测修正、下周预告、本期缺口。全文另有池外亮点、观察项、待验证、信源与数据口径、免责声明。

```bash
python3 .agents/skills/dalton-tob-industry-weekly/scripts/tob_industry_weekly.py \
  --json validate --workspace-dir <workspace> \
  --file temp/tob-industry-weekly/<friday>-card.md --profile card

python3 .agents/skills/dalton-tob-industry-weekly/scripts/tob_industry_weekly.py \
  --json validate --workspace-dir <workspace> \
  --file temp/tob-industry-weekly/<friday>.md --profile report
```

`vague_phrase`、`missing_section`、`feishu_latex_risk`、`caliber_label`、`card_too_long` 清零才提交。`number_without_period` 是提醒。卡片上限 10000 字。不用 Markdown 表格，正文不出现 `$`。

三组口径必须标清：指引和实际分开；营收、ARR、cRPO 不混用；beat/miss 写明对比的是市场共识还是公司指引。卖方修正那行要出现「盈利预测」或「目标价」。公司改指引时写明「指引」并给新旧区间。

## 5. 必答

1. 本周几家发业绩，beat / miss / inline 各几家，分市场。
2. 指引上调或下调有没有集中出现。
3. 管理层发言里有没有行业性信号（AI 支出、云需求、招聘、定价）。
4. 卖方预测修正的方向和集中在哪些细分。文本口径。
5. 下周哪些重点公司发业绩。

## 6. 提交

提交物是卡片。全文只落本地归档。卡片不设外部文档链接，也不设「全文」栏。会影响判断的缺口写进「⚠️ 本期缺口」，最多三条。

校验通过后，在第一行标题之后插入一行 `-# 业绩窗口 MM-DD 至 MM-DD ｜ 信源窗口 MM-DD 至 MM-DD ｜ 事件驱动，120 家`，其余不动，然后把这份卡片放进 `text`。`report_run_id` 用 `tob-industry-weekly-<friday>`。

```python
automation_report(action="submit", report_run_id=RUN_ID, material="yes", text=CARD_MARKDOWN)
automation_report(action="status", report_run_id=RUN_ID)
```

只有返回的 `report_run_id` 一致、`content_kind` 为 `text`、`ok` 为 true、`delivery_confirmed` 为 true、`status` 为 `sent`，才归档全文，再写快照。两条命令用同一组确认参数。未确认时两条都不写。

```bash
python3 .agents/skills/dalton-tob-industry-weekly/scripts/tob_industry_weekly.py \
  --json archive --workspace-dir <workspace> \
  --file temp/tob-industry-weekly/<friday>.md --as-of <friday> \
  --content-kind text --delivery-confirmed --status sent

python3 .agents/skills/dalton-tob-industry-weekly/scripts/tob_industry_weekly.py \
  --json snapshot --workspace-dir <workspace> --as-of <friday> --write \
  --content-kind text --delivery-confirmed --status sent
```

同一 `report_run_id` 已有不同正文时返回 `content_conflict`，不改正文，也不换第二个标识。`not_automation_run` 时停止，不归档。确认发送后，最终回复只写 `NO_REPLY`。

正文还没保存时，省略 `text`，提交一行说明哪一步、什么错：

```python
automation_report(
    action="submit",
    report_run_id=RUN_ID,
    material="yes",
    failure_text="ToB 行业周报第 N 步失败：原因",
)
```

正文已经保存、投递确定失败时，不再提交 `failure_text`。同一 `report_run_id` 换成另一份正文会返回 `content_conflict`。平台对已保存的纯文本，在投递确定失败后向 profile 的 `failure_targets` 发一次告警。未确定的投递，以及权限或路由跳过，不发告警。已经发出的告警不重复发。失败接收方的目标若与正文投递目标相同，就跳过该接收方，不看正文那条是否已经发送成功；其余失败接收方仍各发一次。profile 没有 `failure_targets` 时不发。

## 已知限制

- 财报日历没有专用数据源，靠 AlphaEngine、`web_search` 和逐家公告拼。池内 120 家靠逐家检索。
- `finance(route="sec", operation="list-filings")` 没有日期参数，日期在本地滤。
- `finance(route="cn-hk", operation="quote")` 没有历史日期。交易日来自 `get_market_overview` 的返回，按报告周写入 `sessions.json`。返回里没有日期时，`price_as_of` 留空，业绩窗口终点用周五，并写上 `holiday_note`。三市最早的 `price_as_of` 早于窗口起点时不输出业绩窗口。
- 公告筛选只看 A 股标题。只发业绩说明会通知的公司不算命中。标题写法特殊、命中数明显偏低时，再读一遍原始 filings。
- 预测修正无全市场统计库。覆盖到写进本期的机构为止。
- 美股财报高峰单周可能超过 30 家。时间不够时先放下池外亮点，池内照写。
