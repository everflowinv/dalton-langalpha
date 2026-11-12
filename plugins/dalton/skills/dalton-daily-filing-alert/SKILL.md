---
name: dalton-daily-filing-alert
description: "无人值守的覆盖池公告日报：扫描美股 SEC 与中港公告，分层摘要，并用 automation_report 提交卡片。"
---

# 覆盖池公告日报

无人值守。不提问，不创建子任务。本轮只做这一份日报。

安装路径是 `.agents/skills/dalton-daily-filing-alert/`。下面的命令用这个目录里的 `scripts/daily_filing_alert.py`。每条命令都带 `--workspace-dir <workspace>`。`<workspace>` 用本轮工作区根目录，不靠向上查找说明文件来猜。

接收方在任务的 `metadata.report_profile` 里，配置名是 `daily-filing-alert`。卡片和工具参数都不写接收方。

覆盖池随技能走：`watchlist.txt`（美股）、`watchlist_cn_hk.txt`（A 股与港股）。本轮不改这两份名单，也不运行导入脚本。

状态在 `state/daily-filing-alert/`：`last_check.json`、`cik_map.json`、`enrich_cache.json`、`filings.json`。`last_check.json` 在 SEC `scan` 成功返回时按代码写入，确认发送之后不再改。整份 SEC 代码表拉不到、并且没有缓存时，`scan` 返回 `ok` 为 false，也不写 `last_check.json`。`scan-cn-hk` 不写 `last_check.json`。草稿只放在 `temp/daily-filing-alert/`。

正文先直接给判断，再写事实。自行撰写的标题、摘要、导语和最终回复不用这些防御性否定：不代表、并非、而不是、不能、不构成、不应。栏目、数字、来源和失败说明保持原样。交付的卡片文本不用 emoji。

## 分工

- 美股扫描、正文补齐、分层、金额汇总和卡片版面由脚本完成。模型不重算金额，不加字段去改重点或例行。
- A 股和港股公告在 ExecuteCode 里调用 `finance` 取回，写成一份 JSON。脚本按窗口筛选、去重，把 A 股正文写成 `body_excerpt`、`digest` 与回购或翌日披露字段，再生成卡片。
- 模型写摘要、导语和 payload，再提交卡片。

## 窗口

先取香港日期和回看天数。周一 `lookback_days` 为 3，`window_note` 为「覆盖上周五至今」；其余工作日 `lookback_days` 为 1，`window_note` 为「覆盖过去 24 小时」。`report_run_id` 为 `daily-filing-alert-<YYYY-MM-DD>`。后面的 `{LOOKBACK_DAYS}`、`<report_date>`、`RUN_ID` 用这一步的字段，不要另算。顶层 `start`、`end` 只给美股。A 股和港股用 `cn_hk` 每一项自己的 `start`、`end`。

```bash
python3 .agents/skills/dalton-daily-filing-alert/scripts/daily_filing_alert.py \
  --json window --workspace-dir <workspace>
```

记下 `start`、`end`、`limit` 和 `cn_hk`。`start` 与 `end` 是美股边界：报告日减去 `lookback_days` 到报告日，两端都包含。SEC `scan` 仍用 `--days`，不读这两个日期。`cn_hk` 每一项有自己的 `start`、`end`，以及 `ticker`（交给 finance 的 5 位港股或 6 位 A 股代码）、`code`（卡片上的代码）、`name` 和 `market`。A 股沿用按天循环：`lookback_days` 为 1 时 `start` 和 `end` 都是报告日，只含当天；为 3 时含报告日和它前面的两天。港股沿用原来的检索边界：`start` 是报告日减去 `lookback_days`，`end` 是报告日的下一天。

自检可以单独跑。自检失败就按文末失败节处理，不继续扫描。

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

成功时 `ok` 为 true。读 `--output` 那个文件。标准输出只是状态。命令中断时从该文件恢复，同一轮不要重跑 `scan`。

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

`A_START`、`A_END` 用 300308 那一项的 `start`、`end`。`HK_START`、`HK_END` 用 00700 那一项的 `start`、`end`。其余代码同样用自己的那一项。

一次返回的条数等于 `limit` 时，把该代码的日期区间拆成更小的几段，用同样的 `route`、`operation`、`ticker`、`limit` 再调用，只改 `start` 和 `end`。每一段都放进 `calls`，`ticker` 保持不变；那次达到 `limit` 的原始调用不放进 `calls`，只放拆分后的各段。脚本按标题、日期和链接合并去重。某一段的条数仍然等于 `limit`，就继续拆这一段。已经拆到单独一天并且仍然等于 `limit` 时停止，该代码会标成截断，写进确认后的缺口句。不要编造没有返回的公告。

`finance` 的 `cn-hk` 只有列表，没有正文接口。对 A 股公告的 `pdf_link` 取正文。港股不取。每条 A 股公告取一次，条数就是这次窗口里的 A 股公告条数，与原来对每条 A 股公告取一次正文相同。单条只调用一次：原来单条正文最多按页取 12 页，这里一次取回该链接的全文，不再按页拆。标题里看不出翌日披露或回购时也要取，正文开头可能才是翌日披露报表。

```python
WebFetch(url, prompt)
```

`url` 用该条的 `pdf_link`。`prompt` 要求返回公告正文原文，保留翌日披露报表的章节、回购股数、金额、价格区间，以及注销或持作库存股的去向。公开页面打不开时，在该条写 `enrich_error`，内容以「取正文失败:」开头，然后继续其余公告。不传 cookies、Authorization 或登录态。

把取回的原文写进该条的 `notice_text`。脚本写入 `pdf_url`、`body_excerpt`；标题含「翌日披露」，或正文开头含「翌日披露報表」时，再写入 `ndr` 和 `digest`。解析失败时脚本写入 `enrich_error`。这些输出字段与原来的正文补齐结果同名。

把全部结果写成 `temp/daily-filing-alert/<report_date>-cn-hk-raw.json`。`rows` 用工具返回的列表；工具放在 `data` 里时照原样留下，脚本两种都认。同一 `ticker` 拆分后的各段都留在 `calls` 里（不含被拆分的原始调用）。顶层 `end` 写香港报告日。

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

然后交给脚本筛选。脚本不访问网络。

```bash
python3 .agents/skills/dalton-daily-filing-alert/scripts/daily_filing_alert.py \
  --json scan-cn-hk --workspace-dir <workspace> \
  --days {LOOKBACK_DAYS} \
  --input temp/daily-filing-alert/<report_date>-cn-hk-raw.json \
  --output temp/daily-filing-alert/<report_date>-cn-hk.json
```

读 `--output`。公告字段是 `title`、`type`、`date`、`url`。A 股另有 `pdf_url`、`body_excerpt`，翌日披露报表另有 `ndr` 和 `digest`，取不到正文时有 `enrich_error`。筛选用各市场原来的日期边界，锚点是 raw JSON 的 `end`（香港报告日）和命令里的 `--days`。A 股在 `--days` 为 1 时只留报告日当天。港股从报告日减去 `--days` 留到报告日的下一天。某代码 `ok` 为 false，或 raw JSON 里没有这个代码，结果里的 `errors` 有说明，`announcements` 为空。`truncated` 为 true 表示还有一段的返回条数达到 `limit`。合并后的总条数可以多于 50。`day_errors` 和每条结果里的 `errors` 留到最后那句回复，不写进卡片。

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
- `stats` 只填 `scanned`：美股用 `tickers_scanned`，A 股用 `cn_stocks_scanned`，港股用 `hk_stocks_scanned`。某个市场这次没扫就省略该 key。不填 `hit`。
- `lead` 只点今天最重要的一两件事，约 40 个汉字以内，写清代码、事件和金额。没有值得点名的事就省略 `lead`。
- `items[].tag` 用原名：8-K、6-K、10-K、10-Q、Form 4、Form 144、13D、13G、A股公告、港股公告。SEC 结果里的 `type` 已把 4 和 4/A 写成 Form 4。
- `items[].url` 用结果里的 `url`。
- `items[].amount_usd` 只在知道美元整数时填写。税款代扣、常规授予、人民币金额都省略。人民币保留「元人民币」，不换算。
- 美股 `name` 用代码本身。A 股和港股 `code` 用窗口 `cn_hk[].code`，`name` 用中文名。
- Form 4 先用 `parsed`。`parsed.tx_type` 保留原始交易类型。`parsed.summary` 为空，或只有 `DERIVATIVE ACQUISITION` 且没有金额，视为常规授予，同一家公司合成一条 item。`parsed.summary` 为 GIFT、DISPOSE 或其他未单独成句的原始类型时，按这个类型写，不要写成常规授予。金额为 $0 或股数为 0 的条目不输出。行权后卖出只写卖出结果和套现金额。税款代扣同样合成一条。多位内部人卖出时，`summary` 写合计股数和金额，`items` 按金额从大到小。
- 其他表单按 `headline`、`cover_summary`、`item_labels`、`body_excerpt` 的顺序写具体事项。这些字段都空时，`text` 写「未取得正文或具体事项」。不用 accession 当标题，也不按表单类型编事件。
- A 股 `text` 优先用 `digest`，没有则用 `body_excerpt`，再没有才用标题。`digest` 里的回购股数、金额、价格和注销或库存股去向照抄。港股按 `title` 和 `type` 写一句 `text`，数字和日期以标题为准。标题没有的金额不要补。`summary` 只写 `digest`、正文或标题里已经出现的回购、业绩或股份变动。
- 卡片上不放警告。抓取超时、接口报错、返回条数达到 `limit` 都留到最后一句回复。
- 窗口内没有新动态时，两个 section 的 `companies` 用空数组，`stats` 照常填。仍要提交这张灰色卡片，`material` 用 `yes`。

分层由脚本按 tag 和金额完成。实质公告进重点区。只有内部人交易且合计低于 1000 万美元的公司进例行区。

```bash
python3 .agents/skills/dalton-daily-filing-alert/scripts/daily_filing_alert.py \
  --json render-card --workspace-dir <workspace> \
  --payload temp/daily-filing-alert/<report_date>-payload.json \
  --output temp/daily-filing-alert/<report_date>-card.json
```

`ok` 为 true 后，读 `card.json` 的对象作为 `CARD`。同一天重试沿用同一个 `RUN_ID` 和同一份卡片字节。`card_conflict` 表示磁盘上的卡片和这次渲染不同，保留原文件，不要改 payload 后再渲第二张。

## 4. 提交

调用 `automation_report`，不从 Python 导入，也不用 Bash 调用。`config` 以及用户、工作区、线程、运行和自动化身份由服务器注入。`cards` 只含这一张卡片。

```python
automation_report(action="submit", report_run_id=RUN_ID, material="yes", cards=[CARD])
automation_report(action="status", report_run_id=RUN_ID)
```

只有返回里的 `report_run_id` 与 `RUN_ID` 一致，`content_kind` 为 `cards`，并且 `ok` 为 true、`delivery_confirmed` 为 true、`status` 为 `sent`，才算发送成功。

未确认时，保留原卡片，用同一 `RUN_ID` 和同一份 `cards` 再提交一次。不要换第二个 `report_run_id`。`content_conflict` 表示这个 `report_run_id` 已经保存了不同正文，不要改卡片。`code` 为 `not_automation_run` 时停止：最终回复说明本次不是自动化运行，不附卡片正文。

确认发送之后，有抓取缺口就只写一句话，写上有新动态的公司家数，并补上缺口（例如「已提交卡片：9 家公司有新动态；300502 公告未取回」）。SEC 结果里任一公司的 `errors` 非空，必须写进这句，不能只交一张不加说明的「无新动态」卡。没有缺口就只写 `NO_REPLY`。不要重复卡片正文。

## 失败

SEC 代码表不可用、美股扫描失败，或 A 股与港股的 `finance` 一次都没有写成 raw JSON：不要编造公告，不要提交空卡片冒充成功。提交一行失败说明，`cards` 省略：

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

- 美股列表和正文来自 SEC submissions 与公告原文，由 `scan` 读取。A 股和港股列表来自 `finance` 的 `cn-hk` `filings`，字段是日期、标题、类别和 `pdf_link`。A 股正文由 `WebFetch` 取回该链接，脚本生成 `body_excerpt`、`digest` 和回购或翌日披露解析。
- 回看按公告日期的日历日比较。SEC `scan` 的默认天数是 3，本轮改用窗口里的 `{LOOKBACK_DAYS}`，包含报告日减去该天数。`scan-cn-hk` 传入同一个天数。A 股只保留从报告日往前数的 `days` 个日历日，`days` 为 1 时只有报告日。港股保留报告日减去 `days` 到报告日的下一天。raw JSON 的 `end` 写香港报告日。
- 观察名单之外的公司不进卡片。调试时可以加 `--tickers` 或 `--no-enrich`，定时运行不用这两项。
